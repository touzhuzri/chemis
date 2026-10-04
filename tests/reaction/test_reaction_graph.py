"""Reaction graph (Stage 5.3).

A derived, deterministic, depth-limited graph over local data:
Molecule --SUBSTRATE_OF--> Reaction --SUPPORTED_BY--> Literature. It never
generates a product (no product pattern is encoded), never recommends, and
never reaches the network.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from crea.core.errors import ReactionGraphError
from crea.core.models.literature import Identifiers, Paper
from crea.core.models.reaction_evidence import ReactionEvidenceType
from crea.core.models.reaction_graph import GraphEdgeType, GraphNodeType
from crea.services.knowledge_base import KnowledgeBase
from crea.services.literature_record import LiteratureRecordService
from crea.services.reaction_evidence import ReactionEvidenceService
from crea.services.reaction_graph import MAX_DEPTH, ReactionGraphService


@pytest.fixture()
def env(tmp_path):
    kb = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    kb.initialize()
    return kb


def _register_molecule(kb, smiles: str):
    from crea.semantic.resolver import ChemicalInput, SemanticResolver
    from crea.services.identity import IdentityResolutionService

    entities = kb.entity_registry()
    resolver = SemanticResolver(identity_service=IdentityResolutionService(),
                                entity_registry=entities)
    return resolver.resolve(ChemicalInput.of("SMILES", smiles)).entity


def _link_literature(kb, template_id="primary-alcohol-oxidation",
                     paper_id="paper-A"):
    LiteratureRecordService(kb).create(Paper(
        paper_id=paper_id, title="An oxidation study", year=2021,
        journal="J. Synth. Chem.", identifiers=Identifiers(doi="10.1000/abc"),
        retrieved_at="2026-01-01"))
    ReactionEvidenceService(kb=kb).link_literature(
        template_id, paper_id=paper_id,
        evidence_type=ReactionEvidenceType.TRANSFORMATION_SUPPORT,
        support_scope="primary alcohol -> aldehyde")


# -- 1. molecule root --------------------------------------------------------


def test_molecule_graph_depth1_has_reactions_not_literature(env):
    entity = _register_molecule(env, "CCO")
    _link_literature(env)
    graph = ReactionGraphService(env).molecule_graph(entity.entity_id, depth=1)
    types = {n.type for n in graph.nodes}
    assert GraphNodeType.MOLECULE in types
    assert GraphNodeType.REACTION in types
    assert GraphNodeType.LITERATURE not in types
    assert all(e.type is GraphEdgeType.SUBSTRATE_OF for e in graph.edges)


def test_molecule_graph_depth2_adds_literature(env):
    entity = _register_molecule(env, "CCO")
    _link_literature(env)
    graph = ReactionGraphService(env).molecule_graph(entity.entity_id, depth=2)
    assert any(n.type is GraphNodeType.LITERATURE for n in graph.nodes)
    assert any(e.type is GraphEdgeType.SUPPORTED_BY for e in graph.edges)


def test_substrate_of_means_pattern_match_not_success(env):
    entity = _register_molecule(env, "CCO")
    graph = ReactionGraphService(env).molecule_graph(entity.entity_id)
    for edge in graph.edges:
        assert edge.type is GraphEdgeType.SUBSTRATE_OF
        assert edge.metadata["match_level"] in ("substrate_match",
                                                "transformation_match")


# -- 2. reaction root --------------------------------------------------------


def test_reaction_graph_lists_literature_and_library_molecules(env):
    _register_molecule(env, "CCO")  # a matching substrate in the library
    _link_literature(env)
    graph = ReactionGraphService(env).reaction_graph(
        "primary-alcohol-oxidation", depth=2)
    assert graph.root == "rxn:primary-alcohol-oxidation"
    assert any(n.type is GraphNodeType.LITERATURE for n in graph.nodes)
    assert any(n.type is GraphNodeType.MOLECULE for n in graph.nodes)
    assert any(e.type is GraphEdgeType.SUPPORTED_BY for e in graph.edges)
    assert any(e.type is GraphEdgeType.SUBSTRATE_OF for e in graph.edges)


def test_reaction_graph_never_creates_a_produces_edge(env):
    graph = ReactionGraphService(env).reaction_graph("primary-alcohol-oxidation")
    assert all(e.type is not GraphEdgeType.PRODUCES for e in graph.edges)
    assert any("product structure unavailable" in d for d in graph.diagnostics)


# -- 3. literature root ------------------------------------------------------


def test_literature_graph_finds_citing_reactions(env):
    entity = _register_molecule(env, "CCO")
    _link_literature(env)
    graph = ReactionGraphService(env).literature_graph("paper-A")
    assert graph.root == "lit:paper-A"
    reactions = [n for n in graph.nodes if n.type is GraphNodeType.REACTION]
    assert [n.metadata["template_id"] for n in reactions] == [
        "primary-alcohol-oxidation"]
    edge = next(e for e in graph.edges if e.type is GraphEdgeType.SUPPORTED_BY)
    assert edge.metadata["support_scope"] == "primary alcohol -> aldehyde"
    assert edge.metadata["evidence_id"]  # evidence provenance carried


# -- 4. depth is capped ------------------------------------------------------


def test_depth_is_capped(env):
    entity = _register_molecule(env, "CCO")
    graph = ReactionGraphService(env).molecule_graph(entity.entity_id, depth=99)
    assert graph.depth == MAX_DEPTH


# -- 5. determinism ----------------------------------------------------------


def test_graph_is_deterministic(env):
    entity = _register_molecule(env, "CCO")
    _link_literature(env)
    svc = ReactionGraphService(env)
    assert svc.molecule_graph(entity.entity_id, depth=2).model_dump() == \
        svc.molecule_graph(entity.entity_id, depth=2).model_dump()


# -- 6. structured errors ----------------------------------------------------


def test_unknown_molecule_is_a_structured_error(env):
    with pytest.raises(ReactionGraphError) as exc:
        ReactionGraphService(env).molecule_graph("CHEM-99999999")
    assert exc.value.code == "MOLECULE_NOT_FOUND"


def test_unknown_template_is_a_structured_error(env):
    with pytest.raises(ReactionGraphError) as exc:
        ReactionGraphService(env).reaction_graph("no-such-template")
    assert exc.value.code == "TEMPLATE_NOT_FOUND"


def test_unknown_literature_is_a_structured_error(env):
    with pytest.raises(ReactionGraphError) as exc:
        ReactionGraphService(env).literature_graph("no-such-paper")
    assert exc.value.code == "LITERATURE_NOT_FOUND"


# -- 7. API ------------------------------------------------------------------


def test_reaction_graph_api(env, tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    entity = _register_molecule(env, "CCO")
    ctx = create_context(db_path=env.path, documents_dir=tmp_path, offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        g = c.get(f"/api/reaction-graph/molecule/{entity.entity_id}").json()
        assert g["root"] == f"mol:{entity.entity_id}"
        assert g["nodes"]

        r = c.get("/api/reaction-graph/reaction/primary-alcohol-oxidation").json()
        assert r["root"] == "rxn:primary-alcohol-oxidation"

        assert c.get(
            "/api/reaction-graph/molecule/CHEM-99999999").status_code == 404
        # depth is bounded by the query parameter
        assert c.get(
            f"/api/reaction-graph/molecule/{entity.entity_id}",
            params={"depth": 99}).status_code == 422

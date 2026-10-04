"""AI context (Stage 6.1).

Fact assembly, not reasoning: a versioned, structured context built from local
data, with relations preserved, scope respected, and nothing invented. It
contains no answer and no recommendation, and a structurally broken context is
refused rather than returned.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from crea.core.errors import ContextIntegrityError
from crea.core.models.ai_context import CONTEXT_VERSION, AIContextRequest
from crea.core.models.literature import Identifiers, Paper
from crea.core.models.reaction_evidence import ReactionEvidenceType
from crea.services.ai_context import AIContextService
from crea.services.knowledge_base import KnowledgeBase
from crea.services.literature_record import LiteratureRecordService
from crea.services.reaction_evidence import ReactionEvidenceService


@pytest.fixture()
def env(tmp_path):
    kb = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    kb.initialize()
    return kb


def _molecule(kb, smiles):
    from crea.semantic.resolver import ChemicalInput, SemanticResolver
    from crea.services.identity import IdentityResolutionService

    resolver = SemanticResolver(identity_service=IdentityResolutionService(),
                                entity_registry=kb.entity_registry())
    return resolver.resolve(ChemicalInput.of("SMILES", smiles)).entity


def _link(kb, template_id="primary-alcohol-oxidation", paper_id="paper-A"):
    LiteratureRecordService(kb).create(Paper(
        paper_id=paper_id, title="An oxidation study", year=2021,
        journal="J. Synth. Chem.", identifiers=Identifiers(doi="10.1000/abc"),
        retrieved_at="2026-01-01"))
    ReactionEvidenceService(kb=kb).link_literature(
        template_id, paper_id=paper_id,
        evidence_type=ReactionEvidenceType.TRANSFORMATION_SUPPORT,
        support_scope="primary alcohol -> aldehyde")


# -- 1. molecule context -----------------------------------------------------


def test_molecule_context_carries_standard_identity(env):
    entity = _molecule(env, "CCO")
    context = AIContextService(env).build(
        AIContextRequest(molecule_ids=[entity.entity_id]))
    mol = context.molecules[0]
    assert mol.entity_id == entity.entity_id
    assert mol.standard_inchikey == entity.inchikey
    assert mol.identity_key == entity.identity_key
    assert mol.smiles == "CCO"
    assert mol.molecular_formula == "C2H6O"
    assert "Alcohol" in mol.functional_groups


def test_name_is_never_invented(env):
    entity = _molecule(env, "CCO")
    context = AIContextService(env).build(
        AIContextRequest(molecule_ids=[entity.entity_id]))
    mol = context.molecules[0]
    if not mol.names:
        assert mol.name_status == "unavailable"


# -- 2. context version ------------------------------------------------------


def test_context_version_is_explicit(env):
    entity = _molecule(env, "CCO")
    context = AIContextService(env).build(
        AIContextRequest(molecule_ids=[entity.entity_id]))
    assert context.context_version == CONTEXT_VERSION


# -- 3. reaction context preserves detail -----------------------------------


def test_reaction_context_preserves_chemistry(env):
    context = AIContextService(env).build(AIContextRequest(
        reaction_template_ids=["alkene-hydroboration-oxidation"]))
    reaction = context.reactions[0]
    assert reaction.transformation["input_features"] == ["alkene"]
    assert reaction.selectivity["regioselectivity"] == "anti-Markovnikov"
    assert reaction.reagents  # reagents kept, not summarised away
    assert reaction.limitations


# -- 4. match info -----------------------------------------------------------


def test_matching_reactions_carry_match_level(env):
    entity = _molecule(env, "CCO")
    context = AIContextService(env).build(
        AIContextRequest(molecule_ids=[entity.entity_id]))
    assert context.matches
    assert all(m.match_level in ("substrate_match", "transformation_match")
               for m in context.matches)


# -- 5. evidence -> literature relation is kept -----------------------------


def test_evidence_literature_relation_is_preserved(env):
    entity = _molecule(env, "CCO")
    _link(env)
    context = AIContextService(env).build(
        AIContextRequest(molecule_ids=[entity.entity_id]))
    linked = [e for e in context.evidence if e.literature_id]
    assert linked and linked[0].literature_id == "paper-A"
    assert any(lit.paper_id == "paper-A" for lit in context.literature)
    assert linked[0].support_scope == "primary alcohol -> aldehyde"


def test_fulltext_permission_is_independent_of_evidence(env):
    entity = _molecule(env, "CCO")
    _link(env)
    context = AIContextService(env).build(
        AIContextRequest(molecule_ids=[entity.entity_id]))
    paper = next(l for l in context.literature if l.paper_id == "paper-A")
    # no permission is established, yet the evidence stands
    assert paper.fulltext_permission == "unknown"
    assert any(e.literature_id == "paper-A" for e in context.evidence)


# -- 6. no answer / recommendation fields -----------------------------------


def test_context_has_no_answer_or_recommendation(env):
    entity = _molecule(env, "CCO")
    context = AIContextService(env).build(
        AIContextRequest(molecule_ids=[entity.entity_id]))
    fields = set(context.model_dump().keys())
    for banned in ("ai_answer", "answer", "recommendation", "reasoning",
                   "confidence", "prediction"):
        assert banned not in fields


# -- 7. deterministic ordering ----------------------------------------------


def test_ordering_is_stable(env):
    entity = _molecule(env, "CCO")
    _link(env)
    svc = AIContextService(env)
    a = svc.build(AIContextRequest(molecule_ids=[entity.entity_id]))
    b = svc.build(AIContextRequest(molecule_ids=[entity.entity_id]))
    assert [m.entity_id for m in a.molecules] == [m.entity_id for m in b.molecules]
    assert [r.template_id for r in a.reactions] == [r.template_id for r in b.reactions]
    assert [e.evidence_id for e in a.evidence] == [e.evidence_id for e in b.evidence]
    # sorted order is what makes it stable
    assert [r.template_id for r in a.reactions] == sorted(
        r.template_id for r in a.reactions)


# -- 8. scope ----------------------------------------------------------------


def test_scope_is_limited_to_matching_reactions(env):
    entity = _molecule(env, "CCO")
    context = AIContextService(env).build(
        AIContextRequest(molecule_ids=[entity.entity_id]))
    # a molecule request loads its matching reactions, not the whole library
    assert 0 < len(context.reactions) < 46


def test_graph_is_only_included_when_requested(env):
    entity = _molecule(env, "CCO")
    without = AIContextService(env).build(
        AIContextRequest(molecule_ids=[entity.entity_id]))
    with_graph = AIContextService(env).build(
        AIContextRequest(molecule_ids=[entity.entity_id], include_graph=True))
    assert without.graphs == []
    assert with_graph.graphs and with_graph.graphs[0].root


# -- 9. warnings -------------------------------------------------------------


def test_missing_molecule_is_a_warning_not_an_error(env):
    context = AIContextService(env).build(
        AIContextRequest(molecule_ids=["CHEM-99999999"]))
    assert any(w.code == "MOLECULE_NOT_FOUND" for w in context.warnings)


def test_product_unavailable_is_a_warning(env):
    context = AIContextService(env).build(
        AIContextRequest(reaction_template_ids=["primary-alcohol-oxidation"]))
    assert any(w.code == "PRODUCT_STRUCTURE_UNAVAILABLE" for w in context.warnings)


# -- 10. integrity -----------------------------------------------------------


def test_dangling_literature_reference_is_detected(env):
    entity = _molecule(env, "CCO")
    context = AIContextService(env).build(
        AIContextRequest(molecule_ids=[entity.entity_id]))
    broken = context.model_copy(deep=True)
    broken.evidence[0].literature_id = "paper-MISSING"
    problems = AIContextService(env).check_integrity(broken)
    assert any("paper-MISSING" in p for p in problems)
    # and the error type carries the problems
    error = ContextIntegrityError("broken", problems)
    assert error.problems == problems


def test_dangling_reaction_reference_is_detected(env):
    entity = _molecule(env, "CCO")
    context = AIContextService(env).build(
        AIContextRequest(molecule_ids=[entity.entity_id]))
    broken = context.model_copy(deep=True)
    broken.matches[0].template_id = "no-such-template"
    assert AIContextService(env).check_integrity(broken)


# -- 11. API -----------------------------------------------------------------


def test_ai_context_api(env, tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    entity = _molecule(env, "CCO")
    ctx = create_context(db_path=env.path, documents_dir=tmp_path, offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        body = c.post("/api/ai/context",
                      json={"molecule_ids": [entity.entity_id]}).json()
        assert body["context_version"] == CONTEXT_VERSION
        assert "context" in body and "answer" not in body
        assert body["context"]["molecules"]

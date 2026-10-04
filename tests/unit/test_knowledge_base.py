"""The KnowledgeBase facade: one door, deterministic answers.

Phase 3.11 §26-§33, §65-§71. The store tests prove bytes survive a
restart; these prove the *facade* keeps the research semantics: name
search never creates, paper/entity relations go through mentions, a
reaction's evidence is only what resolves, exports are deterministic and
carry no secrets, and one research result commits as one unit or not at
all.
"""

from __future__ import annotations

import json

import pytest

from crea.core.models.entity import ChemicalEntity, ChemicalMention
from crea.core.models.evidence import Evidence, EvidenceType, Locator
from crea.core.models.literature import EvidenceStrength, Paper
from crea.core.models.reaction import ComponentRole
from crea.core.models.reaction_knowledge import (
    Reaction,
    ReactionComponent,
    RouteStepReactionLink,
    reaction_key,
)
from crea.core.models.research import ResearchTask
from crea.core.models.route import ReactionMatchType
from crea.core.models.session import ResearchSession, WorkflowStatus
from crea.repositories.errors import ConstraintViolation, NotFound
from crea.services.knowledge_base import KnowledgeBase
from tests.unit.test_repositories_sqlite import (
    make_evidence,
    make_entity,
    make_reaction,
    make_route,
)


@pytest.fixture()
def kb(tmp_path):
    base = KnowledgeBase(tmp_path / "crea.db")
    base.initialize()
    yield base
    base.close()


# -- queries that cannot invent (§30/§31) -----------------------------------


def test_name_search_finds_every_entity_and_never_registers_one(kb):
    kb.save_entity(make_entity(1, name="Phenylacetylene"))
    kb.save_entity(make_entity(2, name="phenylacetylene"))
    found = kb.search_entities_by_name("phenylacetylene")
    assert [e.entity_id for e in found] == ["CHEM-00000001", "CHEM-00000002"]
    assert kb.search_entities_by_name("nothing-here") == []
    assert len(kb.list_entities()) == 2
    with pytest.raises(NotFound):
        kb.get_entity("CHEM-99999999")


def test_search_reactions_requires_a_filter(kb):
    kb.save_evidence(make_evidence())
    kb.save_reaction(make_reaction("RXN-1"))
    with pytest.raises(ValueError) as excinfo:
        kb.search_reactions()
    assert "INVALID_QUERY" in str(excinfo.value)
    found = kb.search_reactions(paper_id="paper-a")
    assert [r.reaction_id for r in found] == ["RXN-1"]
    assert kb.search_reactions(paper_id="nobody") == []


# -- Paper -> Mention -> Entity (§65) ---------------------------------------


def test_papers_for_entity_goes_through_the_mention_layer(kb):
    kb.save_entity(make_entity(1, name="A"))
    kb.save_paper(Paper(paper_id="paper-a", retrieved_at="t"))
    mention = ChemicalMention(mention_id="m-1", text="compound 7",
                              source_id="paper-a", resolution_status="resolved",
                              entity_id="CHEM-00000001")
    kb.save_mention(mention)
    kb.link_paper_mention("paper-a", "m-1")

    papers = kb.get_papers_for_entity("CHEM-00000001")
    assert [p.paper_id for p in papers] == ["paper-a"]
    # a paper that never mentioned the entity does not appear -- no
    # shortcut around the mention layer
    kb.save_paper(Paper(paper_id="paper-b", retrieved_at="t"))
    assert [p.paper_id for p in kb.get_papers_for_entity("CHEM-00000001")] == ["paper-a"]


def test_evidence_for_reaction_returns_only_what_resolves(kb):
    evidence = make_evidence("real quote")
    kb.save_evidence(evidence)
    kb.save_entity(make_entity(1))
    kb.save_reaction(make_reaction("RXN-1", evidence_refs=[evidence.evidence_id]))
    assert [e.evidence_id for e in kb.get_evidence_for_reaction("RXN-1")] == [
        evidence.evidence_id]
    # a reaction with no citations at all gets an empty list -- never
    # borrowed support from elsewhere
    kb.save_reaction(make_reaction(
        "RXN-2", key=reaction_key(["e:other-reactant"], ["e:other-product"])))
    assert kb.get_evidence_for_reaction("RXN-2") == []


def test_routes_for_entity_says_the_role_and_keeps_the_context(kb):
    entity = make_entity(1)
    kb.save_entity(entity)
    kb.save_evidence(make_evidence())
    mention = ChemicalMention(mention_id="m-1", text="x", source_id="paper-a",
                              resolution_status="resolved",
                              entity_id=entity.entity_id)
    reaction = make_reaction(
        "RXN-1",
        components=[ReactionComponent(role=ComponentRole.REACTANT, name="x",
                                      mention=mention,
                                      entity_id=entity.entity_id,
                                      canonical_smiles=entity.canonical_smiles)])
    kb.save_reaction(reaction)
    route = make_route()
    # make_route's step uses C#Cc1ccccc1 on both sides; the entity's
    # canonical smiles is what identity comparison reads (§69)
    for molecule in route.steps[0].reactants + route.steps[0].products:
        molecule.canonical_smiles = entity.canonical_smiles
    kb.save_route(route)
    link = RouteStepReactionLink(
        link_id="link-1", step_id="route-1-s1", reaction_id="RXN-1",
        match_type=ReactionMatchType.EXACT, screening_only=False)
    kb.save_route_reaction_link(link)

    hits = kb.get_routes_for_entity(entity.entity_id)
    assert {"route_id": "route-1", "relation_role": "reactant",
            "step_id": "route-1-s1"} in hits
    assert {"route_id": "route-1", "relation_role": "product",
            "step_id": "route-1-s1"} in hits
    for hit in hits:
        assert set(hit) == {"route_id", "relation_role", "step_id"}

    # and only stored links are followed (§68)
    assert [(r, l.link_id) for r, l in kb.get_routes_for_reaction("RXN-1")] == [
        ("route-1", "link-1")]
    assert kb.get_routes_for_reaction("RXN-nope") == []


# -- one research result as one unit (§52/§79) -------------------------------


def test_persist_research_result_commits_everything_together(kb):
    entity = make_entity(1, name="A")
    evidence = make_evidence()
    mention = ChemicalMention(mention_id="m-1", text="compound 7",
                              source_id="paper-a", resolution_status="resolved",
                              entity_id=entity.entity_id)
    reaction = make_reaction(
        "RXN-1", evidence_refs=[evidence.evidence_id],
        components=[ReactionComponent(role=ComponentRole.REACTANT, name="A",
                                      mention=mention,
                                      entity_id=entity.entity_id)])
    route = make_route()
    link = RouteStepReactionLink(link_id="link-1", step_id="route-1-s1",
                                 reaction_id="RXN-1",
                                 match_type=ReactionMatchType.EXACT,
                                 screening_only=False)
    session = ResearchSession(
        session_id="session-1",
        task=ResearchTask(task_id="t1", question="q"),
        status=WorkflowStatus.COMPLETED,
        chemical_entity_ids=[entity.entity_id],
        chemical_mention_ids=["m-1"],
        reaction_ids=["RXN-1"],
        evidence_ids=[evidence.evidence_id],
        route_ids=["route-1"],
        sources=[],
    )
    kb.persist_research_result(
        session=session, entities=[entity], mentions=[mention],
        papers=[Paper(paper_id="paper-a", retrieved_at="t")],
        evidence=[evidence], reactions=[reaction], routes=[route],
        links=[link])
    # the paper relation was written because the reaction names its paper
    assert [p.paper_id for p in kb.get_papers_for_entity(entity.entity_id)] == [
        "paper-a"]
    assert kb.get_session("session-1").reaction_ids == ["RXN-1"]


def test_a_failed_commit_leaves_nothing_behind(kb):
    entity = make_entity(1)
    mention = ChemicalMention(mention_id="m-1", text="x", source_id="paper-a",
                              resolution_status="resolved",
                              entity_id=entity.entity_id)
    good = make_reaction(
        "RXN-good",
        key=reaction_key(["e:good-r"], ["e:good-p"]),
        components=[ReactionComponent(role=ComponentRole.REACTANT, name="x",
                                      mention=mention,
                                      entity_id=entity.entity_id)])
    bad = make_reaction("RXN-bad", key=reaction_key(["e:bad-r"], ["e:bad-p"]),
                        evidence_refs=["EVIDENCE-nope"])
    session = ResearchSession(
        session_id="session-1",
        task=ResearchTask(task_id="t1", question="q"),
        reaction_ids=["RXN-good", "RXN-bad"],
        chemical_entity_ids=[entity.entity_id],
    )
    with pytest.raises(ConstraintViolation):
        kb.persist_research_result(
            session=session, entities=[entity], mentions=[mention],
            evidence=[], reactions=[good, bad])
    assert kb.store.get_reaction("RXN-good") is None, "no half a result (§52)"
    assert kb.store.get_reaction("RXN-bad") is None
    assert kb.store.get_entity("CHEM-00000001") is None
    assert kb.store.get_session("session-1") is None


# -- export (§70/§71/§109) --------------------------------------------------


def test_export_is_deterministic_and_carries_only_knowledge(kb, tmp_path):
    kb.save_entity(make_entity(1, name="A"))
    kb.save_evidence(make_evidence())
    kb.save_reaction(make_reaction("RXN-1", evidence_refs=[make_evidence().evidence_id]))
    first = kb.export(tmp_path / "knowledge.json")
    text_a = first.read_text(encoding="utf-8")
    second = kb.export(tmp_path / "knowledge-2.json")
    text_b = second.read_text(encoding="utf-8")
    assert text_a == text_b, "the same knowledge exports the same bytes"

    payload = json.loads(text_a)
    assert payload["format"] == "crea-knowledge-export-v1"
    assert payload["schema_version"] == 1
    for collection in ("entities", "mentions", "papers", "evidence",
                       "reactions", "routes", "sessions"):
        assert collection in payload
    lowered = text_a.lower()
    for secret_marker in ("api_key", "apikey", "authorization", "bearer ",
                          "password", "secret"):
        assert secret_marker not in lowered, "an export is knowledge, not credentials"


# -- reuse across bases (§41) ------------------------------------------------


def test_registry_over_the_store_is_the_reuse_mechanism(kb, tmp_path):
    registry = kb.entity_registry()
    inchi = "InChI=1S/C8H8/c1-2-8-6-4-3-5-7-8/h1,3-7H,2H2"
    from crea.core.models.identity import (
        ChemicalIdentityCandidate,
        ChemicalIdentityResolution,
        IdentityBasis,
        StructureInput,
        StructureInputKind,
        StructureResolutionStatus,
    )

    candidate = ChemicalIdentityCandidate(
        candidate_id="c1", source="PUBCHEM", source_id="733",
        input_name="phenylacetylene", canonical_smiles="C#Cc1ccccc1",
        inchi=inchi, identity_basis=IdentityBasis.EXACT_STRUCTURE)
    resolution = ChemicalIdentityResolution(
        status=StructureResolutionStatus.RESOLVED,
        input=StructureInput(kind=StructureInputKind.NAME,
                             value="phenylacetylene"),
        candidates=[candidate], selected=candidate)
    first = registry.register_resolution(resolution)
    kb.close()

    # a whole new process over the same file
    reopened = KnowledgeBase(tmp_path / "crea.db")
    reopened.initialize()
    again = reopened.entity_registry().register_resolution(resolution)
    assert again.entity_id == first.entity_id == "CHEM-00000001"
    assert len(reopened.list_entities()) == 1
    reopened.close()

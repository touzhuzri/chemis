"""Research intelligence: coverage, gaps, plan, synthesis.

Phase 3.12. What is under test is the deterministic reading of stored
knowledge: states that follow from named objects, gaps phrased for
humans (never "置信度 42%"), a plan that follows from the gaps (never
from a model), and a synthesis that keeps every value's citation and
never averages a conflict away (§22-§24).
"""

from __future__ import annotations

import pytest

from crea.core.models.knowledge import (
    KnowledgeFacet,
    KnowledgeState,
    ResearchPlanAction,
)
from crea.core.models.reaction import (
    ComponentRole,
    FieldStatus,
    Quantity,
    ReactionConditions,
    ReactionRecord,
)
from crea.core.models.reaction_knowledge import (
    ReactionComponent,
    ReactionStatus,
    RouteStepReactionLink,
    reaction_key,
)
from crea.core.models.route import ReactionMatchType
from crea.core.models.research import ResearchTask
from crea.core.models.session import ResearchSession
from crea.repositories.errors import RepositoryError
from crea.services.knowledge_base import KnowledgeBase
from crea.services.research_intelligence import (
    assess_coverage,
    build_research_plan,
    find_gaps,
    synthesize_fields,
    wants_latest_literature,
)
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


def _record(record_id: str, *, temperature=None, yield_value=None) -> ReactionRecord:
    return ReactionRecord(
        reaction_id=record_id,
        conditions=ReactionConditions(
            temperature=temperature or Quantity(),
            yield_value=yield_value or Quantity()),
    )


def _linked_reaction(reaction_id: str, entity, *, key: str,
                     paper_id: str = "paper-a", doi: str = "",
                     evidence_refs=(),
                     status=ReactionStatus.EVIDENCE_BACKED) -> object:
    """A reaction that actually contains the entity -- otherwise an
    entity-scoped search finds nothing and the fixture lies."""
    reaction = make_reaction(
        reaction_id, key=key, evidence_refs=list(evidence_refs),
        components=[ReactionComponent(
            role=ComponentRole.REACTANT, name="x", entity_id=entity.entity_id,
            identity_key=entity.identity_key,
            canonical_smiles=entity.canonical_smiles)])
    reaction.paper_id = paper_id
    reaction.doi = doi
    reaction.status = status
    return reaction


# -- coverage states (§8/§12) -----------------------------------------------


def test_an_empty_knowledge_base_is_all_unknown_with_readable_gaps(kb):
    coverage = assess_coverage(kb, entity_id="CHEM-00000001", target="X")
    assert coverage.state_of(KnowledgeFacet.ENTITY) is KnowledgeState.UNKNOWN
    assert coverage.state_of(KnowledgeFacet.REACTION) is KnowledgeState.UNKNOWN
    gaps = find_gaps(coverage)
    assert any(g.facet is KnowledgeFacet.REACTION for g in gaps)
    assert any("尚未建立" in g.reason for g in gaps)


def test_reactions_without_experimental_support_are_partial_and_say_so(kb):
    entity = make_entity(1)
    kb.save_entity(entity)
    evidence = make_evidence()
    kb.save_evidence(evidence)
    kb.save_reaction(_linked_reaction(
        "RXN-1", entity, key=reaction_key(["e:r"], ["e:p"]),
        evidence_refs=[evidence.evidence_id]))

    coverage = assess_coverage(kb, entity_id=entity.entity_id, target="X")
    assert coverage.state_of(KnowledgeFacet.ENTITY) is KnowledgeState.KNOWN
    assert coverage.state_of(KnowledgeFacet.REACTION) is KnowledgeState.KNOWN
    assert coverage.state_of(
        KnowledgeFacet.EXPERIMENTAL_EVIDENCE) is KnowledgeState.PARTIALLY_KNOWN

    gaps = find_gaps(coverage)
    assert any(g.facet is KnowledgeFacet.EXPERIMENTAL_EVIDENCE for g in gaps)
    assert any("引用证据" in g.reason and "没有实验级" in g.reason for g in gaps)


def test_structures_known_but_no_conditions_is_the_named_sentence(kb):
    """§12's example sentence, verbatim."""
    entity = make_entity(1)
    kb.save_entity(entity)
    kb.save_reaction(_linked_reaction(
        "RXN-1", entity, key=reaction_key(["e:r"], ["e:p"])))

    coverage = assess_coverage(kb, entity_id=entity.entity_id, target="X")
    gaps = find_gaps(coverage)
    assert any(g.reason == "已有反应结构信息，但尚未找到可验证的实验条件证据。"
               for g in gaps)


def test_an_experimentally_supported_reaction_makes_evidence_known(kb):
    entity = make_entity(1)
    kb.save_entity(entity)
    kb.save_evidence(make_evidence())
    kb.save_reaction(_linked_reaction(
        "RXN-1", entity, key=reaction_key(["e:r"], ["e:p"]),
        status=ReactionStatus.EXPERIMENTALLY_SUPPORTED))

    coverage = assess_coverage(kb, entity_id=entity.entity_id, target="X")
    assert coverage.state_of(
        KnowledgeFacet.EXPERIMENTAL_EVIDENCE) is KnowledgeState.KNOWN
    assert not any(g.facet is KnowledgeFacet.EXPERIMENTAL_EVIDENCE
                   for g in find_gaps(coverage))


def test_route_evidence_distinguishes_exact_links_from_screening_only(kb):
    entity = make_entity(1)
    kb.save_entity(entity)
    kb.save_evidence(make_evidence())
    kb.save_reaction(_linked_reaction(
        "RXN-1", entity, key=reaction_key(["e:r"], ["e:p"]),
        status=ReactionStatus.EXPERIMENTALLY_SUPPORTED))
    route = make_route()
    kb.save_route(route)
    kb.save_route_reaction_link(RouteStepReactionLink(
        link_id="link-1", step_id="route-1-s1", reaction_id="RXN-1",
        match_type=ReactionMatchType.PARTIAL, screening_only=True))
    coverage = assess_coverage(kb, entity_id=entity.entity_id, target="X")
    assert coverage.state_of(
        KnowledgeFacet.ROUTE_EVIDENCE) is KnowledgeState.PARTIALLY_KNOWN

    kb.save_route_reaction_link(RouteStepReactionLink(
        link_id="link-2", step_id="route-1-s1", reaction_id="RXN-1",
        match_type=ReactionMatchType.EXACT, screening_only=False))
    coverage = assess_coverage(kb, entity_id=entity.entity_id, target="X")
    assert coverage.state_of(KnowledgeFacet.ROUTE_EVIDENCE) is KnowledgeState.KNOWN


# -- plan (§17-§18/§39/§40) -------------------------------------------------


def test_the_latest_markers_are_spelled_not_interpreted():
    assert wants_latest_literature("查找最新的合成文献")
    assert wants_latest_literature("recent advances")
    assert wants_latest_literature("2026 年的论文")
    assert not wants_latest_literature("研究 phenylacetylene 的合成方法")
    assert not wants_latest_literature("研究 1,4-diphenyl-1,3-diyne")


def test_a_covered_question_with_no_latest_request_plans_no_search(kb):
    entity = make_entity(1)
    kb.save_entity(entity)
    kb.save_evidence(make_evidence())
    kb.save_reaction(_linked_reaction(
        "RXN-1", entity, key=reaction_key(["e:r"], ["e:p"]),
        status=ReactionStatus.EXPERIMENTALLY_SUPPORTED))
    route = make_route()
    kb.save_route(route)
    kb.save_route_reaction_link(RouteStepReactionLink(
        link_id="link-1", step_id="route-1-s1", reaction_id="RXN-1",
        match_type=ReactionMatchType.EXACT, screening_only=False))

    coverage = assess_coverage(kb, entity_id=entity.entity_id, target="X")
    gaps = find_gaps(coverage)
    plan = build_research_plan(coverage, gaps, question="研究合成方法")
    assert not plan.has(ResearchPlanAction.SEARCH_LITERATURE), (
        "§39: 已有知识覆盖且未要求最新文献时不重复检索")
    assert plan.has(ResearchPlanAction.REPORT)

    # ... unless the user asks for the latest (§40)
    plan = build_research_plan(coverage, gaps, question="查找最新的文献")
    assert plan.has(ResearchPlanAction.SEARCH_LITERATURE)
    assert plan.wants_latest


def test_an_experimental_gap_plans_document_and_extraction_work(kb):
    entity = make_entity(1)
    kb.save_entity(entity)
    kb.save_reaction(_linked_reaction(
        "RXN-1", entity, key=reaction_key(["e:r"], ["e:p"])))
    coverage = assess_coverage(kb, entity_id=entity.entity_id, target="X")
    plan = build_research_plan(coverage, find_gaps(coverage), question="q")
    assert plan.has(ResearchPlanAction.SEARCH_LITERATURE)
    assert plan.has(ResearchPlanAction.INSPECT_DOCUMENT)
    assert plan.has(ResearchPlanAction.EXTRACT_REACTION_EVIDENCE)


def test_an_empty_knowledge_base_plans_the_full_pipeline(kb):
    coverage = assess_coverage(kb, entity_id=None, target="X")
    plan = build_research_plan(coverage, find_gaps(coverage), question="q")
    for action in (ResearchPlanAction.SEARCH_LITERATURE,
                   ResearchPlanAction.INSPECT_DOCUMENT,
                   ResearchPlanAction.RETROSYNTHESIZE):
        assert plan.has(action)


def test_a_supplied_document_is_always_planned(kb):
    coverage = assess_coverage(kb, entity_id=None, target="X")
    plan = build_research_plan(coverage, find_gaps(coverage), question="q",
                               has_document=True)
    assert plan.has(ResearchPlanAction.INSPECT_DOCUMENT)


# -- synthesis / conflicts (§22-§24) ----------------------------------------


def test_two_papers_reporting_different_conditions_is_a_conflict_not_an_error(kb):
    entity = make_entity(1)
    kb.save_entity(entity)
    evidence_a = make_evidence("80 C, 82%", source_id="paper-a")
    evidence_b = make_evidence("60 C, 71%", source_id="paper-b")
    kb.save_evidence(evidence_a)
    kb.save_evidence(evidence_b)

    reaction_a = _linked_reaction(
        "RXN-a", entity, key=reaction_key(["e:r"], ["e:p"]),
        paper_id="paper-a", status=ReactionStatus.EXPERIMENTALLY_SUPPORTED)
    reaction_b = _linked_reaction(
        "RXN-b", entity, key=reaction_key(["e:r"], ["e:p"]),
        paper_id="paper-b", doi="10.0000/b",
        status=ReactionStatus.EXPERIMENTALLY_SUPPORTED)
    kb.save_reaction(reaction_a)
    kb.save_reaction(reaction_b)

    record_a = _record("rec-a", temperature=Quantity(
        value=80, raw_value="80", unit="C", raw_unit="C",
        status=FieldStatus.EXPLICIT, evidence_refs=[evidence_a.evidence_id]))
    record_b = _record("rec-b", temperature=Quantity(
        value=60, raw_value="60", unit="C", raw_unit="C",
        status=FieldStatus.EXPLICIT, evidence_refs=[evidence_b.evidence_id]))
    kb.save_reaction_record(record_a, reaction_id="RXN-a")
    kb.save_reaction_record(record_b, reaction_id="RXN-b")

    synthesis = synthesize_fields(kb, entity_id=entity.entity_id)
    conflicts = [c for c in synthesis.conflicts if c.field == "temperature"]
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.kind == "conflict", "CONFLICT, not INVALID (§23)"
    values = {(o.value, o.evidence_id) for o in conflict.observations}
    assert (80.0, evidence_a.evidence_id) in {(float(v), e) for v, e in values} \
        or (80, evidence_a.evidence_id) in values
    cited = {o.evidence_id for o in conflict.observations}
    assert evidence_a.evidence_id in cited and evidence_b.evidence_id in cited, (
        "每个冲突值保留其证据出处（§24）")
    # no averaging, no winner: both raw values survive untouched
    raws = {o.raw_value for o in conflict.observations}
    assert raws == {"80", "60"}


def test_the_same_value_from_two_papers_is_agreement_not_conflict(kb):
    entity = make_entity(1)
    kb.save_entity(entity)
    kb.save_evidence(make_evidence())
    reaction_a = _linked_reaction(
        "RXN-a", entity, key=reaction_key(["e:r"], ["e:p"]),
        paper_id="paper-a")
    reaction_b = _linked_reaction(
        "RXN-b", entity, key=reaction_key(["e:r"], ["e:p"]),
        paper_id="paper-b", doi="10.0000/b")
    kb.save_reaction(reaction_a)
    kb.save_reaction(reaction_b)
    same = Quantity(value=80, raw_value="80", unit="C", raw_unit="C",
                    status=FieldStatus.EXPLICIT)
    kb.save_reaction_record(_record("rec-a", temperature=same),
                            reaction_id="RXN-a")
    kb.save_reaction_record(_record("rec-b", temperature=same),
                            reaction_id="RXN-b")

    synthesis = synthesize_fields(kb, entity_id=entity.entity_id)
    assert [c for c in synthesis.conflicts if c.field == "temperature"] == []
    temperatures = [o for o in synthesis.observations if o.field == "temperature"]
    assert len(temperatures) == 2, "two sources, two observations"


def test_a_reaction_with_no_record_contributes_no_conditions(kb):
    entity = make_entity(1)
    kb.save_entity(entity)
    kb.save_reaction(_linked_reaction(
        "RXN-1", entity, key=reaction_key(["e:r"], ["e:p"])))
    synthesis = synthesize_fields(kb, entity_id=entity.entity_id)
    assert synthesis.observations == [], "没有记录就没有条件（§27 永不猜）"
    assert synthesis.conflicts == []


def test_synthesize_needs_a_filter(kb):
    with pytest.raises(RepositoryError):
        synthesize_fields(kb)

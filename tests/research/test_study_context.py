"""M1.5 -- the study context: one study's persistent research memory.

What is pinned here: study isolation (the blind-study boundary), the
fact/hypothesis separation with no silent promotion, evidence referenced
with its type and provenance intact (never converted), identity ambiguity
saved and resumed through the USER's selection, unresolved issues and
decisions surviving into the next session, and additive schema compatibility
with a pre-M1.5 database.
"""

from __future__ import annotations

import pathlib

import pytest

from crea.core.errors import ResearchStudyError
from crea.core.models.evidence import Evidence, EvidenceType, Locator
from crea.core.models.literature import EvidenceStrength
from crea.core.models.study_context import (
    FactVerification,
    HypothesisStatus,
    IdentityCandidate,
    IssueStatus,
    StudyEvidenceKind,
    StudySessionSummary,
)
from crea.core.provenance import Provenance
from crea.services.knowledge_base import KnowledgeBase
from crea.services.research_study import ResearchStudyService
from crea.services.study_context import StudyContextService


@pytest.fixture()
def env(tmp_path):
    kb = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    kb.initialize()
    return kb


@pytest.fixture()
def studies(env):
    return ResearchStudyService(env)


@pytest.fixture()
def ctx(env):
    return StudyContextService(env)


@pytest.fixture()
def study_a(studies):
    return studies.create(title="A", question="目标分子 A 的合成研究")


@pytest.fixture()
def study_b(studies):
    return studies.create(title="B", question="目标分子 B 的合成研究")


def _evidence(env, *, source="user_provided", quote="80 °C, 2 h"):
    record = Evidence.build(
        evidence_type=EvidenceType.EXPERIMENTAL,
        strength=EvidenceStrength.PRIMARY_DOCUMENT_EVIDENCE,
        source=source, source_id="doc-1", raw_quote=quote,
        locator=Locator(locator_type="document_region", document_id="doc-1",
                        section="Experimental"),
        retrieved_at="2026-01-01T00:00:00+00:00",
        provenance=Provenance(tool="document_reader", source=source))
    return env.save_evidence(record)


# ---------------------------------------------------------------------------
# 1-2. create / read, strict isolation
# ---------------------------------------------------------------------------


def test_context_creates_and_reads_back_within_one_study(ctx, study_a):
    opened = ctx.open(study_a.study_id, goal="研究 A 的合成路线",
                      constraints=["不使用钯催化剂"])
    assert opened.goal == "研究 A 的合成路线"
    assert opened.constraints == ["不使用钯催化剂"]

    ctx.add_fact(study_a.study_id, fact_type="identity_resolved",
                 payload={"smiles": "CCO"}, source="resolve_chemical_identity",
                 verification=FactVerification.TOOL_VERIFIED)
    reloaded = ctx.get_context(study_a.study_id)
    assert [f.fact_type for f in reloaded.facts] == ["identity_resolved"]
    assert reloaded.facts[0].verification is FactVerification.TOOL_VERIFIED


def test_two_studies_never_see_each_others_context(ctx, study_a, study_b):
    ctx.add_fact(study_a.study_id, fact_type="identity_resolved",
                 source="resolve_chemical_identity",
                 verification=FactVerification.TOOL_VERIFIED)
    ctx.add_hypothesis(study_a.study_id, "A 可能经过内酯化")
    ctx.record_decision(study_a.study_id, "先解析身份", reason="结构未知")
    ctx.record_unresolved_issue(study_a.study_id, "缺少压力数据",
                                kind="experimental_conditions")

    context_b = ctx.get_context(study_b.study_id)
    assert context_b.facts == []
    assert context_b.hypotheses == []
    assert context_b.decisions == []
    assert context_b.issues == []
    # And narrowing queries are still scoped: no method answers across studies.
    assert ctx.facts(study_b.study_id) == []
    assert ctx.hypotheses(study_b.study_id) == []
    assert ctx.decisions(study_b.study_id) == []
    assert ctx.open_issues(study_b.study_id) == []


def test_records_of_an_unknown_study_refuse_to_exist(ctx):
    with pytest.raises(ResearchStudyError) as excinfo:
        ctx.get_context("study-ghost")
    assert excinfo.value.code == "STUDY_NOT_FOUND"


# ---------------------------------------------------------------------------
# 3. cross-session reads
# ---------------------------------------------------------------------------


def test_a_new_service_instance_reads_what_the_first_session_recorded(
        tmp_path, study_a):
    db = pathlib.Path(tmp_path) / "crea.db"
    first = StudyContextService(KnowledgeBase(db))
    first.add_fact(study_a.study_id, fact_type="routes_held",
                   source="retrosynthesize",
                   verification=FactVerification.TOOL_VERIFIED,
                   session_id="sess-1")
    first.record_unresolved_issue(study_a.study_id, "路线未验证",
                                  kind="route", session_id="sess-1")

    second = StudyContextService(KnowledgeBase(db))
    context = second.get_context(study_a.study_id)
    assert [f.session_id for f in context.facts] == ["sess-1"]
    assert [i.content for i in context.issues] == ["路线未验证"]


# ---------------------------------------------------------------------------
# 4-5. facts vs hypotheses; no silent promotion
# ---------------------------------------------------------------------------


def test_facts_and_hypotheses_are_separate_queries(ctx, study_a):
    ctx.add_fact(study_a.study_id, fact_type="identity_resolved",
                 source="resolve_chemical_identity",
                 verification=FactVerification.TOOL_VERIFIED)
    ctx.add_hypothesis(study_a.study_id, "羧酸可能先活化")

    assert [f.fact_type for f in ctx.facts(study_a.study_id)] == [
        "identity_resolved"]
    assert [h.content for h in ctx.hypotheses(study_a.study_id)] == [
        "羧酸可能先活化"]
    assert ctx.hypotheses(study_a.study_id, status=HypothesisStatus.PROPOSED)


def test_a_hypothesis_starts_proposed_and_cannot_self_promote(ctx, study_a):
    hypothesis = ctx.add_hypothesis(study_a.study_id, "A 经由苯炔中间体")
    assert hypothesis.status is HypothesisStatus.PROPOSED

    with pytest.raises(ResearchStudyError) as excinfo:
        ctx.update_hypothesis(study_a.study_id, hypothesis.hypothesis_id,
                              HypothesisStatus.SUPPORTED)
    assert excinfo.value.code == "HYPOTHESIS_EVIDENCE_REQUIRED"

    # With named evidence, the move is explicit and recorded.
    moved = ctx.update_hypothesis(
        study_a.study_id, hypothesis.hypothesis_id,
        HypothesisStatus.SUPPORTED, supporting_evidence_ids=["ev-1"])
    assert moved.status is HypothesisStatus.SUPPORTED
    assert moved.supporting_evidence_ids == ["ev-1"]


def test_repeating_a_hypothesis_does_not_change_it(ctx, study_a):
    first = ctx.add_hypothesis(study_a.study_id, "A 经由苯炔中间体")
    second = ctx.add_hypothesis(study_a.study_id, "A 经由苯炔中间体")
    assert first.hypothesis_id != second.hypothesis_id
    assert all(h.status is HypothesisStatus.PROPOSED
               for h in ctx.hypotheses(study_a.study_id))


def test_a_fact_records_how_it_was_established(ctx, study_a):
    fact = ctx.add_fact(
        study_a.study_id, fact_type="identity_resolved",
        payload={"smiles": "CCO"}, source="resolve_chemical_identity",
        verification=FactVerification.TOOL_VERIFIED,
        provenance={"tool": "resolve_chemical_identity"},
        evidence_ids=["ev-9"], session_id="sess-1")
    reloaded = ctx.facts(study_a.study_id, fact_type="identity_resolved")[0]
    assert reloaded.verification is FactVerification.TOOL_VERIFIED
    assert reloaded.source == "resolve_chemical_identity"
    assert reloaded.provenance == {"tool": "resolve_chemical_identity"}
    assert reloaded.evidence_ids == ["ev-9"]

    # Forgetting the verification records an UNVERIFIED fact, never a
    # confirmed one, and a tool-verified fact must name its tool.
    lazy = ctx.add_fact(study_a.study_id, fact_type="route_held")
    assert lazy.verification is FactVerification.UNVERIFIED
    with pytest.raises(ResearchStudyError):
        ctx.add_fact(study_a.study_id, fact_type="x",
                     verification=FactVerification.TOOL_VERIFIED)


# ---------------------------------------------------------------------------
# 6-7. evidence references keep type and provenance; no conversion
# ---------------------------------------------------------------------------


def test_an_evidence_reference_keeps_source_type_and_provenance(
        ctx, env, study_a):
    record = _evidence(env)
    ref = ctx.add_evidence_reference(study_a.study_id, record.evidence_id,
                                     kind=StudyEvidenceKind.LITERATURE,
                                     note="实验条件出处")
    assert ref.evidence_id == record.evidence_id
    assert ref.kind is StudyEvidenceKind.LITERATURE

    stored = env.get_evidence(record.evidence_id)
    assert stored.evidence_type is EvidenceType.EXPERIMENTAL
    assert stored.source == "user_provided"
    assert stored.provenance.tool == "document_reader"
    assert [r.evidence_id for r in
            ctx.evidence_refs(study_a.study_id)] == [record.evidence_id]


def test_a_literature_record_never_becomes_reaction_evidence(ctx, env, study_a):
    record = _evidence(env, quote="the reaction was heated at 80 °C")
    ctx.add_evidence_reference(study_a.study_id, record.evidence_id,
                               kind=StudyEvidenceKind.LITERATURE)

    # The stored evidence is untouched and still typed as it was built.
    stored = env.get_evidence(record.evidence_id)
    assert stored.evidence_type is EvidenceType.EXPERIMENTAL
    assert not hasattr(stored, "reaction_template_id")
    # The context reference classifies; it never rewrites. Reading the
    # reference back gives the caller's classification and the same id.
    refs = ctx.evidence_refs(study_a.study_id)
    assert refs[0].kind is StudyEvidenceKind.LITERATURE
    assert ctx.evidence_refs(study_a.study_id,
                             kind=StudyEvidenceKind.REACTION) == []


def test_referencing_evidence_that_does_not_exist_is_refused(ctx, study_a):
    with pytest.raises(ResearchStudyError) as excinfo:
        ctx.add_evidence_reference(study_a.study_id, "ev-ghost",
                                   kind=StudyEvidenceKind.LITERATURE)
    assert excinfo.value.code == "EVIDENCE_NOT_FOUND"


# ---------------------------------------------------------------------------
# 8-9. identity ambiguity: candidates saved, user selection restores
# ---------------------------------------------------------------------------


def test_identity_candidates_are_saved_open_and_selected_by_the_user(
        ctx, study_a):
    record = ctx.save_identity_candidates(
        study_a.study_id, question="“苯乙炔”对应哪个结构？",
        candidates=[
            IdentityCandidate(kind="SMILES", value="C#Cc1ccccc1",
                              label="苯乙炔"),
            IdentityCandidate(kind="NAME", value="(ethynyl)benzene",
                              label="(ethynyl)benzene"),
        ], session_id="sess-1")
    assert record.selected_index is None
    with pytest.raises(ResearchStudyError) as excinfo:
        ctx.identity_resolve_arguments(study_a.study_id, record.record_id)
    assert excinfo.value.code == "IDENTITY_SELECTION_REQUIRED"

    # The selection API demands the user's own words; it never writes them.
    with pytest.raises(ResearchStudyError):
        ctx.select_identity_candidate(study_a.study_id, record.record_id, 0,
                                      user_selection_statement="   ")
    with pytest.raises(ResearchStudyError):
        ctx.select_identity_candidate(study_a.study_id, record.record_id, 7,
                                      user_selection_statement="选第一个")

    chosen = ctx.select_identity_candidate(
        study_a.study_id, record.record_id, 0,
        user_selection_statement="用户说：选第一个带三键的结构")
    assert chosen.selected_by == "user"
    assert chosen.user_selection_statement == "用户说：选第一个带三键的结构"

    arguments = ctx.identity_resolve_arguments(study_a.study_id,
                                               record.record_id)
    assert arguments == {
        "kind": "SMILES", "value": "C#Cc1ccccc1",
        "selected_candidate_index": 0,
        "user_selection_statement": "用户说：选第一个带三键的结构",
    }


def test_an_unselected_record_survives_into_the_next_session(
        tmp_path, study_a):
    db = pathlib.Path(tmp_path) / "crea.db"
    StudyContextService(KnowledgeBase(db)).save_identity_candidates(
        study_a.study_id, question="哪个候选？",
        candidates=[IdentityCandidate(kind="NAME", value="甲苯", label="甲苯")],
        session_id="sess-1")
    later = StudyContextService(KnowledgeBase(db))
    records = later.identity_records(study_a.study_id)
    assert len(records) == 1
    assert records[0].question == "哪个候选？"
    assert records[0].selected_index is None


# ---------------------------------------------------------------------------
# 10-11. unresolved issues and decision history across sessions
# ---------------------------------------------------------------------------


def test_unresolved_issues_survive_and_resolve_with_a_reason(ctx, study_a):
    issue = ctx.record_unresolved_issue(study_a.study_id, "缺少 13C 数据",
                                        kind="experimental_conditions",
                                        session_id="sess-1")
    # The same issue from a second run is deduplicated, not doubled.
    again = ctx.record_unresolved_issue(study_a.study_id, "缺少 13C 数据",
                                        kind="experimental_conditions",
                                        session_id="sess-2")
    assert again.issue_id == issue.issue_id
    assert len(ctx.open_issues(study_a.study_id)) == 1

    resolved = ctx.resolve_unresolved_issue(study_a.study_id, issue.issue_id,
                                            "用户补充了谱图",
                                            session_id="sess-2")
    assert resolved.status is IssueStatus.RESOLVED
    assert resolved.resolution == "用户补充了谱图"
    assert ctx.open_issues(study_a.study_id) == []
    kept = ctx.get_context(study_a.study_id, include_resolved_issues=True)
    assert [i.issue_id for i in kept.issues] == [issue.issue_id]


def test_decision_history_is_append_only(ctx, study_a):
    first = ctx.record_decision(study_a.study_id, "先解析身份",
                                reason="结构未知", session_id="sess-1",
                                observation_kind="needs_input")
    second = ctx.record_decision(study_a.study_id, "改用替代检索",
                                 reason="主数据源不可用", session_id="sess-1",
                                 observation_kind="unavailable",
                                 plan_change_kinds=["add_step"])
    history = ctx.decisions(study_a.study_id)
    assert [d.decision for d in history] == ["先解析身份", "改用替代检索"]
    assert history[0].decision_id == first.decision_id
    assert history[1].plan_change_kinds == ["add_step"]
    assert history[0].created_at <= history[1].created_at


# ---------------------------------------------------------------------------
# Session summaries + the cross-session demonstration (spec §十一)
# ---------------------------------------------------------------------------


def test_the_full_two_session_demonstration(tmp_path, studies, ctx, env):
    db = pathlib.Path(tmp_path) / "crea.db"
    study = studies.create(title="盲测复现", question="独立完成目标分子研究")

    # -- session 1 ----------------------------------------------------------
    first = StudyContextService(KnowledgeBase(db))
    first.open(study.study_id, constraints=["不查看原论文"])
    first.add_fact(study.study_id, fact_type="identity_resolved",
                   payload={"smiles": "CCO"},
                   source="resolve_chemical_identity",
                   verification=FactVerification.TOOL_VERIFIED,
                   evidence_ids=[], session_id="sess-1")
    hypothesis = first.add_hypothesis(study.study_id, "A 可能先做酯化",
                                      session_id="sess-1")
    evidence = _evidence(env)
    first.add_evidence_reference(study.study_id, evidence.evidence_id,
                                 kind=StudyEvidenceKind.LITERATURE,
                                 session_id="sess-1")
    first.record_decision(study.study_id, "先解析身份再检索",
                          reason="结构依赖前置", session_id="sess-1")
    first.record_unresolved_issue(study.study_id, "路线尚未验证",
                                  kind="route", session_id="sess-1")
    first.record_session_summary(study.study_id, StudySessionSummary(
        session_id="sess-1", status="running", outcome="input_required",
        step_count=2, note="身份歧义待用户选择"))

    # -- session 2 ----------------------------------------------------------
    second = StudyContextService(KnowledgeBase(db))
    context = second.get_context(study.study_id)
    assert context.goal == "独立完成目标分子研究"
    assert [f.fact_type for f in context.facts] == ["identity_resolved"]
    assert [h.status for h in context.hypotheses] == [HypothesisStatus.PROPOSED]
    assert [r.kind for r in context.evidence_refs] == [
        StudyEvidenceKind.LITERATURE]
    assert [d.decision for d in context.decisions] == ["先解析身份再检索"]
    assert [i.content for i in context.issues] == ["路线尚未验证"]
    assert [s.session_id for s in context.session_summaries] == ["sess-1"]
    # 事实、证据、假设、未解决问题四类互不混淆。
    assert context.facts and context.hypotheses and context.issues
    assert all(f.verification is FactVerification.TOOL_VERIFIED
               for f in context.facts)
    assert second.resolve_unresolved_issue(
        study.study_id, context.issues[0].issue_id, "已用文献反应验证",
        session_id="sess-2").status is IssueStatus.RESOLVED
    assert hypothesis.hypothesis_id in [h.hypothesis_id
                                        for h in context.hypotheses]


# ---------------------------------------------------------------------------
# 16-17. old-database compatibility and the blind-study boundary
# ---------------------------------------------------------------------------


def test_an_pre_m15_database_keeps_working_and_gains_the_new_tables(
        tmp_path):
    db = pathlib.Path(tmp_path) / "crea.db"
    # A study written by the old surface...
    old_kb = KnowledgeBase(db)
    old_kb.initialize()
    study = ResearchStudyService(old_kb).create(title="旧研究", question="Q")
    old_kb.close()

    # ...reads back after M1.5's schema addition, and the new tables work.
    kb = KnowledgeBase(db)
    kb.initialize()
    assert kb.get_study(study.study_id).title == "旧研究"
    service = StudyContextService(kb)
    service.add_hypothesis(study.study_id, "旧研究里的新假设")
    assert [h.content for h in
            service.hypotheses(study.study_id)] == ["旧研究里的新假设"]
    kb.close()


def test_the_blind_study_boundary_has_no_global_read(ctx, study_a, study_b):
    """A benchmark study cannot be read through another study's context."""
    ctx.add_fact(study_a.study_id, fact_type="reference_route_found",
                 payload={"route": "专家路线"},
                 source="benchmark_reference",
                 verification=FactVerification.USER_STATED)
    # Study B's ordinary context query cannot reach it: every query is
    # scoped, and the service exposes no cross-study search at all.
    context_b = ctx.get_context(study_b.study_id)
    assert all("reference_route_found" != f.fact_type for f in context_b.facts)
    assert ctx.facts(study_b.study_id,
                     fact_type="reference_route_found") == []

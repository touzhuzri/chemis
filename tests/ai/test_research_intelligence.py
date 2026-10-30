"""M2.0 -- the AI Research Intelligence: does the model actually decide?

The Golden Cases (§ ten of the M2.0 brief) live here, and they are the
point of the module: not "can the model return JSON", but "does the model
change what it does next because of what a tool actually returned".

    Case A  a normal research flow, tool chosen and interpreted
    Case B  an ambiguous identity -- no structure-dependent call
    Case C  a tool that returns nothing -- no identical repeat
    Case D  a tool that returns part -- the part is not smoothed over
    Case E  the direction changes because of a result
    Case F  candidates stay candidates; uncertainty survives to the end
    Case G  the budget stops the run and the run says so

Everything else here is the machinery those cases need: the validator,
the budget, the study-context writes, the pause/resume, and the adapter
that lets the M1.7 blind runner drive this loop without seeing a store.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from crea.agent.intelligence import (
    DecisionValidator,
    IntelligenceRunResult,
    ResearchBudget,
    ResearchIntelligence,
    ResearchMode,
    ResearchState,
)
from crea.agent.intelligence_runtime import AiResearchRuntime
from crea.agent.observer import ObservationKind
from crea.agent.tools import ExecutionPolicy, ToolRegistry, ToolSpec
from crea.core.models.research import TaskStatus
from crea.core.models.research_decision import DecisionKind, DecisionOutcome
from crea.core.models.research_intent import (
    EntityIdentityStatus,
    IntentEntity,
    ResearchIntent,
    ResearchIntentKind,
)
from crea.core.models.study_context import FactVerification, HypothesisStatus
from crea.llm.fake import ScriptExhausted, TextScriptedLLMProvider
from crea.services.golden_case_store import GoldenCaseStore
from crea.services.knowledge_base import KnowledgeBase
from crea.services.study_context import StudyContextService


# ---------------------------------------------------------------------------
# A registry whose tools answer from a script
# ---------------------------------------------------------------------------


class _LiteratureArgs(BaseModel):
    query: str = Field(default="")
    limit: int = Field(default=10)


class _IdentityArgs(BaseModel):
    kind: str = Field(default="NAME")
    value: str = Field(default="")


class _AnalyzeArgs(BaseModel):
    smiles: str = Field(default="")


class _RetroArgs(BaseModel):
    target_smiles: str = Field(default="")


class Script:
    """One tool's answers, in order. A payload, an exception, or a callable."""

    def __init__(self, payloads: list | None = None) -> None:
        self.payloads = list(payloads or [])
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self.payloads:
            return {"status": "ok"}
        payload = self.payloads.pop(0)
        if callable(payload):
            payload = payload(**kwargs)
        if isinstance(payload, Exception):
            raise payload
        return payload

    @property
    def count(self) -> int:
        return len(self.calls)


def build_registry(*, literature=None, identity=None, analyze=None,
                   retro=None, policy=None) -> tuple[ToolRegistry, dict]:
    """An enforcing registry whose tools are all scriptable."""
    scripts = {
        "search_literature": literature or Script(),
        "resolve_chemical_identity": identity or Script(),
        "analyze_molecule": analyze or Script(),
        "retrosynthesize": retro or Script(),
    }
    registry = ToolRegistry(
        enforce_policy=True,
        policy=policy or ExecutionPolicy(allow_network=True,
                                         allow_model_calls=False))
    registry.register(ToolSpec(
        name="search_literature", description="有界文献发现",
        arguments_model=_LiteratureArgs, run=scripts["search_literature"],
        access=("local",), sets_facts=("literature_searched",)))
    registry.register(ToolSpec(
        name="resolve_chemical_identity", description="解析化学身份",
        arguments_model=_IdentityArgs, run=scripts["resolve_chemical_identity"],
        access=("local",),
        fact_rule=lambda data: ("identity_resolved",)
        if (isinstance(data, dict) and data.get("status") == "resolved")
        else ()))
    registry.register(ToolSpec(
        name="analyze_molecule", description="确定性结构计算",
        arguments_model=_AnalyzeArgs, run=scripts["analyze_molecule"],
        access=("local",)))
    registry.register(ToolSpec(
        name="retrosynthesize", description="候选路线",
        arguments_model=_RetroArgs, run=scripts["retrosynthesize"],
        access=("local",), requires=("identity_resolved",)))
    return registry, scripts


def scripted(*payloads) -> TextScriptedLLMProvider:
    return TextScriptedLLMProvider(
        [p if isinstance(p, str) else json.dumps(p, ensure_ascii=False)
         for p in payloads])


def tool_call(tool: str, arguments: dict, *, summary="执行一步", purpose="研究",
              expected="候选结果", evidence=None, hypotheses=None,
              interpretation="") -> dict:
    return {
        "kind": "continue_with_tool",
        "tool_call": {
            "tool": tool, "arguments": arguments,
            "decision_summary": summary, "purpose": purpose,
            "expected_information": expected,
            "related_hypothesis_ids": list(hypotheses or []),
            "relevant_evidence_ids": list(evidence or []),
        },
        "observation_interpretation": interpretation,
    }


def complete(reason="研究完成", sufficiency="partial", limitations=None,
             unresolved=None, achieved=None) -> dict:
    return {
        "kind": "complete",
        "completion": {
            "completion_reason": reason,
            "achieved_objectives": list(achieved or []),
            "unresolved_questions": list(unresolved or []),
            "evidence_sufficiency": sufficiency,
            "limitations": list(limitations or []),
        },
    }


def ask(question, why="需要人来回答", options=None) -> dict:
    return {"kind": "ask_user", "ask_user": {
        "question": question, "why": why, "options": list(options or []),
        "expected_information": "用户的明确选择"}}


def hypothesis(content, *, hypothesis_id="", status="proposed",
               supporting=None, rationale="依据工具结果") -> dict:
    return {"kind": "update_hypothesis", "hypothesis_update": {
        "hypothesis_id": hypothesis_id, "hypothesis": content,
        "rationale_summary": rationale,
        "supporting_evidence_ids": list(supporting or []),
        "contradicting_evidence_ids": [], "uncertainty": "样本有限",
        "next_verification_action": "换一种检索式复核",
        "proposed_status": status}}


def direction(previous, new, reason, steps=None) -> dict:
    return {"kind": "update_direction", "direction_update": {
        "previous_direction": previous, "new_direction": new,
        "reason": reason, "triggering_step_ids": list(steps or []),
        "relevant_evidence_ids": [], "abandoned": []}}


def blocked(reason, blocker="no_tool_can_help") -> dict:
    return {"kind": "blocked", "blocked": {
        "reason": reason, "blocker": blocker, "details": ""}}


AMBIGUOUS_INTENT = ResearchIntent(
    raw_input="研究水杨酸甲酯的合成", goal="研究水杨酸甲酯的合成",
    primary_intent=ResearchIntentKind.RETROSYNTHESIS, validated=True,
    entities=[IntentEntity(
        name="水杨酸甲酯", kind="name", value="水杨酸甲酯",
        identity_status=EntityIdentityStatus.AMBIGUOUS,
        candidates=[{"candidate_name": "甲基水杨酸"},
                    {"candidate_name": "水杨酸甲酯盐"}])])


# ---------------------------------------------------------------------------
# Case A -- a normal research flow
# ---------------------------------------------------------------------------


def test_case_a_the_model_chooses_the_tool_and_the_run_completes():
    literature = Script([{"status": "ok", "count": 2,
                          "papers": [{"title": "A green route"}],
                          "evidence_ids": ["ev-1"]}])
    registry, _ = build_registry(literature=literature)
    llm = scripted(
        tool_call("search_literature", {"query": "阿司匹林 绿色合成",
                                        "limit": 5},
                  summary="检索绿色路线", purpose="定位近五年报道",
                  expected="候选路线", interpretation="检索命中两条"),
        complete("已有两条候选路线", sufficiency="partial",
                 limitations=["未做实验验证"]))

    result = ResearchIntelligence(registry, llm).run("研究阿司匹林的绿色路线")

    assert result.outcome == "completed"
    assert result.status is TaskStatus.COMPLETED
    assert [s.tool for s in result.steps] == ["search_literature"]
    assert literature.calls == [{"query": "阿司匹林 绿色合成", "limit": 5}]
    assert result.mode is ResearchMode.AI_RESEARCH_INTELLIGENCE


def test_case_a_the_tool_result_is_read_not_just_observed():
    """The run keeps the model's scientific reading beside the observer's
    kind -- and the reading never replaces the result."""
    literature = Script([{"status": "ok", "count": 0, "papers": [],
                          "note": "no match"}])
    registry, _ = build_registry(literature=literature)
    llm = scripted(
        tool_call("search_literature", {"query": "x"},
                  interpretation="无命中说明检索式过窄，而不是化合物没有文献"),
        complete("检索完成但无命中", sufficiency="insufficient",
                 limitations=["检索式可能过窄"]))

    result = ResearchIntelligence(registry, llm).run("研究X")

    observation = result.observations[0]
    assert observation.kind is ObservationKind.NO_RESULT
    assert result.interpretations[0].observation_kind == "no_result"
    assert "过窄" in result.interpretations[0].interpretation
    # The original payload is untouched by the interpretation.
    assert result.tool_results[0].data["count"] == 0


def test_case_a_every_turn_leaves_an_audit_record():
    literature = Script([{"status": "ok", "evidence_ids": ["ev-1"]}])
    registry, _ = build_registry(literature=literature)
    llm = scripted(tool_call("search_literature", {"query": "x"}),
                   complete("done"))

    result = ResearchIntelligence(registry, llm).run("研究X")

    assert [r.decision_type for r in result.decision_records] == [
        DecisionKind.CONTINUE_WITH_TOOL, DecisionKind.COMPLETE]
    assert all(r.validation_result is DecisionOutcome.ACCEPTED
               for r in result.decision_records)
    record = result.decision_records[0]
    assert record.selected_tool == "search_literature"
    assert record.model_provider == "text-scripted"
    assert record.prompt_version.startswith("m2.0-intelligence")
    assert record.duration_ms >= 0
    assert record.associated_step_id == "step-1"


# ---------------------------------------------------------------------------
# Case B -- the structure is not settled
# ---------------------------------------------------------------------------


def test_case_b_an_ambiguous_identity_is_shown_to_the_model():
    """The state names the unsettled entities so the model does not have
    to re-derive it -- and so a structure-dependent call is visibly wrong."""
    registry, _ = build_registry()
    state = ResearchState(goal="研究水杨酸甲酯", session_id="s",
                          intent=AMBIGUOUS_INTENT)
    rendered = state.as_prompt_dict()

    assert rendered["entities_whose_structure_is_not_settled"] == [
        "水杨酸甲酯"]
    assert rendered["chemical_entities"][0]["identity_status"] == "ambiguous"
    assert rendered["chemical_entities"][0]["candidates"] == 2


def test_case_b_the_model_asks_rather_than_guessing_an_identity():
    registry, _ = build_registry()
    llm = scripted(ask("水杨酸甲酯指的是哪一个候选？",
                       options=["甲基水杨酸", "水杨酸甲酯盐"]))

    result = ResearchIntelligence(registry, llm).run(
        "研究水杨酸甲酯的合成", intent=AMBIGUOUS_INTENT)

    assert result.outcome == "input_required"
    assert result.awaiting is not None
    assert "候选" in result.awaiting.question
    assert result.awaiting.options == ["甲基水杨酸", "水杨酸甲酯盐"]
    assert result.steps == [], "没有工具被调用：身份未定"
    assert result.evidence_ids == []


def test_case_b_a_structure_tool_refused_by_precondition_is_not_a_fact():
    """A model that tries anyway is stopped by the registry's own guard,
    and the run records the refusal -- never a structure it invented."""
    retro = Script([{"status": "ok", "routes": []}])
    registry, _ = build_registry(retro=retro)
    # The model tries retrosynthesis without ever resolving an identity.
    llm = scripted(
        tool_call("retrosynthesize", {"target_smiles": "C1=CC=CC=C1O"},
                  purpose="直接给路线"),
        blocked("身份未解析，不能继续", "identity_unresolved"))

    result = ResearchIntelligence(registry, llm).run(
        "研究水杨酸甲酯的合成", intent=AMBIGUOUS_INTENT)

    assert retro.count == 0, "前置条件未满足的工具没有执行"
    assert result.observations[0].kind is ObservationKind.NEEDS_INPUT
    assert "identity_resolved" in result.observations[0].summary
    assert result.tool_results[0].error_type == "PRECONDITION_NOT_MET"
    assert result.evidence_ids == []
    assert result.outcome in ("failed", "partial")


def test_case_b_a_name_level_discovery_is_allowed_and_labelled():
    """Without a structure the model may still search by name -- and the
    run keeps the result as a discovery, not as verified chemistry."""
    literature = Script([{"status": "ok", "count": 1,
                          "papers": [{"title": "methyl salicylate"}],
                          "evidence_ids": []}])
    registry, _ = build_registry(literature=literature)
    llm = scripted(
        tool_call("search_literature", {"query": "水杨酸甲酯"},
                  purpose="名称级文献发现",
                  interpretation="这是名称级发现，不是已验证的化学证据"),
        complete("名称级发现完成", sufficiency="insufficient",
                 limitations=["结构身份未确认，不能做结构相关计算"]))

    result = ResearchIntelligence(registry, llm).run(
        "研究水杨酸甲酯的合成", intent=AMBIGUOUS_INTENT)

    assert literature.count == 1
    assert [o.kind for o in result.observations] == [ObservationKind.SUCCESS]
    assert result.completion.evidence_sufficiency.value == "insufficient"
    assert any("结构身份未确认" in item
               for item in result.completion.limitations)


# ---------------------------------------------------------------------------
# Case C -- the tool returns nothing
# ---------------------------------------------------------------------------


def test_case_c_no_result_is_understood_and_not_repeated():
    literature = Script([{"status": "no_result", "count": 0, "papers": []},
                         {"status": "no_result", "count": 0, "papers": []}])
    registry, _ = build_registry(literature=literature)
    llm = scripted(
        tool_call("search_literature", {"query": "X"},
                  interpretation="无命中"),
        # The model tries the identical call again.
        tool_call("search_literature", {"query": "X"}),
        complete("检索完成但无命中", sufficiency="insufficient",
                 limitations=["检索不到不等于化学上不可行"]))

    result = ResearchIntelligence(registry, llm).run("研究X")

    assert literature.count == 1, "完全相同的调用没有被重复执行"
    rejected = [r for r in result.decision_records
                if r.validation_result is DecisionOutcome.REJECTED]
    assert rejected and any("已经执行过" in issue
                            for issue in rejected[0].warnings)


def test_case_c_a_changed_query_is_a_different_call():
    literature = Script([{"status": "no_result", "count": 0},
                         {"status": "ok", "count": 1,
                          "papers": [{"title": "hit"}]}])
    registry, _ = build_registry(literature=literature)
    llm = scripted(
        tool_call("search_literature", {"query": "X"}),
        tool_call("search_literature", {"query": "X 的另一种检索式"},
                  purpose="换一种检索式"),
        complete("第二次检索命中"))

    result = ResearchIntelligence(registry, llm).run("研究X")

    assert literature.count == 2
    assert [c["query"] for c in literature.calls] == ["X", "X 的另一种检索式"]
    assert result.evidence_ids == [] or result.outcome == "completed"


def test_case_c_a_repeated_identical_call_is_refused_by_the_validator():
    registry, _ = build_registry()
    state = ResearchState(goal="g", session_id="s")
    state.note_observation(
        _observation("search_literature", ObservationKind.NO_RESULT),
        arguments={"query": "X"})
    verdict = DecisionValidator(registry).validate(
        _decision_payload(tool_call("search_literature", {"query": "X"})),
        state, budget=ResearchBudget(), tool_calls_used=1)
    assert verdict.outcome is DecisionOutcome.REJECTED
    assert any("已经执行过" in issue for issue in verdict.issues)


# ---------------------------------------------------------------------------
# Case D -- the tool returns part
# ---------------------------------------------------------------------------


def test_case_d_partial_is_recorded_as_partial():
    literature = Script([{"status": "partial", "count": 1,
                          "papers": [{"title": "one"}],
                          "warnings": ["第二数据源不可用"],
                          "evidence_ids": ["ev-1"]}])
    registry, _ = build_registry(literature=literature)
    llm = scripted(
        tool_call("search_literature", {"query": "X"},
                  interpretation="只有一条可靠结果，另一数据源缺失"),
        complete("部分完成", sufficiency="partial",
                 limitations=["只覆盖一个数据源"]))

    result = ResearchIntelligence(registry, llm).run("研究X")

    assert result.observations[0].kind is ObservationKind.PARTIAL
    assert result.tool_results[0].data["warnings"] == ["第二数据源不可用"]
    assert result.outcome == "completed"
    assert result.completion.evidence_sufficiency.value == "partial"
    assert result.completion.limitations == ["只覆盖一个数据源"]


def test_case_d_a_partial_result_is_never_reported_as_a_whole_one():
    """The report the run renders keeps the missing branch visible."""
    from crea.report.intelligence import render_intelligence_report

    literature = Script([{"status": "partial", "papers": [{"title": "one"}],
                          "warnings": ["第二数据源不可用"]}])
    registry, _ = build_registry(literature=literature)
    llm = scripted(tool_call("search_literature", {"query": "X"},
                             interpretation="缺一个分支"),
                   complete("部分完成", sufficiency="partial",
                            limitations=["只覆盖一个数据源"]))

    result = ResearchIntelligence(registry, llm).run("研究X")
    text = render_intelligence_report(result)

    assert "partial" in text
    assert "证据部分充分" in text
    assert "只覆盖一个数据源" in text


# ---------------------------------------------------------------------------
# Case E -- the direction changes
# ---------------------------------------------------------------------------


def test_case_e_the_direction_changes_because_of_a_result():
    literature = Script([{"status": "ok", "evidence_ids": []}])
    registry, _ = build_registry(literature=literature)
    llm = scripted(
        tool_call("search_literature", {"query": "路线报道"},
                  interpretation="没有可靠结构身份，路线检索没有意义"),
        direction("检索合成路线", "先解决化学身份",
                  "检索结果无法对应到唯一结构", steps=["step-1"]),
        tool_call("resolve_chemical_identity",
                  {"kind": "NAME", "value": "水杨酸甲酯"},
                  purpose="先把身份定下来",
                  interpretation="按名称解析，结果可能是候选"),
        complete("身份已解析到候选", sufficiency="partial",
                 limitations=["候选需要用户确认"]))

    result = ResearchIntelligence(registry, llm).run("研究水杨酸甲酯的合成")

    assert [s.tool for s in result.steps] == ["search_literature",
                                              "resolve_chemical_identity"]
    kinds = [d.kind for d in result.decisions]
    assert DecisionKind.UPDATE_DIRECTION in kinds
    index = kinds.index(DecisionKind.UPDATE_DIRECTION)
    # The change comes after the result that forced it, and before the new
    # action it led to.
    assert kinds[:index] == [DecisionKind.CONTINUE_WITH_TOOL]
    assert kinds[index + 1] == DecisionKind.CONTINUE_WITH_TOOL


def test_case_e_a_direction_change_reaches_the_audit_trail_with_its_reason():
    registry, _ = build_registry()
    llm = scripted(direction("A", "B", "结果表明A不合适"),
                   complete("改道完成"))

    result = ResearchIntelligence(registry, llm).run("研究X")

    record = next(r for r in result.decision_records
                  if r.decision_type is DecisionKind.UPDATE_DIRECTION)
    assert record.decision_summary == "结果表明A不合适"
    assert record.selected_tool == ""


# ---------------------------------------------------------------------------
# Case F -- the evidence is not enough
# ---------------------------------------------------------------------------


def test_case_f_candidates_are_not_upgraded_to_facts():
    literature = Script([{"status": "ok", "count": 3,
                          "candidates": [{"candidate_name": "A"},
                                         {"candidate_name": "B"}],
                          "evidence_ids": []}])
    registry, _ = build_registry(literature=literature)
    llm = scripted(
        tool_call("search_literature", {"query": "X"},
                  interpretation="返回的是候选，不是已验证的结果"),
        complete("候选不足以支持结论", sufficiency="insufficient",
                 unresolved=["候选A与B需要实验区分"],
                 limitations=["只有候选，没有实验条件"]))

    result = ResearchIntelligence(registry, llm).run("研究X")

    assert result.evidence_ids == [], "没有证据 id 就是没有任何证据"
    assert result.completion.evidence_sufficiency.value == "insufficient"
    assert result.completion.unresolved_questions == ["候选A与B需要实验区分"]
    assert result.completion.limitations == ["只有候选，没有实验条件"]


def test_case_f_a_fabricated_evidence_id_is_refused():
    """The validator only lets a decision cite ids this run actually held.
    An id the model invented cannot enter a hypothesis's support."""
    literature = Script([{"status": "ok", "evidence_ids": ["ev-real"]}])
    registry, _ = build_registry(literature=literature)
    llm = scripted(
        tool_call("search_literature", {"query": "X"}),
        hypothesis("该路线可行", supporting=["ev-FABRICATED"]),
        complete("完成"))

    result = ResearchIntelligence(registry, llm).run("研究X")

    rejected = [r for r in result.decision_records
                if r.validation_result is DecisionOutcome.REJECTED]
    assert rejected, "编造的证据 id 被拒绝"
    assert any("ev-FABRICATED" in issue for issue in rejected[0].warnings)


def test_case_f_a_hypothesis_cannot_become_supported_without_evidence(
        context):
    """Two gates, not one. The validator refuses `supported` with no
    supporting evidence before the study context is even asked, so the
    statement never enters the study at all -- and the context's own rule
    is still there for a status that gets past the validator."""
    registry, _ = build_registry()
    llm = scripted(hypothesis("该路线可行", status="supported"),
                   complete("完成"))
    result = ResearchIntelligence(registry, llm, study_context=context,
                                  study_id="study-1").run("研究X")

    rejected = [r for r in result.decision_records
                if r.validation_result is DecisionOutcome.REJECTED]
    assert rejected, "无证据的 supported 在校验层被拒绝"
    assert context.hypotheses("study-1") == [], "被拒绝的决策没有写进研究上下文"


def test_the_context_still_refuses_a_supported_it_is_directly_asked_for(
        context):
    """The context's own rule, exercised directly: the intelligence never
    gets to be the only guard."""
    from crea.core.errors import ResearchStudyError

    created = context.add_hypothesis("study-1", "该路线可行")
    with pytest.raises(ResearchStudyError) as excinfo:
        context.update_hypothesis("study-1", created.hypothesis_id,
                                  HypothesisStatus.SUPPORTED)
    assert excinfo.value.code == "HYPOTHESIS_EVIDENCE_REQUIRED"


def test_case_f_a_hypothesis_the_validator_can_refuse_outright():
    registry, _ = build_registry()
    state = ResearchState(goal="g", session_id="s")
    verdict = DecisionValidator(registry).validate(
        _decision_payload(hypothesis("X", status="supported")),
        state, budget=ResearchBudget(), tool_calls_used=0)
    assert verdict.outcome is DecisionOutcome.REJECTED
    assert any("supporting_evidence_ids" in issue for issue in verdict.issues)


# ---------------------------------------------------------------------------
# Case G -- the budget runs out
# ---------------------------------------------------------------------------


def test_case_g_the_budget_stops_the_run_and_the_run_says_so():
    literature = Script([{"status": "ok", "evidence_ids": [f"ev-{i}"]}
                         for i in range(6)])
    registry, _ = build_registry(literature=literature)
    llm = TextScriptedLLMProvider([
        json.dumps(tool_call("search_literature", {"query": f"q{i}"}),
                   ensure_ascii=False) for i in range(6)])

    result = ResearchIntelligence(
        registry, llm, budget=ResearchBudget(max_tool_calls=2)).run("研究X")

    assert literature.count == 2, "工具调用预算是真实的上限"
    assert result.tool_calls_used == 2
    assert result.blocked is not None
    assert result.blocked.blocker == "budget_exhausted"
    assert "预算耗尽" in result.reason
    assert result.outcome == "partial"
    assert result.evidence_ids, "已经完成的研究内容被保留"


def test_case_g_the_decision_budget_is_enforced_too():
    registry, _ = build_registry()
    llm = TextScriptedLLMProvider([
        json.dumps(direction("a", f"b{i}", "调整"), ensure_ascii=False)
        for i in range(5)])
    result = ResearchIntelligence(
        registry, llm, budget=ResearchBudget(max_decisions=3)).run("研究X")

    assert result.decisions_used == 3
    assert result.blocked.blocker == "budget_exhausted"


def test_case_g_a_spent_budget_never_reports_the_research_as_finished():
    registry, _ = build_registry()
    llm = TextScriptedLLMProvider([
        json.dumps(direction("a", "b", "调整"), ensure_ascii=False)
        for _ in range(4)])
    result = ResearchIntelligence(
        registry, llm, budget=ResearchBudget(max_decisions=2)).run("研究X")

    assert result.completion is None, "预算耗尽不等于研究完成"
    assert result.status is not TaskStatus.COMPLETED
    assert result.reason


def test_case_g_wall_clock_is_a_real_bound():
    """A zero-second clock is spent before the first turn; the run stops
    and says which budget ran out rather than looping."""
    registry, _ = build_registry()
    llm = scripted(direction("a", "b", "x"))
    result = ResearchIntelligence(
        registry, llm,
        budget=ResearchBudget(max_wall_clock_seconds=0.0)).run("研究X")

    assert result.blocked.blocker == "budget_exhausted"
    assert "时间预算耗尽" in result.reason


# ---------------------------------------------------------------------------
# The validator: what the model is not allowed to do
# ---------------------------------------------------------------------------


def test_an_unregistered_tool_is_refused_with_the_available_names():
    registry, _ = build_registry()
    state = ResearchState(goal="g", session_id="s")
    verdict = DecisionValidator(registry).validate(
        _decision_payload(tool_call("drop_tables", {"sql": "x"})),
        state, budget=ResearchBudget(), tool_calls_used=0)
    assert verdict.outcome is DecisionOutcome.REJECTED
    assert any("search_literature" in issue for issue in verdict.issues), (
        "拒绝要告诉模型有哪些工具可以用")


def test_arguments_are_validated_against_the_tools_own_schema():
    registry, _ = build_registry()
    state = ResearchState(goal="g", session_id="s")
    verdict = DecisionValidator(registry).validate(
        _decision_payload(tool_call("search_literature",
                                    {"query": "x", "limit": "not-an-int"})),
        state, budget=ResearchBudget(), tool_calls_used=0)
    assert verdict.outcome is DecisionOutcome.REJECTED
    assert any("Schema" in issue for issue in verdict.issues)


def test_a_call_without_a_purpose_is_refused():
    registry, _ = build_registry()
    state = ResearchState(goal="g", session_id="s")
    payload = tool_call("search_literature", {"query": "x"})
    payload["tool_call"]["purpose"] = ""
    verdict = DecisionValidator(registry).validate(
        _decision_payload(payload), state, budget=ResearchBudget(),
        tool_calls_used=0)
    assert verdict.outcome is DecisionOutcome.REJECTED
    assert any("purpose" in issue for issue in verdict.issues)


def test_a_decision_naming_a_missing_hypothesis_is_refused():
    registry, _ = build_registry()
    state = ResearchState(goal="g", session_id="s")
    payload = tool_call("search_literature", {"query": "x"},
                        hypotheses=["hyp-does-not-exist"])
    verdict = DecisionValidator(registry).validate(
        _decision_payload(payload), state, budget=ResearchBudget(),
        tool_calls_used=0)
    assert verdict.outcome is DecisionOutcome.REJECTED
    assert any("hyp-does-not-exist" in issue for issue in verdict.issues)


def test_the_model_cannot_grant_its_own_approval():
    """A persistent tool still needs a person's approval: the registry
    refuses, and the run records the refusal rather than the write."""
    from crea.agent.tools import Effects

    calls = Script([{"status": "ok"}])
    registry = ToolRegistry(
        enforce_policy=True,
        policy=ExecutionPolicy(allow_network=True, allow_model_calls=False))
    registry.register(ToolSpec(
        name="write_claim", description="写入",
        arguments_model=_LiteratureArgs, run=calls, access=("local",),
        effects=Effects.PERSISTENT.value, approval="required"))
    llm = scripted(tool_call("write_claim", {"query": "x"},
                             purpose="写入结论"),
                   blocked("需要审批", "approval_required"))

    result = ResearchIntelligence(registry, llm).run("研究X")

    assert calls.count == 0
    assert result.tool_results[0].error_type == "APPROVAL_REQUIRED"


# ---------------------------------------------------------------------------
# When the model is not there
# ---------------------------------------------------------------------------


def test_no_provider_is_reported_as_model_unavailable_not_as_a_result():
    registry, _ = build_registry()
    result = ResearchIntelligence(registry, None).run("研究X")

    assert result.blocked.blocker == "model_unavailable"
    assert result.outcome == "failed"
    assert result.evidence_ids == []
    assert result.decisions == [], "没有模型就没有研究决策"
    assert result.mode is ResearchMode.AI_RESEARCH_INTELLIGENCE


def test_a_dead_provider_stops_the_run_and_never_falls_back_silently():
    class Dead:
        name = "dead"
        model = "dead-1"

        def complete(self, request):
            raise RuntimeError("connection refused")

    registry, _ = build_registry()
    result = ResearchIntelligence(registry, Dead()).run("研究X")

    assert result.blocked.blocker == "model_unavailable"
    assert "connection refused" in result.reason
    assert result.failures
    assert result.mode is ResearchMode.AI_RESEARCH_INTELLIGENCE, (
        "没有悄悄退化成固定流程")
    assert result.steps == []


def test_forbidding_the_model_is_honoured_not_ignored():
    registry, scripts = build_registry(
        literature=Script([{"status": "ok"}]))
    literature = scripts["search_literature"]
    llm = scripted(tool_call("search_literature", {"query": "x"}))
    result = ResearchIntelligence(registry, llm).run("研究X",
                                                     allow_model=False)
    assert literature.count == 0
    assert result.blocked.blocker == "model_unavailable"
    assert result.reason == "调用方禁止调用模型"


def test_an_unusable_reply_gets_a_bounded_number_of_repair_requests():
    registry, _ = build_registry()
    llm = scripted("这不是 JSON，只是文字",
                   "还是不是 JSON",
                   "依旧不是")
    result = ResearchIntelligence(
        registry, llm, budget=ResearchBudget(max_fix_attempts=1)).run("研究X")

    assert llm.calls.__len__() == 2, "修复请求有上限"
    assert result.blocked.blocker == "model_output_invalid"
    assert result.model_calls_used == 2


def test_a_repair_request_carries_the_refusal_back_to_the_model():
    registry, _ = build_registry()
    llm = scripted(tool_call("drop_tables", {"sql": "x"}),
                   complete("修好之后完成了"))
    result = ResearchIntelligence(registry, llm).run("研究X")

    assert result.outcome == "completed"
    assert len(llm.calls) == 2
    assert "drop_tables" in llm.calls[1].messages[-1]["content"], (
        "修复请求要重复模型自己的问题")


def test_a_run_that_never_gets_a_usable_reply_produces_nothing():
    literature = Script()
    registry, _ = build_registry(literature=literature)
    llm = scripted("文字一", "文字二")
    result = ResearchIntelligence(
        registry, llm, budget=ResearchBudget(max_fix_attempts=1)).run("研究X")

    assert literature.count == 0
    assert result.status is TaskStatus.FAILED
    assert result.evidence_ids == []


# ---------------------------------------------------------------------------
# Study context: the memory the next session reads
# ---------------------------------------------------------------------------


@pytest.fixture()
def kb(tmp_path):
    store = KnowledgeBase(tmp_path / "crea.db")
    store.initialize()
    yield store
    store.close()


@pytest.fixture()
def context(kb):
    from crea.core.models.research_study import ResearchStudy, StudyStatus
    from crea.core.provenance import utc_now_iso

    now = utc_now_iso()
    kb.save_study(ResearchStudy(
        study_id="study-1", title="研究X", question="研究X",
        status=StudyStatus.ACTIVE, created_at=now, updated_at=now))
    service = StudyContextService(kb)
    service.open("study-1", goal="研究X")
    return service


@pytest.fixture()
def store(kb):
    return GoldenCaseStore(kb)


def test_a_successful_tool_call_becomes_a_tool_verified_fact(context):
    literature = Script([{"status": "ok", "evidence_ids": ["ev-1"]}])
    registry, _ = build_registry(literature=literature)
    scripted_llm = scripted(tool_call("search_literature", {"query": "x"}),
                            complete("done"))

    ResearchIntelligence(registry, scripted_llm, study_context=context,
                         study_id="study-1").run("研究X")

    facts = context.facts("study-1")
    assert facts, "工具结果进入了研究上下文"
    assert facts[0].verification is FactVerification.TOOL_VERIFIED
    assert facts[0].source == "search_literature"


def test_the_model_never_writes_a_fact_itself(context):
    """A hypothesis is the only thing a model can add, and it lands as
    `proposed` -- never as a fact, never as `tool_verified`."""
    literature = Script([{"status": "ok", "evidence_ids": ["ev-1"]}])
    registry, _ = build_registry(literature=literature)
    llm = scripted(tool_call("search_literature", {"query": "x"}),
                   hypothesis("该路线使用绿色溶剂", supporting=["ev-1"]),
                   complete("done"))

    ResearchIntelligence(registry, llm, study_context=context,
                         study_id="study-1").run("研究X")

    hypotheses = context.hypotheses("study-1")
    assert len(hypotheses) == 1
    assert hypotheses[0].status is HypothesisStatus.PROPOSED
    assert hypotheses[0].supporting_evidence_ids == ["ev-1"]
    for fact in context.facts("study-1"):
        assert fact.verification is not FactVerification.USER_STATED
        assert "假设" not in fact.fact_type


def test_a_tool_failure_becomes_an_open_issue(context):
    registry, _ = build_registry()
    llm = scripted(blocked("没有工具能解决", "no_tool_can_help"),
                   )
    # Actually make a tool fail first.
    literature = Script([RuntimeError("boom")])
    registry, _ = build_registry(literature=literature)
    llm = scripted(tool_call("search_literature", {"query": "x"}),
                   blocked("工具失败", "tool_error"))

    ResearchIntelligence(registry, llm, study_context=context,
                         study_id="study-1").run("研究X")

    issues = context.open_issues("study-1")
    assert issues and issues[0].kind == "tool_error"


def test_a_decision_reaches_the_study_context(context):
    registry, _ = build_registry()
    llm = scripted(direction("A", "B", "结果不合适"), complete("done"))

    result = ResearchIntelligence(registry, llm, study_context=context,
                                  study_id="study-1").run("研究X")

    decisions = context.decisions("study-1")
    assert decisions
    assert any("update_direction" in d.decision for d in decisions)
    assert result.study_id == "study-1"


def test_the_next_session_starts_from_what_this_one_knows(context):
    """A fact established in session one is handed back to the registry's
    guard in session two -- the prerequisite is not re-planned."""
    retro = Script([{"status": "ok", "routes": [{"route_id": "r-1"}]}])
    literature = Script([{"status": "ok", "evidence_ids": []}])
    identity = Script([{"status": "resolved", "smiles": "C1=CC=CC=C1"}])
    registry, _ = build_registry(literature=literature, retro=retro,
                                 identity=identity)

    # Session one: resolve the identity, which sets `identity_resolved`.
    ResearchIntelligence(
        registry, scripted(
            {"kind": "continue_with_tool",
             "tool_call": {"tool": "resolve_chemical_identity",
                           "arguments": {"kind": "NAME", "value": "X"},
                           "decision_summary": "解析身份",
                           "purpose": "定下研究对象",
                           "expected_information": "身份状态",
                           "related_hypothesis_ids": [],
                           "relevant_evidence_ids": []},
             "observation_interpretation": ""},
            complete("身份已解析")),
        study_context=context, study_id="study-1").run("研究X")
    assert "identity_resolved" in registry.facts.as_list()

    # Session two, a fresh registry: the stored fact is re-seeded.
    registry2, _ = build_registry(literature=Script(), retro=retro,
                                  identity=identity)
    ResearchIntelligence(
        registry2, scripted(
            tool_call("retrosynthesize", {"target_smiles": "C"}),
            complete("已有路线")),
        study_context=context, study_id="study-1").run("研究X",
                                                       session_id="sess-2")

    assert retro.count == 1, "前置事实在第二次会话中被恢复"


def test_the_session_summary_is_written_when_the_run_ends(context):
    literature = Script([{"status": "ok", "evidence_ids": ["ev-1"]}])
    registry, _ = build_registry(literature=literature)
    result = ResearchIntelligence(
        registry, scripted(tool_call("search_literature", {"query": "x"}),
                           complete("done")),
        study_context=context, study_id="study-1").run("研究X")

    summaries = context.get_context("study-1").session_summaries
    assert any(s.session_id == result.session_id for s in summaries)


def test_a_paused_run_writes_no_session_summary_and_can_be_resumed(context):
    literature = Script([{"status": "ok", "evidence_ids": ["ev-1"]}])
    registry, _ = build_registry(literature=literature)
    intelligence = ResearchIntelligence(
        registry, scripted(ask("请选择候选"), complete("done")),
        study_context=context, study_id="study-1")
    paused = intelligence.run("研究X")

    assert paused.outcome == "input_required"
    assert not any(s.session_id == paused.session_id
                   for s in context.get_context("study-1").session_summaries)

    # The person answers; the run continues and the answer is USER_STATED.
    resumed = intelligence.resume(paused, answers={"selection": "候选A"})

    assert resumed.outcome == "completed"
    assert resumed.session_id == paused.session_id
    answers = [f for f in context.facts("study-1")
               if f.fact_type.startswith("user_answer")]
    assert answers and answers[0].verification is FactVerification.USER_STATED


def test_resuming_a_run_that_is_not_paused_is_refused():
    registry, _ = build_registry()
    intelligence = ResearchIntelligence(
        registry, scripted(complete("直接完成")))
    finished = intelligence.run("研究X")
    with pytest.raises(ValueError):
        intelligence.resume(finished, answers={"x": "y"})


def test_a_run_without_a_study_context_still_researches():
    literature = Script([{"status": "ok", "evidence_ids": ["ev-1"]}])
    registry, _ = build_registry(literature=literature)
    result = ResearchIntelligence(
        registry, scripted(hypothesis("X 可能可行"),
                           complete("done"))).run("研究X")

    assert result.outcome == "completed"
    assert result.hypothesis_ids == [], "没有上下文就没有可持久化的假设 id"
    assert any("未提供研究上下文" in d for d in result.diagnostics)


def test_cancelling_stops_before_the_next_decision():
    literature = Script([{"status": "ok", "evidence_ids": []}])
    registry, _ = build_registry(literature=literature)

    class Cancelling:
        name = "fake"
        model = "fake-1"

        def __init__(self):
            self.calls = 0

        def complete(self, request):
            self.calls += 1
            return _response(json.dumps(
                tool_call("search_literature", {"query": "x"}),
                ensure_ascii=False))

    class _Response:
        text = ""
        input_tokens = None
        output_tokens = None

    def _response(text):
        r = _Response()
        r.text = text
        return r

    llm = Cancelling()
    intelligence = ResearchIntelligence(registry, llm)

    # Cancel after the first tool call by cancelling from a hook on the tool.
    original = literature.payloads

    def cancel_after_use(**kwargs):
        intelligence.cancel()
        return {"status": "ok"}

    literature.payloads = [cancel_after_use, *original]
    result = intelligence.run("研究X")

    assert result.cancelled is True
    assert llm.calls == 1, "取消后不再请求下一次模型决策"
    assert literature.count == 1, "已经在跑的工具会跑完并被如实观察"


# ---------------------------------------------------------------------------
# The runtime adapter: the blind runner can drive this loop
# ---------------------------------------------------------------------------


def test_the_adapter_reports_the_runtime_vocabulary():
    literature = Script([{"status": "ok", "evidence_ids": ["ev-1"]}])
    registry, _ = build_registry(literature=literature)
    runtime = AiResearchRuntime(
        registry, scripted(tool_call("search_literature", {"query": "x"}),
                           complete("done")))

    result = runtime.run("研究X")

    assert result.status is TaskStatus.COMPLETED
    assert result.outcome == "completed"
    assert result.session_id.startswith("sess-")
    assert [s.tool for s in result.steps] == ["search_literature"]
    assert result.evidence_ids == ["ev-1"]
    assert any("mode: ai_research_intelligence" in d
               for d in result.diagnostics)
    assert runtime.last is not None
    assert runtime.last.decision_records


def test_the_adapter_reports_a_pause_as_a_pause():
    registry, _ = build_registry()
    runtime = AiResearchRuntime(registry, scripted(
        ask("请确认目标", options=["A", "B"])))
    result = runtime.run("研究X")

    assert result.outcome == "input_required"
    assert result.awaiting is not None
    assert "请确认目标" in result.awaiting.question
    assert "A" in result.awaiting.question


def test_the_adapter_refuses_to_run_when_the_model_is_forbidden():
    literature = Script([{"status": "ok"}])
    registry, _ = build_registry(literature=literature)
    runtime = AiResearchRuntime(
        registry, scripted(tool_call("search_literature", {"query": "x"})))
    result = runtime.run("研究X", use_llm=False)

    assert literature.count == 0
    assert result.outcome == "failed"


def test_a_blind_case_can_be_run_by_the_ai_loop(store):
    """The M1.7 sealed path drives the M2.0 loop through the adapter:
    the runner still touches the store through exactly two doors."""
    from crea.core.models.golden_case import (
        BlindInput, GoldenCase, GoldenCaseType, TraceModelInfo,
        TraceToolVersion,
    )
    from crea.evaluation import RunEnvironment, run_blind_case

    store.create(GoldenCase(
        case_id="case-ai", name="AI 盲测",
        case_type=GoldenCaseType.BLIND_REPRODUCTION,
        blind_input=BlindInput(goal="研究阿司匹林的绿色合成路线")))

    literature = Script([{"status": "ok", "evidence_ids": ["ev-1"]}])
    registry, _ = build_registry(literature=literature)
    llm = scripted(tool_call("search_literature",
                             {"query": "阿司匹林 绿色合成"},
                             interpretation="检索命中"),
                   complete("已有线索", sufficiency="partial",
                            limitations=["未做实验验证"]))

    used: list[str] = []
    original_view = store.get_agent_view
    original_save = store.save_trace

    def spy_view(case_id):
        used.append("get_agent_view")
        return original_view(case_id)

    def spy_save(trace):
        used.append("save_trace")
        return original_save(trace)

    store.get_agent_view = spy_view
    store.save_trace = spy_save

    outcome = run_blind_case(
        store, "case-ai",
        runtime_factory=lambda: AiResearchRuntime(registry, llm),
        environment=RunEnvironment(
            models=[TraceModelInfo(name="test-model", version="1",
                                   provider="fake")],
            tools=[TraceToolVersion(tool="search_literature", version="1")],
            git_commit="a5fde30"))

    assert used == ["get_agent_view", "save_trace"]
    assert outcome.trace.case_id == "case-ai"
    assert outcome.trace.status in (TaskStatus.COMPLETED, TaskStatus.PARTIAL)
    assert [c.tool for c in outcome.trace.tool_calls] == ["search_literature"]
    assert outcome.trace.evidence_ids == ["ev-1"]


def test_the_blind_runner_never_sees_a_reference_through_the_ai_loop(store):
    from crea.core.models.golden_case import (
        BlindInput, GoldenCase, GoldenCaseType, ReferenceMaterial,
        ReferenceMaterialKind,
    )
    from crea.evaluation import run_blind_case

    store.create(GoldenCase(
        case_id="case-ai-2", name="AI 盲测 2",
        case_type=GoldenCaseType.BLIND_REPRODUCTION,
        blind_input=BlindInput(goal="研究X")))
    store.save_reference("case-ai-2", ReferenceMaterial(
        reference_id="ref-secret", material_type=ReferenceMaterialKind.PAPER,
        title="专家答案", content_locator="refs/answer.txt", hidden=True))

    registry, _ = build_registry()
    seen: dict = {}

    class Spy:
        name = "fake"
        model = "fake-1"

        def complete(self, request):
            seen["prompt"] = request.messages[-1]["content"]
            return _response(json.dumps(complete("没有工具可用"), ensure_ascii=False))

    class _Response:
        text = ""
        input_tokens = None
        output_tokens = None

    def _response(text):
        r = _Response()
        r.text = text
        return r

    outcome = run_blind_case(
        store, "case-ai-2",
        runtime_factory=lambda: AiResearchRuntime(registry, Spy()))

    assert "ref-secret" not in seen["prompt"]
    assert "专家答案" not in seen["prompt"]
    assert outcome.trace.evidence_ids == []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _decision_payload(payload: dict) -> ResearchDecision:
    from crea.core.models.research_decision import ResearchDecision as Model
    return Model.model_validate(payload)


def _observation(tool: str, kind: ObservationKind) -> "object":
    from crea.agent.observer import Observation
    return Observation(kind=kind, tool=tool, step_id="step-1",
                       summary="无命中")

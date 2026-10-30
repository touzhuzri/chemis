"""M1.4 -- the core agent runtime: planner + observer + registry in one loop.

Every tool here is a scripted fake behind a real enforcing `ToolRegistry`,
so the full execute -> observe -> decide path runs while nothing reaches a
provider. An LLM, when present, is a scripted fake whose call count is
asserted: an ordinary run must not cost a model call. What is pinned here is
the runtime's own contract -- one execution per step, approval before
persistent effects, pauses a caller can resume, honest status mapping, and a
bounded trajectory -- not the planner's internals, which M1.3 already froze.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from crea.agent.observer import ObservationKind
from crea.agent.planner import PlanChangeKind, PlanDecisionKind, Planner
from crea.agent.runtime import CoreAgentRuntime
from crea.agent.tool_surface import build_agent_tools
from crea.agent.tools import ExecutionPolicy, ToolRegistry, ToolSpec
from crea.core.models.knowledge import (
    ResearchPlan,
    ResearchPlanAction,
    ResearchPlanStep,
)
from crea.core.models.research import ActionKind, TaskStatus, ToolStatus
from crea.llm.fake import FakeLLMProvider, TextScriptedLLMProvider


class _Args(BaseModel):
    query: str = Field(default="")


class _RequiredArgs(BaseModel):
    query: str = Field(min_length=1)


class _Script:
    """A scripted tool: one payload per call, every call recorded.

    An `Exception` in the script is raised, so the registry's real
    exception-to-ERROR path is exercised rather than simulated.
    """

    def __init__(self, payloads: list) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self.payloads:
            return {"status": "ok"}
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return payload


def _registry(*specs: ToolSpec, policy: ExecutionPolicy | None = None) -> ToolRegistry:
    registry = ToolRegistry(enforce_policy=True,
                            policy=policy or ExecutionPolicy())
    for spec in specs:
        registry.register(spec)
    return registry


def _spec(name: str, run, **kwargs) -> ToolSpec:
    kwargs.setdefault("access", ("local",))
    kwargs.setdefault("arguments_model", _Args)
    return ToolSpec(name=name, description=name, run=run, **kwargs)


def _step(step_id: str, tool: str, action: ResearchPlanAction =
          ResearchPlanAction.SEARCH_LITERATURE, arguments: dict | None = None,
          **kwargs) -> ResearchPlanStep:
    return ResearchPlanStep(
        step_id=step_id, tool=tool, action=action,
        arguments={"query": "q"} if arguments is None else arguments, **kwargs)


def _report() -> ResearchPlanStep:
    return ResearchPlanStep(action=ResearchPlanAction.REPORT, step_id="report",
                            reason="汇总")


def _plan(*steps: ResearchPlanStep, goal: str = "goal") -> ResearchPlan:
    return ResearchPlan(goal=goal, steps=list(steps))


# ---------------------------------------------------------------------------
# The loop: one execution per step, in order, through the registry
# ---------------------------------------------------------------------------


def test_run_executes_each_step_once_in_order_and_completes():
    probe = _Script([
        {"status": "ok", "evidence_ids": ["e1"]},
        {"status": "ok", "evidence_ids": ["e2", "e1"]},
    ])
    runtime = CoreAgentRuntime(_registry(_spec("probe", probe)))
    # s2 的参数与 s1 不同：完全相同的调用签名会被计划层判重，这是 M1.3 冻结的语义。
    result = runtime.run("goal", plan=_plan(_step("s1", "probe"),
                                            _step("s2", "probe",
                                                  arguments={"query": "r"}),
                                            _report()))

    assert result.status is TaskStatus.COMPLETED
    assert result.outcome == "completed"
    assert [call["query"] for call in probe.calls] == ["q", "r"]
    assert [s.step_id for s in result.steps] == ["s1", "s2", "report"]
    assert result.steps[0].result_status is ToolStatus.OK
    assert result.steps[-1].action is ActionKind.FINAL
    assert result.steps[-1].tool is None, "报告步骤不得被当作工具执行"
    # Evidence ids arrive only through the tools, in order, deduplicated.
    assert result.evidence_ids == ["e1", "e2"]
    assert result.awaiting is None
    assert result.cancelled is False


def test_the_decision_trail_records_the_start_and_every_step():
    runtime = CoreAgentRuntime(_registry(
        _spec("probe", _Script([{"status": "ok"}, {"status": "ok"}]))))
    result = runtime.run("goal", plan=_plan(_step("s1", "probe"),
                                            _step("s2", "probe",
                                                  arguments={"query": "r"}),
                                            _report()))

    assert [d.kind for d in result.decisions] == [
        PlanDecisionKind.CONTINUE, PlanDecisionKind.CONTINUE,
        PlanDecisionKind.CONTINUE]
    assert result.decisions[0].executed_step_id == ""
    assert result.decisions[1].executed_step_id == "s1"
    assert result.decisions[2].executed_step_id == "s2"
    assert result.decision.kind is PlanDecisionKind.CONTINUE  # report 是最后一步


def test_an_ordinary_run_costs_no_model_call():
    llm = FakeLLMProvider([])
    probe = _Script([{"status": "ok"}])
    runtime = CoreAgentRuntime(_registry(_spec("probe", probe)), llm)
    result = runtime.run("研究 1,4-diphenyl-1,3-diyne",
                         plan=_plan(_step("s1", "probe"), _report()))

    assert llm.calls == []
    assert result.status is TaskStatus.COMPLETED


def test_identical_runs_produce_identical_decisions_and_state():
    def one_run() -> CoreAgentRuntime:
        probe = _Script([{"status": "ok", "evidence_ids": ["e1"]},
                         {"status": "no_results"}])
        return CoreAgentRuntime(_registry(_spec("probe", probe)))

    first = one_run().run("goal", plan=_plan(_step("s1", "probe"),
                                             _step("s2", "probe"), _report()))
    second = one_run().run("goal", plan=_plan(_step("s1", "probe"),
                                              _step("s2", "probe"), _report()))

    assert [d.model_dump(exclude={"trigger"}) for d in first.decisions] == \
        [d.model_dump(exclude={"trigger"}) for d in second.decisions]
    assert first.session.plan.model_dump() == second.session.plan.model_dump()
    assert first.session.outcomes == second.session.outcomes
    assert first.reason == second.reason


# ---------------------------------------------------------------------------
# The six observation kinds, end to end
# ---------------------------------------------------------------------------


def test_no_result_completes_as_a_result_not_a_failure():
    probe = _Script([{"status": "no_results"}])
    runtime = CoreAgentRuntime(_registry(_spec("probe", probe)))
    result = runtime.run("goal", plan=_plan(_step("s1", "probe"), _report()))

    assert result.status is TaskStatus.COMPLETED
    assert result.outcome == "no_results"
    assert "无命中" in result.reason
    assert len(probe.calls) == 1
    assert result.evidence_ids == []


def test_unavailable_uses_the_declared_fallback_once():
    primary = _Script([{"status": "provider_unavailable"}])
    backup = _Script([{"status": "ok"}])
    runtime = CoreAgentRuntime(_registry(_spec("primary", primary),
                                         _spec("backup", backup)))
    plan = _plan(_step("s1", "primary", fallback_tool="backup",
                       fallback_arguments={"query": "alt"}),
                 _report())
    result = runtime.run("goal", plan=plan)

    # 替代步骤成功回答，但 s1 本身的 UNAVAILABLE 仍是记录在案的限制：
    # 计划层的终局规则不允许硬失败被洗成干净的 completed。
    assert result.status is TaskStatus.PARTIAL
    assert result.outcome == "partial"
    assert "s1" in result.reason
    assert len(primary.calls) == 1
    assert backup.calls == [{"query": "alt"}]
    assert result.session.outcomes["s1-alt"] is ObservationKind.SUCCESS
    changes = [c for d in result.decisions for c in d.changes]
    assert any(c.kind is PlanChangeKind.ADD_STEP and c.step_id == "s1-alt"
               for c in changes), "替代步骤必须作为一次可审计的计划变更"


def test_an_error_is_never_reported_as_no_result_and_is_not_retried():
    probe = _Script([RuntimeError("boom")])
    runtime = CoreAgentRuntime(_registry(_spec("probe", probe)))
    result = runtime.run("goal", plan=_plan(_step("s1", "probe"), _report()))

    assert result.outcome == "failed"
    assert result.status is TaskStatus.FAILED
    assert "无命中" not in result.reason
    assert "未成功的步骤：s1" in result.reason
    assert len(probe.calls) == 1, "ERROR 不触发自动重试"


def test_partial_completes_as_partial_and_keeps_the_missing_branches():
    probe = _Script([{"status": "partial_success",
                      "papers": [{"id": "p1"}],
                      "limitations": ["缺少另一侧对照"]}])
    runtime = CoreAgentRuntime(_registry(_spec("probe", probe)))
    result = runtime.run("goal", plan=_plan(_step("s1", "probe"), _report()))

    assert result.status is TaskStatus.PARTIAL
    assert result.outcome == "partial"
    assert result.session.outcomes["s1"] is ObservationKind.PARTIAL
    assert any("缺少另一侧对照" in note for note in result.session.notes)
    assert result.evidence_ids == []


def test_needs_input_records_the_unresolved_item_and_pauses():
    probe = _Script([{"status": "ambiguous"}])
    runtime = CoreAgentRuntime(_registry(_spec("probe", probe)))
    result = runtime.run("goal", plan=_plan(_step("s1", "probe"), _report()))

    assert result.status is TaskStatus.RUNNING
    assert result.outcome == "input_required"
    assert result.awaiting is not None
    assert result.awaiting.step_id == "s1"
    assert result.unresolved_items == ["probe: 需要补充信息（user_selection）"]
    assert len(probe.calls) == 1


# ---------------------------------------------------------------------------
# Approval and user input: pauses the caller resolves by resuming
# ---------------------------------------------------------------------------


def _approval_registry(save: _Script) -> ToolRegistry:
    return _registry(_spec("probe", _Script([{"status": "ok"}])),
                     _spec("save", save, effects="persistent"))


def _approval_plan() -> ResearchPlan:
    return _plan(_step("s1", "probe"),
                 _step("s2", "save", action=ResearchPlanAction.SYNTHESIZE_EVIDENCE),
                 _report())


def test_a_persistent_step_pauses_for_approval_and_never_runs_unapproved():
    save = _Script([{"status": "ok", "saved": True}])
    runtime = CoreAgentRuntime(_approval_registry(save))
    result = runtime.run("goal", plan=_approval_plan())

    assert result.status is TaskStatus.RUNNING
    assert result.outcome == "input_required"
    assert result.awaiting is not None
    assert result.awaiting.requires_approval is True
    assert result.awaiting.step_id == "s2"
    assert save.calls == [], "未授权的持久化步骤不得被执行"
    paused = next(s for s in result.steps if s.step_id == "s2")
    assert paused.result_status is None
    assert "未执行" in paused.note

    resumed = runtime.resume(result, approve_step_id="s2")
    assert resumed.status is TaskStatus.COMPLETED
    assert resumed.outcome == "completed"
    assert len(save.calls) == 1
    assert [s.step_id for s in resumed.steps] == ["s1", "s2", "report"], \
        "恢复后复用暂停时的轨迹条目，不产生重复记录"


def test_caller_pre_approvals_skip_the_pause_without_bypassing_the_guard():
    save = _Script([{"status": "ok"}])
    runtime = CoreAgentRuntime(_approval_registry(save))
    result = runtime.run("goal", plan=_approval_plan(), approvals=["s2"])

    assert result.status is TaskStatus.COMPLETED
    assert len(save.calls) == 1
    assert result.awaiting is None


def test_missing_arguments_pause_and_resume_fills_them_from_answers():
    strict = _Script([{"status": "ok", "n": 1}])
    registry = _registry(
        ToolSpec(name="strict", description="strict", arguments_model=_RequiredArgs,
                 run=strict, access=("local",)))
    runtime = CoreAgentRuntime(registry)
    plan = _plan(_step("s1", "strict", arguments={}), _report())

    result = runtime.run("goal", plan=plan)
    assert result.outcome == "input_required"
    assert result.awaiting is not None
    assert result.awaiting.missing_requirements == ["query"]
    assert strict.calls == []

    resumed = runtime.resume(result, answers={"query": "aspirin"})
    assert resumed.status is TaskStatus.COMPLETED
    assert strict.calls == [{"query": "aspirin"}]
    assert resumed.session.plan.steps[0].arguments == {"query": "aspirin"}


def test_resuming_without_the_missing_answer_pauses_again_without_loops():
    strict = _Script([{"status": "ok"}])
    registry = _registry(
        ToolSpec(name="strict", description="strict", arguments_model=_RequiredArgs,
                 run=strict, access=("local",)))
    runtime = CoreAgentRuntime(registry)
    result = runtime.run("goal", plan=_plan(_step("s1", "strict", arguments={}),
                                            _report()))

    again = runtime.resume(result)
    assert again.outcome == "input_required"
    assert again.awaiting is not None
    assert strict.calls == []


def test_a_user_selection_is_asked_for_and_never_invented():
    probe = _Script([{"status": "ambiguous"}])
    runtime = CoreAgentRuntime(_registry(_spec("probe", probe)))
    result = runtime.run("goal", plan=_plan(_step("s1", "probe"), _report()))

    # The user's answer may reach the context, but the runtime never turns it
    # into a selection the planner did not route: the run stays unfinished.
    resumed = runtime.resume(result, answers={"candidate": 1})
    assert resumed.outcome != "completed"
    assert resumed.session.outcomes["s1"] is ObservationKind.NEEDS_INPUT


def test_resuming_a_finished_run_is_an_error_not_a_silent_replay():
    runtime = CoreAgentRuntime(_registry(
        _spec("probe", _Script([{"status": "ok"}]))))
    result = runtime.run("goal", plan=_plan(_step("s1", "probe"), _report()))

    with pytest.raises(ValueError):
        runtime.resume(result)


# ---------------------------------------------------------------------------
# Facts, policy, and the registry as the only executor
# ---------------------------------------------------------------------------


def test_a_tool_established_fact_reaches_the_session_and_unblocks_the_step():
    resolve = _Script([{"status": "ok"}])
    react = _Script([{"status": "ok"}])
    registry = _registry(
        _spec("resolve_chemical_identity", resolve,
              sets_facts=("identity_resolved",)),
        _spec("react", react, requires=("identity_resolved",)))
    runtime = CoreAgentRuntime(registry)
    result = runtime.run("goal", plan=_plan(_step("s1", "react"), _report()))

    assert result.status is TaskStatus.COMPLETED
    assert [s.step_id for s in result.steps] == [
        "resolve-chemical-identity", "s1", "report"]
    assert "identity_resolved" in result.session.facts
    assert "identity_resolved" in registry.facts.as_list()
    assert len(react.calls) == 1


def test_a_network_step_forbidden_by_policy_is_blocked_without_executing():
    search = _Script([{"status": "ok"}])
    runtime = CoreAgentRuntime(_registry(
        _spec("search", search, access=("network",))))
    result = runtime.run("goal", plan=_plan(_step("s1", "search"), _report()))

    assert result.status is TaskStatus.FAILED
    assert result.outcome == "failed"
    assert "network_access" in result.reason
    assert search.calls == []


def test_an_unregistered_tool_is_blocked_and_never_reaches_any_executor():
    runtime = CoreAgentRuntime(_registry())
    result = runtime.run("goal", plan=_plan(_step("s1", "ghost"), _report()))

    assert result.status is TaskStatus.FAILED
    assert "ghost" in result.reason
    assert result.steps == []


def test_a_toolless_non_report_step_is_blocked_not_skipped():
    runtime = CoreAgentRuntime(_registry(
        _spec("probe", _Script([{"status": "ok"}]))))
    plan = _plan(_step("s1", "", arguments={}), _report())

    result = runtime.run("goal", plan=plan)

    assert result.status is TaskStatus.FAILED
    assert "没有可执行的工具" in result.reason
    assert result.steps == []


def test_an_adopted_plan_gets_ids_and_canonical_tools():
    probe = _Script([{"status": "ok"}])
    registry = _registry(_spec("probe", probe))
    alias = ToolSpec(name="prb", description="alias", arguments_model=_Args,
                     run=probe, alias_of="probe")
    registry.register(alias)
    plan = ResearchPlan(steps=[
        ResearchPlanStep(action=ResearchPlanAction.SEARCH_LITERATURE,
                         tool="prb", arguments={"query": "q"}),
        # 报告步骤也不带 id：adopt 负责补齐。计划也不带 goal：run 负责补上。
        ResearchPlanStep(action=ResearchPlanAction.REPORT, reason="汇总"),
    ])

    result = CoreAgentRuntime(registry).run("my goal", plan=plan)

    assert result.status is TaskStatus.COMPLETED
    assert [s.step_id for s in result.steps] == ["step-1", "step-2"]
    assert result.steps[0].tool == "probe"
    assert any("alias" in w for w in result.diagnostics)
    assert result.session.plan.goal == "my goal"


# ---------------------------------------------------------------------------
# Cancellation and budgets
# ---------------------------------------------------------------------------


def test_cancel_from_inside_a_tool_stops_before_the_next_step():
    holder: dict = {}
    calls: list = []

    def cancelling_tool(**kwargs):
        calls.append(kwargs)
        holder["runtime"].cancel()
        return {"status": "ok"}

    probe = _Script([{"status": "ok"}])
    registry = _registry(_spec("first", cancelling_tool), _spec("second", probe))
    runtime = CoreAgentRuntime(registry)
    holder["runtime"] = runtime

    result = runtime.run("goal", plan=_plan(_step("s1", "first"),
                                            _step("s2", "second"), _report()))

    assert result.cancelled is True
    assert result.status is TaskStatus.PARTIAL
    assert result.outcome == "partial"
    assert "取消" in result.reason
    assert [s.step_id for s in result.steps] == ["s1"], "取消后不得开始新的执行"
    assert probe.calls == []


def test_a_cancelled_run_cannot_be_resumed_and_a_fresh_run_clears_the_flag():
    holder: dict = {}

    def cancelling_tool(**kwargs):
        holder["runtime"].cancel()
        return {"status": "ok"}

    registry = _registry(_spec("first", cancelling_tool),
                         _spec("probe", _Script([{"status": "ok"}])))
    runtime = CoreAgentRuntime(registry)
    holder["runtime"] = runtime
    cancelled = runtime.run("goal", plan=_plan(_step("s1", "first"), _report()))
    assert cancelled.cancelled is True

    with pytest.raises(ValueError):
        runtime.resume(cancelled)

    fresh = runtime.run("goal", plan=_plan(_step("s1", "probe"), _report()))
    assert fresh.status is TaskStatus.COMPLETED
    assert fresh.cancelled is False


def test_the_runtime_iteration_budget_stops_an_overlong_run():
    probe = _Script([{"status": "ok"}] * 5)
    runtime = CoreAgentRuntime(_registry(_spec("probe", probe)),
                               max_iterations=2)
    plan = _plan(_step("s1", "probe", arguments={"query": "a"}),
                 _step("s2", "probe", arguments={"query": "b"}),
                 _step("s3", "probe", arguments={"query": "c"}),
                 _report())

    result = runtime.run("goal", plan=plan)

    assert len(probe.calls) == 2
    assert any("iteration budget exhausted" in d for d in result.diagnostics)
    assert result.status is TaskStatus.PARTIAL
    assert result.reason.startswith("runtime 迭代预算耗尽")


def test_the_planner_decision_budget_still_applies():
    probe = _Script([{"status": "ok"}] * 5)
    registry = _registry(_spec("probe", probe))
    planner = Planner(registry, max_decisions=2)
    runtime = CoreAgentRuntime(registry, planner=planner)
    plan = _plan(_step("s1", "probe", arguments={"query": "a"}),
                 _step("s2", "probe", arguments={"query": "b"}),
                 _report())

    result = runtime.run("goal", plan=plan)

    assert result.status is TaskStatus.PARTIAL
    assert "decision budget exhausted" in result.reason
    assert len(probe.calls) == 2


# ---------------------------------------------------------------------------
# Context budget and the real surface
# ---------------------------------------------------------------------------


def test_the_context_digest_is_bounded_and_declares_truncation():
    probe = _Script([{"status": "ok", "text": "x" * 50_000}])
    runtime = CoreAgentRuntime(_registry(_spec("probe", probe)),
                               context_budget_chars=1024)
    result = runtime.run("goal", plan=_plan(_step("s1", "probe"), _report()))

    digest = result.context
    assert digest["truncated"] is True
    assert digest["original_chars"] >= 50_000
    # 预览里嵌套的引号在序列化时会转义，边界是预算加转义开销，而不是字节数相等。
    assert len(json.dumps(digest, ensure_ascii=False)) <= 2048
    assert len(json.dumps(digest)) < 5_000, "摘要必须远小于原始载荷"
    # The session itself is untouched by the budget.
    assert result.session.observations["s1"].data["text"] == "x" * 50_000


def test_a_model_rejected_plan_is_reported_and_the_run_falls_back():
    llm = TextScriptedLLMProvider(["this is not a plan"])
    runtime = CoreAgentRuntime(
        build_agent_tools(policy=ExecutionPolicy(allow_network=True)), llm)
    result = runtime.run("Compare A and B then search literature and then report")

    assert len(llm.calls) == 1
    assert any("model_rejected" in d for d in result.diagnostics)
    assert [s.step_id for s in result.session.plan.steps] == [
        "assess-knowledge", "search-literature", "report"]


def test_the_real_surface_without_backends_reports_honestly():
    runtime = CoreAgentRuntime(
        build_agent_tools(policy=ExecutionPolicy(allow_network=True)))
    result = runtime.run("研究 1,4-diphenyl-1,3-diyne", use_llm=False)

    assert [s.step_id for s in result.session.plan.steps] == [
        "assess-knowledge", "search-literature", "report"]
    assert result.evidence_ids == []
    assert result.status in (TaskStatus.FAILED, TaskStatus.PARTIAL,
                             TaskStatus.COMPLETED)
    # 不可用绝不能被说成“没有结果”。
    kinds = set(result.session.outcomes.values())
    assert ObservationKind.UNAVAILABLE not in kinds or result.outcome != "no_results"
    for entry in result.steps:
        if entry.action is ActionKind.TOOL_CALL and entry.tool:
            assert entry.result_status is not None, "执行过的步骤必须留下真实状态"

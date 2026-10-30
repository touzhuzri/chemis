"""M1.5 -- CoreAgentRuntime wired to the study context.

The M1.4 loop is unchanged when no context is configured (its own suite
proves that); what is pinned here is the integration: a run loads what its
study already knows, records what its tools declared (and nothing a failing
tool did not), keeps decisions and unresolved issues for the next session,
and never writes outside its study.
"""

from __future__ import annotations

import pathlib

import pytest
from pydantic import BaseModel, Field

from crea.agent.runtime import CoreAgentRuntime
from crea.agent.tools import ExecutionPolicy, ToolRegistry, ToolSpec
from crea.core.models.knowledge import (
    ResearchPlan,
    ResearchPlanAction,
    ResearchPlanStep,
)
from crea.core.models.research import TaskStatus
from crea.core.models.study_context import (
    FactVerification,
    HypothesisStatus,
    IdentityCandidate,
)
from crea.services.knowledge_base import KnowledgeBase
from crea.services.research_study import ResearchStudyService
from crea.services.study_context import StudyContextService


class _Args(BaseModel):
    query: str = Field(default="")


class _ResolveArgs(BaseModel):
    """What the real identity tool accepts, including the user-selection relay."""

    kind: str = Field(min_length=1)
    value: str = Field(min_length=1)
    selected_candidate_index: int | None = None
    user_selection_statement: str = ""


class _Script:
    def __init__(self, payloads: list) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads.pop(0) if self.payloads else {"status": "ok"}
        if isinstance(payload, Exception):
            raise payload
        return payload


@pytest.fixture()
def env(tmp_path):
    kb = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    kb.initialize()
    return kb


@pytest.fixture()
def study(env):
    return ResearchStudyService(env).create(title="S", question="研究目标")


@pytest.fixture()
def ctx(env):
    return StudyContextService(env)


def _registry(*specs: ToolSpec) -> ToolRegistry:
    registry = ToolRegistry(enforce_policy=True, policy=ExecutionPolicy())
    for spec in specs:
        registry.register(spec)
    return registry


def _spec(name: str, run, **kwargs) -> ToolSpec:
    kwargs.setdefault("access", ("local",))
    return ToolSpec(name=name, description=name, arguments_model=_Args,
                    run=run, **kwargs)


def _plan(*steps: ResearchPlanStep) -> ResearchPlan:
    return ResearchPlan(goal="goal", steps=list(steps))


def _step(step_id: str, tool: str,
          action: ResearchPlanAction = ResearchPlanAction.SEARCH_LITERATURE,
          arguments: dict | None = None, **kwargs) -> ResearchPlanStep:
    return ResearchPlanStep(
        step_id=step_id, tool=tool, action=action,
        arguments={"query": "q"} if arguments is None else arguments, **kwargs)


def _report() -> ResearchPlanStep:
    return ResearchPlanStep(action=ResearchPlanAction.REPORT, step_id="report")


# ---------------------------------------------------------------------------
# 12-13. loading context; successful tools update it by declaration
# ---------------------------------------------------------------------------


def test_a_run_loads_tool_verified_facts_and_skips_the_prerequisite(
        env, ctx, study):
    # Session 1 established the fact through the declaring tool.
    ctx.add_fact(study.study_id, fact_type="identity_resolved",
                 source="resolve_chemical_identity",
                 verification=FactVerification.TOOL_VERIFIED,
                 session_id="sess-1")

    react = _Script([{"status": "ok"}])
    registry = _registry(
        _spec("react", react, requires=("identity_resolved",)))
    runtime = CoreAgentRuntime(registry, study_context=ctx,
                               study_id=study.study_id)
    result = runtime.run("goal", plan=_plan(_step("s1", "react"), _report()),
                         session_id="sess-2")

    assert result.status is TaskStatus.COMPLETED
    # No resolve step was planned: the prerequisite came from the context.
    assert [s.step_id for s in result.steps] == ["s1", "report"]
    assert "identity_resolved" in result.session.facts
    assert len(react.calls) == 1


def test_a_successful_tool_records_its_declared_facts(env, ctx, study):
    probe = _Script([{"status": "ok", "smiles": "CCO",
                      "evidence_ids": ["ev-1"]}])
    registry = _registry(
        _spec("probe", probe, sets_facts=("routes_held",)))
    runtime = CoreAgentRuntime(registry, study_context=ctx,
                               study_id=study.study_id)
    result = runtime.run("goal", plan=_plan(_step("s1", "probe"), _report()),
                         session_id="sess-1")

    assert result.status is TaskStatus.COMPLETED
    facts = ctx.facts(study.study_id, fact_type="routes_held")
    assert len(facts) == 1
    assert facts[0].verification is FactVerification.TOOL_VERIFIED
    assert facts[0].source == "probe"
    assert facts[0].session_id == "sess-1"
    assert facts[0].payload["smiles"] == "CCO"
    assert facts[0].evidence_ids == ["ev-1"]


def test_a_fact_rule_fact_is_recorded_when_the_rule_fires(env, ctx, study):
    resolve = _Script([{"status": "ok", "chemistry_allowed": True}])
    registry = _registry(_spec(
        "resolve_chemical_identity", resolve, sets_facts=(),
        fact_rule=lambda data: (("identity_resolved",)
                                if data.get("chemistry_allowed") is True
                                else ())))
    runtime = CoreAgentRuntime(registry, study_context=ctx,
                               study_id=study.study_id)
    runtime.run("goal",
                plan=_plan(_step("s1", "resolve_chemical_identity"),
                           _report()),
                session_id="sess-1")

    assert [f.fact_type for f in
            ctx.facts(study.study_id)] == ["identity_resolved"]


# ---------------------------------------------------------------------------
# 14. a failed tool fabricates nothing
# ---------------------------------------------------------------------------


def test_a_failed_tool_records_no_fact_but_keeps_the_issue(env, ctx, study):
    probe = _Script([RuntimeError("boom")])
    registry = _registry(_spec("probe", probe, sets_facts=("routes_held",)))
    runtime = CoreAgentRuntime(registry, study_context=ctx,
                               study_id=study.study_id)
    result = runtime.run("goal", plan=_plan(_step("s1", "probe"), _report()),
                         session_id="sess-1")

    assert result.outcome == "failed"
    assert ctx.facts(study.study_id) == [], "失败的工具不得产生事实"
    issues = ctx.open_issues(study.study_id)
    assert [i.kind for i in issues] == ["tool_error"]
    assert "probe" in issues[0].content


def test_an_unavailable_tool_records_an_unavailable_issue(env, ctx, study):
    probe = _Script([{"status": "provider_unavailable"}])
    registry = _registry(_spec("probe", probe, sets_facts=("routes_held",)))
    runtime = CoreAgentRuntime(registry, study_context=ctx,
                               study_id=study.study_id)
    runtime.run("goal", plan=_plan(_step("s1", "probe"), _report()),
                session_id="sess-1")

    assert ctx.facts(study.study_id) == []
    assert [i.kind for i in ctx.open_issues(study.study_id)] == [
        "tool_unavailable"]


# ---------------------------------------------------------------------------
# Decisions, session summaries, and the two-session continuation
# ---------------------------------------------------------------------------


def test_decisions_and_a_session_summary_are_kept_for_the_next_session(
        env, ctx, study):
    primary = _Script([{"status": "provider_unavailable"}])
    backup = _Script([{"status": "ok"}])
    registry = _registry(_spec("primary", primary),
                         _spec("backup", backup, sets_facts=("routes_held",)))
    runtime = CoreAgentRuntime(registry, study_context=ctx,
                               study_id=study.study_id)
    plan = _plan(_step("s1", "primary", fallback_tool="backup",
                       fallback_arguments={"query": "alt"}),
                 _report())
    runtime.run("goal", plan=plan, session_id="sess-1")

    decisions = ctx.decisions(study.study_id)
    assert any("add_step" in d.plan_change_kinds for d in decisions)
    assert any(d.observation_kind == "unavailable" for d in decisions)

    summaries = ctx.get_context(study.study_id).session_summaries
    assert [s.session_id for s in summaries] == ["sess-1"]
    assert summaries[0].outcome == "partial"
    assert summaries[0].step_count == 2
    # The next session reads the summary instead of a transcript.
    assert "sess-1" == ctx.get_context(study.study_id).session_summaries[0].session_id


def test_a_second_session_resumes_the_same_study_end_to_end(env, ctx, study):
    # -- session 1: identity ambiguity pauses the run -----------------------
    ambiguous = _Script([{
        "status": "ambiguous", "chemistry_allowed": False,
        "candidates": [
            {"input_name": "苯乙炔", "candidate_name": "苯乙炔",
             "canonical_smiles": "C#Cc1ccccc1", "source": "pubchem"},
            {"input_name": "苯乙炔", "candidate_name": "2-丙炔基苯",
             "canonical_smiles": "C#Cc1ccccc1-c1ccccc1", "source": "pubchem"},
        ],
        "reason": "两个候选结构",
    }])
    registry = _registry(_spec("resolve_chemical_identity", ambiguous,
                               sets_facts=()))
    first = CoreAgentRuntime(registry, study_context=ctx,
                             study_id=study.study_id)
    paused = first.run("goal", plan=_plan(
        _step("s1", "resolve_chemical_identity"), _report()),
        session_id="sess-1")
    assert paused.outcome == "input_required"
    records = ctx.identity_records(study.study_id)
    assert len(records) == 1
    assert records[0].selected_index is None
    assert [c.value for c in records[0].candidates] == [
        "C#Cc1ccccc1", "C#Cc1ccccc1-c1ccccc1"]

    # -- the user chooses, in the next session ------------------------------
    chosen = ctx.select_identity_candidate(
        study.study_id, records[0].record_id, 0,
        user_selection_statement="用户说：选第一个")
    arguments = ctx.identity_resolve_arguments(study.study_id,
                                               chosen.record_id)

    # -- session 2: the selection drives a fresh run ------------------------
    resolved = _Script([{"status": "ok", "chemistry_allowed": True}])
    registry2 = ToolRegistry(enforce_policy=True, policy=ExecutionPolicy())
    registry2.register(ToolSpec(
        name="resolve_chemical_identity", description="resolve",
        arguments_model=_ResolveArgs, run=resolved, access=("local",),
        sets_facts=(),
        fact_rule=lambda data: (("identity_resolved",)
                                if data.get("chemistry_allowed") else ())))
    registry2.register(_spec(
        "react", _Script([{"status": "ok"}]), requires=("identity_resolved",)))
    second = CoreAgentRuntime(registry2, study_context=ctx,
                              study_id=study.study_id)
    result = second.run("goal", identity_input=arguments,
                        plan=_plan(
                            _step("s1", "resolve_chemical_identity",
                                  arguments=arguments),
                            _step("s2", "react"),
                            _report()),
                        session_id="sess-2")
    assert result.status is TaskStatus.COMPLETED
    assert resolved.calls[0]["selected_candidate_index"] == 0
    assert resolved.calls[0]["user_selection_statement"] == "用户说：选第一个"
    assert [f.session_id for f in ctx.facts(study.study_id)] == ["sess-2"]
    # sess-1 paused without resuming, so only the finished session closed.
    summaries = ctx.get_context(study.study_id).session_summaries
    assert {s.session_id for s in summaries} == {"sess-2"}


def test_a_user_selection_from_the_context_seeds_the_next_run(
        env, ctx, study):
    # An earlier session saved candidates; the user has chosen.
    record = ctx.save_identity_candidates(
        study.study_id, question="哪个结构？",
        candidates=[IdentityCandidate(kind="SMILES", value="CCO",
                                      label="乙醇")])
    ctx.select_identity_candidate(study.study_id, record.record_id, 0,
                                  user_selection_statement="用户说：乙醇")

    resolve = _Script([{"status": "ok", "chemistry_allowed": True}])
    registry = ToolRegistry(enforce_policy=True, policy=ExecutionPolicy())
    registry.register(ToolSpec(
        name="resolve_chemical_identity", description="resolve",
        arguments_model=_ResolveArgs, run=resolve, access=("local",),
        sets_facts=("identity_resolved",)))
    # The chemistry gate refuses to run on an unresolved identity: the tool
    # answers with chemistry_allowed=False, and the planner plans the
    # prerequisite using the user's saved selection from the context.
    registry.register(_spec(
        "react", _Script([{"chemistry_allowed": False},
                          {"status": "ok"}]),
        requires=(), sets_facts=()))
    runtime = CoreAgentRuntime(registry, study_context=ctx,
                               study_id=study.study_id)
    result = runtime.run("goal", plan=_plan(_step("s1", "react"), _report()),
                         session_id="sess-2")

    assert resolve.calls[0]["value"] == "CCO"
    assert resolve.calls[0]["selected_candidate_index"] == 0
    assert resolve.calls[0]["user_selection_statement"] == "用户说：乙醇"
    # react 的 needs-input 是记录在案的限制：运行如实不报 completed。
    assert result.status is not TaskStatus.COMPLETED


def test_open_issues_reach_the_next_runs_context(env, ctx, study):
    ctx.record_unresolved_issue(study.study_id, "缺少压力数据",
                                kind="experimental_conditions")
    registry = _registry(_spec("probe", _Script([{"status": "ok"}])))
    runtime = CoreAgentRuntime(registry, study_context=ctx,
                               study_id=study.study_id)
    result = runtime.run("goal", plan=_plan(_step("s1", "probe"), _report()))

    assert result.session.context["open_issues"] == ["缺少压力数据"]


# ---------------------------------------------------------------------------
# 15. isolation from the runtime side; hypotheses stay out of facts
# ---------------------------------------------------------------------------


def test_a_runtime_run_never_writes_outside_its_study(env, ctx, study):
    other = ResearchStudyService(env).create(title="其他", question="别的研究")
    probe = _Script([{"status": "ok"}])
    registry = _registry(_spec("probe", probe, sets_facts=("routes_held",)))
    runtime = CoreAgentRuntime(registry, study_context=ctx,
                               study_id=study.study_id)
    runtime.run("goal", plan=_plan(_step("s1", "probe"), _report()),
                session_id="sess-1")

    assert [f.fact_type for f in ctx.facts(study.study_id)] == ["routes_held"]
    assert ctx.facts(other.study_id) == []
    assert ctx.get_context(other.study_id).session_summaries == []
    assert ctx.decisions(other.study_id) == []


def test_the_runtime_never_writes_a_hypothesis(env, ctx, study):
    probe = _Script([{"status": "ok", "reason": "probably an ester"}])
    registry = _registry(_spec("probe", probe))
    runtime = CoreAgentRuntime(registry, study_context=ctx,
                               study_id=study.study_id)
    runtime.run("goal", plan=_plan(_step("s1", "probe"), _report()),
                session_id="sess-1")
    study_id = study.study_id

    # Tool prose stays inside the tool-verified fact's payload; it never
    # becomes a hypothesis or a promoted fact of another kind.
    assert ctx.hypotheses(study_id) == []
    assert all(f.verification is FactVerification.TOOL_VERIFIED
               for f in ctx.facts(study_id))


def test_without_a_study_context_the_run_is_exactly_the_m14_loop():
    probe = _Script([{"status": "ok"}])
    runtime = CoreAgentRuntime(_registry(_spec("probe", probe)))
    result = runtime.run("goal", plan=_plan(_step("s1", "probe"), _report()))

    assert result.status is TaskStatus.COMPLETED
    assert result.session_id.startswith("sess-"), "无上下文也有会话 id，只是不落盘"
    assert probe.calls == [{"query": "q"}]

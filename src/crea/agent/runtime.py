"""M1.4 -- the core agent runtime: the loop that runs a plan to the end.

M1.1 declared what a tool may do, M1.2 read what a tool call produced, and
M1.3 decided what to do next. This module is the only place those three are
wired into execution:

    plan --(planner)--> CONTINUE(step)
        --(runtime)--> ToolRegistry.execute(...)
        --(observer)--> Observation
        --(planner)--> the next decision ... until COMPLETE / BLOCKED /
                       ASK_USER (a pause the caller resolves by resuming).

Four boundaries this module holds:

* **The planner never executes and the runtime never classifies.** A step runs
  through the registry exactly once, and its result is read by
  `observe_from_registry` -- the runtime adds no status vocabulary of its own.
  `RuntimeResult.status`/`outcome` reuse the existing `TaskStatus` and the
  report's `outcome` strings; nothing here reinterprets an observation.
* **An unapproved persistent step is never executed.** The planner's guard
  already refuses it; the runtime passes `approved=True` only for a step the
  session records as approved, so there is no second path around M1.1.
* **A pause is a result, not a failure.** `ASK_USER` returns the run's state
  with an `awaiting` prompt; `resume()` supplies an approval or answers and
  continues the same session. The runtime never invents an answer.
* **The trajectory is the existing model.** Executions are `ResearchStep`s,
  decisions are `PlanDecision`s, and run state is the planner's `PlanSession`.
  There is no second execution record, and an evidence id enters the result
  only through what a tool actually returned.

Relationship to `ResearchWorkflow` (the migration boundary): the workflow
remains the production control path, untouched by this module; the runtime
neither calls it nor is called by it. Wiring the API onto the runtime is a
later decision, not an M1.4 rewrite. What the runtime produces -- steps,
statuses, evidence ids and unresolved items -- is shaped so that decision can
be made later without translating a second vocabulary.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from crea.agent.observer import Observation, ObservationKind, observe_from_registry
from crea.agent.planner import (
    PlanDecision,
    PlanDecisionKind,
    PlanProposal,
    Planner,
    PlanSession,
)
from crea.agent.tools import _bound_output
from crea.core.models.knowledge import ResearchPlanAction
from crea.core.models.research import (
    ActionKind,
    ResearchStep,
    TaskStatus,
    ToolResult,
)

#: A defense-in-depth cap on loop iterations. The planner's own decision
#: budget (16) is the real bound; this exists so a planner bug cannot spin.
MAX_RUNTIME_ITERATIONS = 32

#: The aggregate budget for the context digest returned on every result. The
#: registry bounds each tool's output; this bounds the run's whole history.
DEFAULT_CONTEXT_BUDGET_CHARS = 16_000

#: The same three hard-failure kinds the planner blocks dependencies on.
_HARD_FAILURES = frozenset({
    ObservationKind.NEEDS_INPUT,
    ObservationKind.UNAVAILABLE,
    ObservationKind.ERROR,
})


class UserPrompt(BaseModel):
    """What a paused run needs from a person, exactly as the planner said it."""

    step_id: str = ""
    question: str = ""
    missing_requirements: list[str] = Field(default_factory=list)
    requires_approval: bool = False


class RuntimeResult(BaseModel):
    """One run (or one resumed continuation), with everything it rests on.

    `status`/`outcome` reuse the existing vocabularies: `TaskStatus` and the
    report's outcome strings (`completed` / `partial` / `input_required` /
    `no_results` / `failed`). `decision` is the last `PlanDecision`;
    `decisions` is the full decision trail; `steps` is the execution
    trajectory in `ResearchStep` form. `awaiting` is set only for a paused
    run. `context` is a budget-bounded digest of the session, never a
    substitute for `session` itself.
    """

    status: TaskStatus
    #: "completed" | "partial" | "input_required" | "no_results" | "failed".
    outcome: str
    reason: str
    decision: PlanDecision
    session: PlanSession
    #: The execution trajectory: one entry per executed (or waited-on) step.
    steps: list[ResearchStep] = Field(default_factory=list)
    #: Every decision the planner returned, in order.
    decisions: list[PlanDecision] = Field(default_factory=list)
    #: Set only when the run paused on the user (`outcome == "input_required"`).
    awaiting: UserPrompt | None = None
    cancelled: bool = False
    #: Evidence ids the tools actually returned, in order, deduplicated.
    evidence_ids: list[str] = Field(default_factory=list)
    #: Steps that ran but need a person before the work can continue.
    unresolved_items: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    #: A bounded, redacted digest of the session for a model or a caller.
    context: dict[str, Any] = Field(default_factory=dict)


class CoreAgentRuntime:
    """The loop: decide, execute once through the registry, observe, repeat.

    Constructed over a `ToolRegistry` (the M1.1 surface; `build_agent_tools()`
    is the enforcing one) and an optional LLM used only by the planner, under
    the planner's own rules. The runtime itself never calls a model.
    """

    def __init__(self, registry: Any, llm: Any = None, *,
                 planner: Planner | None = None,
                 max_iterations: int = MAX_RUNTIME_ITERATIONS,
                 context_budget_chars: int = DEFAULT_CONTEXT_BUDGET_CHARS) -> None:
        self._registry = registry
        self._planner = planner if planner is not None else Planner(registry, llm)
        self.max_iterations = max(1, int(max_iterations))
        self.context_budget_chars = max(256, int(context_budget_chars))
        self._cancelled = False

    # -- public API ---------------------------------------------------------

    def cancel(self) -> None:
        """Ask the run to stop before its next execution.

        Checked between steps: a tool already running finishes and is
        observed honestly; nothing new starts afterwards.
        """
        self._cancelled = True

    def run(self, goal: str, *, identity_input: dict[str, Any] | None = None,
            smiles: str = "", document_id: str = "",
            wants_routes: bool = False, use_llm: bool | None = None,
            context: dict[str, Any] | None = None,
            approvals: list[str] | None = None,
            plan: Any = None,
            allow_model_revision: bool = False) -> RuntimeResult:
        """Plan (or adopt a plan) and run it to a terminal state or a pause.

        `plan` adopts an existing `ResearchPlan` instead of generating one --
        ids are assigned and alias tools canonicalized by `adopt_plan`.
        `approvals` seeds the session with steps the caller has already
        approved (an operator decision made before the run, recorded like any
        other approval). `allow_model_revision` follows the planner's rule:
        off by default so an ordinary run never pays for a model call.
        """
        self._cancelled = False
        context = dict(context or {})
        if identity_input:
            context.setdefault("identity_input", dict(identity_input))

        if plan is not None:
            adopted, warnings = self._planner.adopt_plan(plan.model_copy(deep=True))
            if not adopted.goal:
                adopted.goal = goal
            proposal = PlanProposal(plan=adopted, source="adopted",
                                    warnings=list(warnings))
        else:
            proposal = self._planner.initial_plan(
                goal, identity_input=identity_input, smiles=smiles,
                document_id=document_id, wants_routes=wants_routes,
                use_llm=use_llm, context=context)

        session = PlanSession(plan=proposal.plan, context=context)
        for step_id in approvals or []:
            session.approve(step_id)
        self._sync_facts(session)

        diagnostics = [f"plan source: {proposal.source}"]
        diagnostics.extend(proposal.warnings)
        decision = self._planner.start(session)
        return self._drive(session, decision, steps=[], decisions=[],
                           evidence=[], diagnostics=diagnostics,
                           allow_model_revision=allow_model_revision)

    def resume(self, result: RuntimeResult, *, approve_step_id: str = "",
               answers: dict[str, Any] | None = None) -> RuntimeResult:
        """Continue a paused run with what the user supplied.

        `approve_step_id` records an approval for a step; `answers` are merged
        into the session context so the planner can complete arguments. The
        planner re-guards the pending step against the new state; if it still
        needs input, the run pauses again -- `resume` never loops and never
        guesses.
        """
        if result.awaiting is None:
            raise ValueError(
                f"只有等待用户输入的运行可以恢复（当前 outcome: {result.outcome}）")
        session = result.session
        if approve_step_id:
            session.approve(approve_step_id)
        if answers:
            session.context.update(answers)
        self._cancelled = False
        decision = self._planner.decide(session)
        return self._drive(session, decision,
                           steps=list(result.steps),
                           decisions=list(result.decisions),
                           evidence=list(result.evidence_ids),
                           diagnostics=list(result.diagnostics),
                           allow_model_revision=False)

    # -- the loop -----------------------------------------------------------

    def _drive(self, session: PlanSession, decision: PlanDecision, *,
               steps: list[ResearchStep], decisions: list[PlanDecision],
               evidence: list[str], diagnostics: list[str],
               allow_model_revision: bool) -> RuntimeResult:
        while True:
            decisions.append(decision)
            self._sync_facts(session)

            if self._cancelled:
                return self._assemble(session, steps, decisions, evidence,
                                      diagnostics, decision, cancelled=True)
            if len(decisions) > self.max_iterations:
                diagnostics.append(
                    f"runtime iteration budget exhausted "
                    f"({self.max_iterations})")
                return self._assemble(
                    session, steps, decisions, evidence, diagnostics,
                    self._budget_decision(
                        session, PlanDecisionKind.BLOCKED,
                        f"runtime 迭代预算耗尽（{self.max_iterations}）"))

            if decision.kind is PlanDecisionKind.ASK_USER:
                return self._await(session, steps, decisions, evidence,
                                   diagnostics, decision)
            if decision.kind is not PlanDecisionKind.CONTINUE:
                # COMPLETE or BLOCKED: the run is over.
                return self._assemble(session, steps, decisions, evidence,
                                      diagnostics, decision)

            step = self._step(session, decision.next_step_id)
            if step is None:
                return self._assemble(
                    session, steps, decisions, evidence,
                    diagnostics + [f"planner 指向了不存在的步骤 "
                                   f"{decision.next_step_id!r}"],
                    self._budget_decision(session, PlanDecisionKind.BLOCKED,
                                          "计划指向不存在的步骤"))
            if session.done(step.step_id):
                return self._assemble(
                    session, steps, decisions, evidence,
                    diagnostics + [f"运行时拒绝重复执行已完成步骤 {step.step_id}"],
                    self._budget_decision(
                        session, PlanDecisionKind.BLOCKED,
                        f"步骤 {step.step_id} 已执行过，不重复执行"))

            if not step.tool:
                if step.action is ResearchPlanAction.REPORT:
                    steps.append(ResearchStep(
                        step_id=step.step_id, index=len(steps),
                        action=ActionKind.FINAL,
                        note="报告步骤：运行时在此汇总轨迹；报告生成属于报告层"))
                    return self._assemble(
                        session, steps, decisions, evidence, diagnostics,
                        decision, completed_at_report=True,
                        reason_override="计划中的可执行步骤已全部执行；报告"
                                        "步骤属于报告层，运行时在此汇总轨迹")
                return self._assemble(
                    session, steps, decisions, evidence,
                    diagnostics + [f"步骤 {step.step_id} 没有可执行的工具"],
                    self._budget_decision(
                        session, PlanDecisionKind.BLOCKED,
                        f"步骤 {step.step_id} 没有可执行的工具，运行时不会"
                        f"跳过它假装完成"))

            # Execute exactly once, through the registry, with the approval
            # the session records -- never more, never approved by default.
            entry = _trajectory_entry(steps, step)
            result, observation = self._execute(session, step)
            entry.result_status = result.status
            entry.error = result.error
            for evidence_id in observation.evidence_ids:
                if evidence_id not in evidence:
                    evidence.append(evidence_id)

            decision = self._planner.decide(
                session, step.step_id, observation,
                allow_model_revision=allow_model_revision)

    def _execute(self, session: PlanSession,
                 step: Any) -> tuple[ToolResult, Observation]:
        approved = step.step_id in session.approvals
        result = self._registry.execute(step.tool, dict(step.arguments),
                                        step_id=step.step_id,
                                        approved=approved)
        observation = observe_from_registry(self._registry, step.tool, result)
        if not observation.step_id:
            observation.step_id = step.step_id
        self._sync_facts(session)
        return result, observation

    # -- assembly -----------------------------------------------------------

    def _await(self, session: PlanSession, steps: list[ResearchStep],
               decisions: list[PlanDecision], evidence: list[str],
               diagnostics: list[str],
               decision: PlanDecision) -> RuntimeResult:
        """A pause: record what the user must supply, keep everything else."""
        step = self._step(session, decision.next_step_id)
        if (decision.next_step_id
                and not any(s.step_id == decision.next_step_id for s in steps)):
            # The step the planner is holding has not run; say so rather than
            # leaving a hole in the trajectory.
            steps.append(ResearchStep(
                step_id=decision.next_step_id, index=len(steps),
                action=ActionKind.TOOL_CALL,
                tool=step.tool if step is not None else None,
                arguments=dict(step.arguments) if step is not None else {},
                result_status=None,
                note="未执行：等待用户输入"))
        prompt = UserPrompt(
            step_id=decision.next_step_id or decision.executed_step_id,
            question=decision.reason,
            missing_requirements=list(decision.missing_requirements),
            requires_approval=decision.requires_approval,
        )
        return self._assemble(session, steps, decisions, evidence, diagnostics,
                              decision, awaiting=prompt)

    def _assemble(self, session: PlanSession, steps: list[ResearchStep],
                  decisions: list[PlanDecision], evidence: list[str],
                  diagnostics: list[str], decision: PlanDecision, *,
                  awaiting: UserPrompt | None = None,
                  cancelled: bool = False,
                  completed_at_report: bool = False,
                  reason_override: str = "") -> RuntimeResult:
        """Derive the run's status from what was recorded -- never from hope.

        The mapping reuses `TaskStatus` and the report's outcome vocabulary.
        A hard failure anywhere rules out `completed`, mirroring the
        planner's own final-decision rule. Stopping at the plan's report step
        counts as finishing the executable work -- the report *document* is
        the report layer's job, not an execution step.
        """
        kinds = list(session.outcomes.values())
        hard = sorted(sid for sid, kind in session.outcomes.items()
                      if kind in _HARD_FAILURES)
        has_results = any(kind in (ObservationKind.SUCCESS, ObservationKind.PARTIAL)
                          for kind in kinds)
        misses = sum(1 for kind in kinds if kind is ObservationKind.NO_RESULT)
        partial = any(kind is ObservationKind.PARTIAL for kind in kinds)
        finished = ((decision.kind is PlanDecisionKind.COMPLETE
                     or completed_at_report) and not hard)

        if awaiting is not None:
            status, outcome = TaskStatus.RUNNING, "input_required"
            reason = decision.reason
        elif cancelled:
            status = (TaskStatus.PARTIAL if session.outcomes
                      else TaskStatus.FAILED)
            outcome = "partial" if status is TaskStatus.PARTIAL else "failed"
            reason = (f"调用方取消；已执行 {len(session.outcomes)} 步。"
                      f"{decision.reason}")
        elif finished:
            if partial:
                status, outcome = TaskStatus.PARTIAL, "partial"
            elif not has_results and misses:
                status, outcome = TaskStatus.COMPLETED, "no_results"
            else:
                status, outcome = TaskStatus.COMPLETED, "completed"
            reason = reason_override or decision.reason
        else:
            # BLOCKED, a CONTINUE that stopped short, or a COMPLETE that hard
            # failures contradict: report what exists, never a clean finish.
            status = TaskStatus.PARTIAL if has_results else TaskStatus.FAILED
            outcome = "partial" if has_results else "failed"
            reason = reason_override or decision.reason
            if hard:
                reason = (f"{reason}；未成功的步骤：{', '.join(hard)}")

        if status is TaskStatus.COMPLETED and outcome == "no_results":
            reason = f"{reason}；全部查询完成但无命中（这是结果，不是失败）"

        return RuntimeResult(
            status=status,
            outcome=outcome,
            reason=reason,
            decision=decision,
            session=session,
            steps=steps,
            decisions=decisions,
            awaiting=awaiting,
            cancelled=cancelled,
            evidence_ids=evidence,
            unresolved_items=_unresolved(session),
            diagnostics=diagnostics,
            context=self._bounded_context(session),
        )

    @staticmethod
    def _budget_decision(session: PlanSession, kind: PlanDecisionKind,
                         reason: str) -> PlanDecision:
        """A terminal decision the runtime itself makes (never a plan edit)."""
        return PlanDecision(kind=kind, reason=reason,
                            revision=session.plan.revision,
                            decisions_used=session.decisions)

    # -- state --------------------------------------------------------------

    def _sync_facts(self, session: PlanSession) -> None:
        """Merge the registry's facts into the session's.

        The registry is the authority on what tools established (it records
        `sets_facts` / `fact_rule` on an OK result); the session is the
        planner's view. One-directional on purpose: a fact the caller seeded
        into the session never silently widens the registry's guard.
        """
        facts = getattr(self._registry, "facts", None)
        if facts is None:
            return
        try:
            names = facts.as_list()
        except Exception:  # noqa: BLE001 - a facts object without as_list
            return
        for name in names:
            if name and name not in session.facts:
                session.facts.append(name)

    @staticmethod
    def _step(session: PlanSession, step_id: str) -> Any | None:
        if not step_id:
            return None
        for step in session.plan.steps:
            if step.step_id == step_id:
                return step
        return None

    def _bounded_context(self, session: PlanSession) -> dict[str, Any]:
        """The run's history, bounded by the aggregate budget.

        Uses the registry's own truncation vocabulary (`truncated`,
        `original_chars`) so a shortened digest is declared, never silent.
        """
        payload: dict[str, Any] = {
            "goal": session.plan.goal,
            "revision": session.plan.revision,
            "facts": list(session.facts),
            "notes": list(session.notes),
            "steps": [
                {"step_id": step_id, "tool": observation.tool,
                 "kind": observation.kind.value,
                 "summary": observation.summary, "data": observation.data}
                for step_id, observation in session.observations.items()
            ],
        }
        bounded, truncated, _original = _bound_output(
            payload, self.context_budget_chars)
        if not isinstance(bounded, dict):  # pragma: no cover - defensive
            return {"truncated": True}
        return bounded


def _trajectory_entry(steps: list[ResearchStep], step: Any) -> ResearchStep:
    """The trajectory entry for a step about to run.

    A step that was paused on the user already has a not-executed marker in
    the trajectory; resuming reuses that entry instead of appending a second
    one for the same step.
    """
    for candidate in reversed(steps):
        if candidate.step_id == step.step_id and candidate.result_status is None:
            candidate.tool = step.tool
            candidate.arguments = dict(step.arguments)
            candidate.note = ""
            return candidate
    entry = ResearchStep(
        step_id=step.step_id, index=len(steps), action=ActionKind.TOOL_CALL,
        tool=step.tool, arguments=dict(step.arguments))
    steps.append(entry)
    return entry


def _unresolved(session: PlanSession) -> list[str]:
    """The steps a person has to act on, in the agents' own phrasing."""
    items: list[str] = []
    for observation in session.observations.values():
        if observation.kind is not ObservationKind.NEEDS_INPUT:
            continue
        missing = "、".join(observation.missing_requirements)
        item = (f"{observation.tool}: 需要补充信息（{missing}）" if missing
                else f"{observation.tool}: 需要补充信息")
        if item not in items:
            items.append(item)
    return items


__all__ = [
    "CoreAgentRuntime",
    "DEFAULT_CONTEXT_BUDGET_CHARS",
    "MAX_RUNTIME_ITERATIONS",
    "RuntimeResult",
    "UserPrompt",
]

"""The AI Research Intelligence behind the runtime's own interface (M2.0).

`CoreAgentRuntime` is the M1.4 loop: a deterministic plan driven to the
end, optionally touched by a model *inside the planner*. The M2.0
intelligence is a different loop -- the model decides every next move --
and it does not replace the runtime: a golden-case blind run, a scripted
test and a deterministic offline workflow still want the old one.

What this module adds is the seam between them. `AiResearchRuntime` speaks
the runtime's exact interface (`run(...) -> RuntimeResult`) and drives
`ResearchIntelligence` underneath, so every existing caller of the runtime
-- `run_blind_case` above all -- can run a case through the model-driven
loop without learning a second result vocabulary.

The mapping is a translation, not a re-reading: the intelligence's own
decisions, interpretations and audit records are *not* flattened into the
`RuntimeResult`. A caller that wants them asks the intelligence directly.
What travels here is the execution trajectory, the evidence ids, the
pause, and the honest stop reason -- the same things the M1.4 runtime
reports, produced by a different decision process.
"""

from __future__ import annotations

from typing import Any

from crea.agent.intelligence import (
    IntelligenceRunResult,
    ResearchIntelligence,
)
from crea.agent.planner import PlanDecision, PlanDecisionKind, PlanSession
from crea.agent.runtime import RuntimeResult, UserPrompt
from crea.core.models.knowledge import ResearchPlan
from crea.core.models.research import TaskStatus
from crea.core.models.study_context import HypothesisStatus


class AiResearchRuntime:
    """`ResearchIntelligence` under the `CoreAgentRuntime` interface.

    Holds one intelligence instance. Constructed with the same registry
    and LLM the deterministic runtime would take, plus the optional study
    context. `run(...)` accepts the runtime's own keyword set; the
    structure-oriented keywords (`smiles`, `wants_routes`,
    `identity_input`) are handed to the intelligence as context, because
    in an AI-driven run the model decides whether to use them.
    """

    def __init__(self, registry: Any, llm: Any = None, *,
                 intelligence: ResearchIntelligence | None = None,
                 **kwargs: Any) -> None:
        self._intelligence = intelligence or ResearchIntelligence(
            registry, llm, **kwargs)
        #: The last intelligence result, for callers that want the full
        #: record (`decision_records`, interpretations, token usage).
        self.last: IntelligenceRunResult | None = None

    @property
    def intelligence(self) -> ResearchIntelligence:
        return self._intelligence

    def cancel(self) -> None:
        self._intelligence.cancel()

    def run(self, goal: str, *, identity_input: dict[str, Any] | None = None,
            smiles: str = "", document_id: str = "",
            wants_routes: bool = False, use_llm: bool | None = None,
            context: dict[str, Any] | None = None,
            approvals: list[str] | None = None,
            plan: Any = None,
            allow_model_revision: bool = False,
            session_id: str = "") -> RuntimeResult:
        merged: dict[str, Any] = dict(context or {})
        if identity_input:
            merged["identity_input"] = dict(identity_input)
        if smiles:
            merged["caller_supplied_smiles"] = smiles
        if document_id:
            merged["document_id"] = document_id
        if wants_routes:
            merged["wants_routes"] = True
        if plan is not None and isinstance(plan, ResearchPlan) and plan.goal:
            merged.setdefault("caller_supplied_plan_goal", plan.goal)

        result = self._intelligence.run(
            goal, session_id=session_id, approvals=approvals,
            context=merged, allow_model=use_llm is not False)
        self.last = result
        return _to_runtime_result(result, goal)


def _to_runtime_result(result: IntelligenceRunResult,
                       goal: str) -> RuntimeResult:
    """The intelligence's report, in runtime clothing. Nothing is smoothed.

    A paused run stays a pause (`input_required` with an `awaiting`
    prompt); a run that stopped on a budget stays stopped and says so; and
    the hypothesis statuses the model asked for are never reported as
    facts -- `SUPPORTED` was the study context's decision, not ours.
    """
    plan = ResearchPlan(goal=goal, steps=[])
    session = PlanSession(plan=plan)
    for observation in result.observations:
        session.observations[observation.step_id] = observation
        session.outcomes[observation.step_id] = observation.kind
        session.executed_calls.append(
            f"{observation.tool}|{observation.step_id}")

    decisions = [
        PlanDecision(kind=_kind_of(record), reason=record.decision_summary,
                     executed_step_id=record.associated_step_id,
                     revision=index)
        for index, record in enumerate(result.decision_records, start=1)
        if record.validation_result.value == "accepted"
    ]
    last = decisions[-1] if decisions else PlanDecision(
        kind=PlanDecisionKind.BLOCKED, reason=result.reason)

    awaiting = None
    if result.awaiting is not None:
        awaiting = UserPrompt(
            step_id="",
            question=(f"{result.awaiting.question}"
                      + (f"\n原因：{result.awaiting.why}"
                         if result.awaiting.why else "")
                      + (f"\n可选项：{'；'.join(result.awaiting.options)}"
                         if result.awaiting.options else "")),
            missing_requirements=[result.awaiting.expected_information]
            if result.awaiting.expected_information else [],
            requires_approval=False)

    return RuntimeResult(
        status=result.status if result.status is not TaskStatus.PENDING
        else TaskStatus.COMPLETED,
        outcome=result.outcome,
        session_id=result.session_id,
        reason=result.reason,
        decision=last,
        session=session,
        steps=list(result.steps),
        decisions=decisions,
        awaiting=awaiting,
        cancelled=result.cancelled,
        evidence_ids=list(result.evidence_ids),
        unresolved_items=list(result.unresolved_items),
        diagnostics=list(result.diagnostics) + [
            f"mode: {result.mode.value}",
            f"decisions={result.decisions_used} tool_calls="
            f"{result.tool_calls_used} model_calls={result.model_calls_used}",
        ],
        context={
            "goal": result.goal,
            "mode": result.mode.value,
            "hypotheses": [
                {"hypothesis_id": hid} for hid in result.hypothesis_ids],
            "evidence_ids": list(result.evidence_ids),
            "interpretations": [
                {"step_id": item.step_id, "tool": item.tool,
                 "interpretation": item.interpretation}
                for item in result.interpretations],
        },
    )


def _kind_of(record: Any) -> PlanDecisionKind:
    """An accepted intelligence decision, in the runtime's four kinds."""
    decision_type = getattr(record, "decision_type", None)
    if decision_type is None:
        return PlanDecisionKind.BLOCKED
    from crea.core.models.research_decision import DecisionKind
    if decision_type is DecisionKind.ASK_USER:
        return PlanDecisionKind.ASK_USER
    if decision_type is DecisionKind.COMPLETE:
        return PlanDecisionKind.COMPLETE
    if decision_type is DecisionKind.BLOCKED:
        return PlanDecisionKind.BLOCKED
    return PlanDecisionKind.CONTINUE


#: Kept so a reader of this module can see the hypothesis vocabulary is the
#: study context's, not a new one invented for the intelligence.
_HYPOTHESIS_STATUSES = tuple(status.value for status in HypothesisStatus)


__all__ = ["AiResearchRuntime"]

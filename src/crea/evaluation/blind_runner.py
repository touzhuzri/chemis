"""M1.7.2 -- the blind research runner: agent view in, trace out.

The isolation this module is built around: `BlindResearchRunner` never
holds a store, never sees a `GoldenCase`, and cannot reach a reference
because it is never handed one. Its input is a `GoldenCaseAgentView` --
the projection whose fields *are* the visible half -- and its outputs
are a `RuntimeResult` and the `ResearchTrace` assembled from it. The
only object that holds both the runner and the store is the module-level
`run_blind_case` function, and it touches the store through exactly two
named doors: `get_agent_view` in, `save_trace` out.

What a run records, so the trace can answer "why did it conclude that":
the case_id and the case_version **the view froze at run time** (a later
case edit never re-labels this run), the research session id, the plan
and its revision, every executed step with its observation kind, the
person inputs the run received, every re-planning decision, the evidence
the tools actually returned, and the environment the caller declared
(model, tool and data-source versions, the CREA commit). The runner
records what the runtime reports; it computes nothing and infers
nothing.

The normal research entry points -- `CoreAgentRuntime.run` called
directly, `ResearchWorkflow` -- are untouched. This is a new caller of
the same runtime, not a change to it.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, Field

from crea.agent.runtime import CoreAgentRuntime, RuntimeResult
from crea.core.errors import GoldenCaseError, ResearchStudyError
from crea.core.ids import content_hash
from crea.core.models.golden_case import (
    ResearchTrace,
    TraceModelInfo,
    TracePlanChange,
    TraceToolCall,
    TraceToolVersion,
    TraceDataSourceVersion,
    TraceUserInput,
)
from crea.core.models.golden_case_access import GoldenCaseAgentView
from crea.core.provenance import utc_now_iso
from crea.services.golden_case_store import GoldenCaseStore
from crea.services.input_understanding import InputUnderstandingService


class RunEnvironment(BaseModel):
    """What the caller declares about the run's own execution context.

    Declared, never guessed: the runner knows which model or data
    sources took part only if the caller says so. An empty environment
    is recorded as empty -- not as "unknown was fine".
    """

    models: list[TraceModelInfo] = Field(default_factory=list)
    tools: list[TraceToolVersion] = Field(default_factory=list)
    data_sources: list[TraceDataSourceVersion] = Field(default_factory=list)
    git_commit: str = ""


class BlindRunOutcome(BaseModel):
    """One blind run: the runtime's own result, and its trace."""

    result: RuntimeResult
    trace: ResearchTrace


class BlindResearchRunner:
    """Run one agent view through a fresh `CoreAgentRuntime`.

    Constructed with a runtime *factory* rather than a runtime: each run
    gets its own runtime, so one case's facts, approvals and session
    state can never bleed into the next case's run.
    """

    def __init__(self, runtime_factory: Callable[[], CoreAgentRuntime]) -> None:
        self._runtime_factory = runtime_factory

    def run(self, view: GoldenCaseAgentView, *,
            environment: RunEnvironment | None = None,
            session_id: str = "",
            use_llm: bool | None = None,
            ) -> BlindRunOutcome:
        """Run the case's visible half to a result and a trace.

        The trace is built here but NOT saved: this object holds no
        store. `run_blind_case` saves it through the store's named door.
        """
        environment = environment or RunEnvironment()
        started_at = utc_now_iso()
        runtime = self._runtime_factory()
        try:
            # Both halves of the input path refuse loudly: an inconsistent
            # intent never reaches the planner, and a refused run never
            # becomes a half-trace.
            planner_inputs = self._planner_inputs(view)
            result = runtime.run(
                planner_inputs.pop("goal"),
                session_id=session_id,
                use_llm=use_llm,
                **planner_inputs,
            )
        except ResearchStudyError as exc:
            raise GoldenCaseError(
                "CASE_INPUT_INVALID",
                f"golden case {view.case_id!r} has an input the runtime "
                f"refused: {exc}") from exc
        ended_at = utc_now_iso()

        trace = self._build_trace(view, result, environment,
                                  started_at, ended_at)
        return BlindRunOutcome(result=result, trace=trace)

    # -- input adaptation ---------------------------------------------------

    def _planner_inputs(self, view: GoldenCaseAgentView) -> dict[str, Any]:
        """The agent view's visible half, in the runtime's own terms.

        A structured intent travels through M1.6's validated bridge --
        the same path a live user request takes, including its refusal
        of an inconsistent intent. Without an intent, the goal travels
        as the goal, and a case-author-supplied SMILES travels as the
        caller-supplied structure the runtime already accepts for
        deterministic computation -- never as a resolved identity.
        """
        blind = view.blind_input
        if blind.intent is not None:
            inputs = InputUnderstandingService().to_planner_inputs(
                blind.intent)
        else:
            inputs = {"goal": blind.goal, "identity_input": None,
                      "smiles": "", "wants_routes": False,
                      "document_id": "", "context": {}}
            target = blind.target_structure
            if target is not None and target.kind.value == "smiles":
                inputs["smiles"] = target.value
        inputs["context"] = {
            **(inputs.get("context") or {}),
            "golden_case_id": view.case_id,
            "golden_case_version": view.case_version,
            "golden_case_type": view.case_type.value,
        }
        inputs["goal"] = blind.goal or inputs["goal"]
        return inputs

    # -- trace assembly -----------------------------------------------------

    def _build_trace(self, view: GoldenCaseAgentView, result: RuntimeResult,
                     environment: RunEnvironment,
                     started_at: str, ended_at: str) -> ResearchTrace:
        """The runtime's report, in trace clothing. Nothing is added and
        nothing is smoothed over: a failed run traces as failed."""
        outcomes = result.session.outcomes
        tool_calls = [
            TraceToolCall(
                step_id=step.step_id,
                tool=step.tool or "",
                observation_kind=(outcomes[step.step_id].value
                                  if step.step_id in outcomes else ""),
                summary=step.note or step.error or "",
                at=step.at,
            )
            for step in result.steps if step.tool
        ]
        plan_changes: list[TracePlanChange] = []
        for decision in result.decisions:
            kinds = [change.kind.value for change in decision.changes]
            if not kinds:
                continue
            plan_changes.append(TracePlanChange(
                plan_revision=decision.revision,
                change_kinds=kinds,
                reason=decision.reason,
            ))
        warnings = list(result.unresolved_items) + list(result.diagnostics)
        if result.awaiting is not None:
            warnings.append(
                f"run paused awaiting user input at step "
                f"{result.awaiting.step_id!r}: {result.awaiting.question}")
        failures = [result.reason] if result.outcome == "failed" else []

        trace_id = "trace-" + content_hash(
            f"{view.case_id}|{result.session_id}|{started_at}", 16)
        return ResearchTrace(
            trace_id=trace_id,
            case_id=view.case_id,
            # The version the VIEW froze -- not the case's current one.
            case_version=view.case_version,
            session_id=result.session_id,
            started_at=started_at,
            ended_at=ended_at,
            status=result.status,
            models=list(environment.models),
            tools=list(environment.tools),
            data_sources=list(environment.data_sources),
            git_commit=environment.git_commit,
            plan=result.session.plan,
            plan_revision=result.session.revisions,
            steps=list(result.steps),
            tool_calls=tool_calls,
            user_inputs=self._user_inputs(result),
            plan_changes=plan_changes,
            evidence_ids=list(result.evidence_ids),
            warnings=warnings,
            failures=failures,
        )

    @staticmethod
    def _user_inputs(result: RuntimeResult) -> list[TraceUserInput]:
        """Person inputs this run actually received.

        A pause is not an input: an unanswered question goes to warnings
        (it says why the run stopped), never here dressed up as an
        answer the person never gave. Approvals the session recorded are
        the person's, relayed by the caller through `approvals`; the
        runtime keeps them on the session, and they are recorded here
        verbatim, never paraphrased.
        """
        return [TraceUserInput(step_id=step_id, kind="approval",
                               answer="approved")
                for step_id in result.session.approvals]


def run_blind_case(store: GoldenCaseStore, case_id: str, *,
                   runtime_factory: Callable[[], CoreAgentRuntime],
                   environment: RunEnvironment | None = None,
                   session_id: str = "",
                   use_llm: bool | None = None,
                   ) -> BlindRunOutcome:
    """The one orchestration path: agent view in, trace saved out.

    This function -- and only this function -- holds both the store and
    the runner. It touches the store through two named doors:
    `get_agent_view` (what the run may see) and `save_trace` (what the
    run produced). It never calls `get`, `get_manager_view`, or
    anything on the unseal side: a blind run cannot ask for the answer
    key, and the tests pin exactly which store methods this path uses.
    """
    view = store.get_agent_view(case_id)
    runner = BlindResearchRunner(runtime_factory)
    outcome = runner.run(view, environment=environment,
                         session_id=session_id, use_llm=use_llm)
    store.save_trace(outcome.trace)
    return outcome


__all__ = [
    "BlindResearchRunner",
    "BlindRunOutcome",
    "RunEnvironment",
    "run_blind_case",
]

"""M2.0 -- the AI Research Intelligence: one decision loop, model-driven.

This module is the research *decision* centre. It replaces the three
pre-M2.0 loops that each invented their own reply shape and stop contract
(`ResearchAgent`, `ChemistryAgent`, `EvidenceAgent`) with a single loop
whose moves are the `ResearchDecision` protocol in
`crea.core.models.research_decision`.

The loop, in full:

    understand (M1.6) -> assemble ResearchState -> LLM -> ResearchDecision
        -> DecisionValidator -> ToolRegistry.execute -> Observation
        -> scientific interpretation -> StudyContext update -> reflection
        -> next decision ... until COMPLETE / ASK_USER / BLOCKED / budget.

Five boundaries it holds, each pinned by a test:

* **The model proposes; the registry decides.** A decision names a tool and
  a JSON object. The validator checks the tool is registered, canonical,
  and that the arguments pass the tool's own schema; the registry then
  re-validates, applies the execution policy (facts, network, model,
  approval) and executes. There is no second execution path, and a model
  cannot grant its own approval.
* **Nothing the model says becomes a fact.** Facts enter `StudyContext`
  only through what a tool declared (`sets_facts` / `fact_rule`) with
  `TOOL_VERIFIED`; a model's hypothesis enters as `proposed` and
  `SUPPORTED` still needs named evidence ids the study context checks.
* **The loop is bounded for real.** Decisions, tool calls, model calls,
  wall clock, consecutive no-progress turns, repeated identical calls and
  parse-repair attempts are all charged against a `ResearchBudget` that is
  checked on the execution path, not merely configured.
* **A dead model is a reported dead model.** A provider failure surfaces as
  `MODEL_UNAVAILABLE` and stops the run. There is no silent fallback to a
  deterministic pipeline that then reports an "AI research" result; a
  caller may opt into a declared fallback, and the run's `mode` says which
  one ran.
* **The run is auditable.** Every turn produces a `DecisionRecord` with the
  model, prompt and schema versions, the validator's ruling, timing and
  token usage, and every tool call produces a `ResearchStep` plus a
  `ScientificInterpretation`. No hidden reasoning is stored.
"""

from __future__ import annotations

import json
import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from crea.agent.intelligence_prompt import (
    SYSTEM,
    build_user_message,
    parse_decision,
    repair_message,
)
from crea.agent.observer import Observation, ObservationKind, observe_from_registry
from crea.core.errors import CreaError
from crea.core.ids import content_hash
from crea.core.models.knowledge import ResearchPlan, ResearchPlanStep
from crea.core.models.research import (
    ActionKind,
    ResearchStep,
    TaskStatus,
    ToolResult,
)
from crea.core.models.research_decision import (
    INTELLIGENCE_PROMPT_VERSION,
    BlockedStatement,
    DecisionKind,
    DecisionOutcome,
    DecisionRecord,
    ResearchDecision,
    ScientificInterpretation,
)
from crea.core.models.research_intent import (
    EntityIdentityStatus,
    ResearchIntent,
)
from crea.core.models.study_context import (
    FactVerification,
    HypothesisStatus,
    StudySessionSummary,
)
from crea.core.provenance import utc_now_iso
from crea.llm.base import LLMProvider, LLMRequest

#: A defence-in-depth ceiling on loop iterations. The budget is the real
#: bound; this exists so a budget bug cannot spin.
MAX_INTELLIGENCE_ITERATIONS = 64


class ResearchMode(str, Enum):
    """Which loop actually ran. Recorded on every result, never guessed.

    A run that claims to be AI-driven but quietly fell back to a fixed
    pipeline is the failure this enum exists to prevent.
    """

    AI_RESEARCH_INTELLIGENCE = "ai_research_intelligence"
    #: The caller opted in AND the model died. The run says so.
    DETERMINISTIC_FALLBACK = "deterministic_fallback"


class ResearchBudget(BaseModel):
    """Every limit of one run, checked on the execution path.

    Each field is a real bound: the loop charges it and stops when it is
    spent. A budget that is only configuration is not a budget.
    """

    model_config = ConfigDict(extra="forbid")

    #: Decisions the model may return (including rejected ones).
    max_decisions: int = 14
    #: Tool calls that may execute.
    max_tool_calls: int = 20
    #: Model round trips, including parse-repair requests.
    max_model_calls: int = 24
    #: Identical tool+arguments calls the loop will refuse.
    max_identical_calls: int = 1
    #: Consecutive turns that changed nothing (no new tool call, no new
    #: evidence, no hypothesis move) before the run declares itself stuck.
    max_consecutive_no_progress: int = 3
    #: Parse-repair requests after an unusable reply.
    max_fix_attempts: int = 1
    #: Wall-clock seconds for the whole run.
    max_wall_clock_seconds: float = 900.0
    #: Per model call, in seconds. The provider's own timeout applies too;
    #: this is the loop's own bound.
    model_call_timeout_seconds: float = 180.0
    #: Per tool call, in seconds. Reported after the fact when the tool
    #: cannot be interrupted -- the loop never claims it stopped a call.
    max_tool_call_seconds: float = 120.0
    #: Output ceiling for one model call, when the provider honours it.
    max_output_tokens: int | None = 2000

    @field_validator(
        "max_decisions", "max_tool_calls", "max_model_calls",
        "max_identical_calls", "max_consecutive_no_progress",
        "max_fix_attempts",
        mode="before",
    )
    @classmethod
    def _positive(cls, value: Any) -> Any:
        value = int(value)
        if value < 1:
            raise ValueError("a research budget bound must be at least 1")
        return value

    def exhausted(self, *, decisions: int, tool_calls: int,
                  model_calls: int, elapsed: float) -> str:
        """Why the run must stop now, or "" when it may continue."""
        if decisions >= self.max_decisions:
            return (f"决策轮数预算耗尽（{self.max_decisions}）")
        if tool_calls >= self.max_tool_calls:
            return f"工具调用预算耗尽（{self.max_tool_calls}）"
        if model_calls >= self.max_model_calls:
            return f"模型调用预算耗尽（{self.max_model_calls}）"
        if elapsed >= self.max_wall_clock_seconds:
            return (f"运行时间预算耗尽（{self.max_wall_clock_seconds:.0f}s）")
        return ""

    def note(self, *, decisions: int, tool_calls: int,
             model_calls: int) -> str:
        """A one-line remaining-budget note for the prompt."""
        return (f"剩余决策 {max(0, self.max_decisions - decisions)}，"
                f"剩余工具调用 {max(0, self.max_tool_calls - tool_calls)}，"
                f"剩余模型调用 {max(0, self.max_model_calls - model_calls)}")


class IntelligenceAwaiting(BaseModel):
    """What a paused run needs from a person, in the model's own words."""

    question: str
    why: str = ""
    expected_information: str = ""
    options: list[str] = Field(default_factory=list)


class IntelligenceRunResult(BaseModel):
    """One run: what it decided, what it ran, what it ended as.

    `status`/`outcome` reuse `TaskStatus` and the runtime's outcome strings
    so a caller never learns a second vocabulary.
    """

    status: TaskStatus
    #: "completed" | "partial" | "input_required" | "no_results" | "failed"
    outcome: str
    session_id: str
    study_id: str = ""
    mode: ResearchMode = ResearchMode.AI_RESEARCH_INTELLIGENCE
    #: Why the run stopped, in one sentence.
    reason: str
    goal: str
    started_at: str
    ended_at: str = ""
    duration_ms: int = 0
    #: Every turn, accepted or not. This is the audit trail.
    decision_records: list[DecisionRecord] = Field(default_factory=list)
    #: The accepted decisions only, in order.
    decisions: list[ResearchDecision] = Field(default_factory=list)
    #: The execution trajectory.
    steps: list[ResearchStep] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    interpretations: list[ScientificInterpretation] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    #: Set only when the run paused on the person.
    awaiting: IntelligenceAwaiting | None = None
    cancelled: bool = False
    evidence_ids: list[str] = Field(default_factory=list)
    #: Hypotheses this run created or moved, by id.
    hypothesis_ids: list[str] = Field(default_factory=list)
    #: A person question the run could not settle, and every stop reason.
    unresolved_items: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    #: Model, tool and prompt versions actually seen, for the trace.
    model_provider: str = ""
    model_name: str = ""
    prompt_version: str = INTELLIGENCE_PROMPT_VERSION
    budget: ResearchBudget = Field(default_factory=ResearchBudget)
    #: Budget actually consumed.
    decisions_used: int = 0
    tool_calls_used: int = 0
    model_calls_used: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    #: What the final decision said the research achieved and did not.
    completion: Any = None
    blocked: Any = None

    @property
    def stopped_for_person(self) -> bool:
        return self.awaiting is not None


class ResearchState:
    """Everything the model is shown about one run.

    Assembled fresh at every turn from the study context and this run's own
    observations, so the model works from the current state rather than a
    transcript of itself. `as_prompt_dict()` is the rendering contract.
    """

    def __init__(self, *, goal: str, session_id: str, study_id: str = "",
                 intent: ResearchIntent | None = None,
                 catalog: list[dict] | None = None) -> None:
        self.goal = goal
        self.session_id = session_id
        self.study_id = study_id
        self.intent = intent
        self.catalog = list(catalog or [])
        self.constraints: list[str] = []
        self.entities: list[dict] = []
        self.facts: list[dict] = []
        self.hypotheses: list[dict] = []
        self.evidence_ids: list[str] = []
        self.decisions: list[dict] = []
        self.issues: list[dict] = []
        self.observations: list[dict] = []
        self.executed_calls: list[str] = []
        self.direction: str = ""
        self.unsafe_entities: list[str] = []
        if intent is not None:
            self._absorb_intent(intent)

    def _absorb_intent(self, intent: ResearchIntent) -> None:
        self.constraints = [
            f"{c.kind}: {c.value}" for c in intent.constraints
            if not c.superseded_by
        ]
        for entity in intent.entities:
            self.entities.append({
                "name": entity.name,
                "kind": entity.kind,
                "value": entity.value,
                "identity_status": entity.identity_status.value,
                "role": entity.role.value,
                "candidates": len(entity.candidates),
                "structure_validated": entity.structure_validated,
            })
            # An identity that is not settled blocks every structure-
            # dependent tool. Named once here so the model does not have to
            # re-derive it, and enforced again by the registry's facts.
            if entity.identity_status not in (
                    EntityIdentityStatus.RESOLVED,
                    EntityIdentityStatus.STRUCTURE_VALIDATED):
                self.unsafe_entities.append(entity.name)
        if not self.direction:
            self.direction = intent.goal or intent.raw_input

    def note_observation(self, observation: Observation, *,
                         arguments: dict | None = None) -> None:
        """One tool result enters the state, as the observer read it."""
        self.observations.append({
            "step_id": observation.step_id,
            "tool": observation.tool,
            "kind": observation.kind.value,
            "summary": observation.summary,
            "missing_requirements": list(observation.missing_requirements),
            "evidence_ids": list(observation.evidence_ids),
            "data": _bounded(observation.data),
        })
        signature = _signature(observation.tool, arguments or {})
        if signature not in self.executed_calls:
            self.executed_calls.append(signature)
        for evidence_id in observation.evidence_ids:
            if evidence_id not in self.evidence_ids:
                self.evidence_ids.append(evidence_id)

    def call_already_ran(self, tool: str, arguments: dict) -> bool:
        return _signature(tool, arguments) in self.executed_calls

    def as_prompt_dict(self) -> dict:
        return {
            "research_goal": self.goal,
            "session_id": self.session_id,
            "study_id": self.study_id,
            "research_direction": self.direction,
            "constraints": self.constraints,
            "chemical_entities": self.entities,
            "entities_whose_structure_is_not_settled": self.unsafe_entities,
            "established_facts": self.facts,
            "hypotheses": self.hypotheses,
            "evidence_ids_held": self.evidence_ids,
            "decisions_already_taken": self.decisions,
            "open_issues": self.issues,
            "observations": self.observations,
            "tool_calls_already_made": self.executed_calls,
            "available_tools": self.catalog,
        }



# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ValidationOutcome(BaseModel):
    """The gate's ruling on one decision."""

    outcome: DecisionOutcome
    detail: str = ""
    issues: list[str] = Field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.outcome is DecisionOutcome.ACCEPTED


class DecisionValidator:
    """Checks a model decision against what is actually possible here.

    The validator decides what may be *attempted*; the registry still
    enforces the execution policy on the attempt. A rejection is always
    accompanied by issues the model can act on -- a gate that only says
    "no" teaches nothing and provokes a repeat.
    """

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    def validate(self, decision: ResearchDecision, state: ResearchState,
                 *, budget: ResearchBudget, tool_calls_used: int,
                 ) -> ValidationOutcome:
        if decision.kind is DecisionKind.CONTINUE_WITH_TOOL:
            return self._validate_tool(decision, state, budget=budget,
                                       tool_calls_used=tool_calls_used)
        if decision.kind is DecisionKind.UPDATE_HYPOTHESIS:
            return self._validate_hypothesis(decision, state)
        if decision.kind is DecisionKind.ASK_USER:
            if not (decision.ask_user and decision.ask_user.question.strip()):
                return ValidationOutcome(
                    outcome=DecisionOutcome.REJECTED,
                    detail="ask_user needs a question a person can answer",
                    issues=["ask_user.question 不能为空"])
            return ValidationOutcome(outcome=DecisionOutcome.ACCEPTED)
        if decision.kind is DecisionKind.UPDATE_DIRECTION:
            update = decision.direction_update
            assert update is not None
            if not update.new_direction.strip():
                return ValidationOutcome(
                    outcome=DecisionOutcome.REJECTED,
                    detail="a direction change needs a new direction",
                    issues=["direction_update.new_direction 不能为空"])
            return ValidationOutcome(outcome=DecisionOutcome.ACCEPTED)
        if decision.kind is DecisionKind.COMPLETE:
            completion = decision.completion
            assert completion is not None
            if not completion.completion_reason.strip():
                return ValidationOutcome(
                    outcome=DecisionOutcome.REJECTED,
                    detail="completion needs a reason",
                    issues=["completion.completion_reason 不能为空"])
            return ValidationOutcome(outcome=DecisionOutcome.ACCEPTED)
        blocked = decision.blocked
        assert blocked is not None
        if not blocked.reason.strip():
            return ValidationOutcome(
                outcome=DecisionOutcome.REJECTED,
                detail="blocked needs a reason",
                issues=["blocked.reason 不能为空"])
        return ValidationOutcome(outcome=DecisionOutcome.ACCEPTED)

    # -- tool calls ---------------------------------------------------------

    def _validate_tool(self, decision: ResearchDecision, state: ResearchState,
                       *, budget: ResearchBudget,
                       tool_calls_used: int) -> ValidationOutcome:
        call = decision.tool_call
        assert call is not None
        issues: list[str] = []

        if tool_calls_used >= budget.max_tool_calls:
            return ValidationOutcome(
                outcome=DecisionOutcome.REJECTED,
                detail="the tool-call budget is spent; complete or block "
                       "instead",
                issues=[f"工具调用预算已耗尽（{budget.max_tool_calls}）"])

        spec = self._registry.get(call.tool) if call.tool else None
        if spec is None:
            names = ", ".join(self._registry.names()[:24])
            return ValidationOutcome(
                outcome=DecisionOutcome.REJECTED,
                detail=f"{call.tool!r} is not a registered tool",
                issues=[f"未注册的工具 {call.tool!r}；可用工具：{names}"])
        if getattr(spec, "alias_of", None):
            return ValidationOutcome(
                outcome=DecisionOutcome.REJECTED,
                detail=f"{call.tool!r} is an alias of {spec.alias_of!r}",
                issues=[f"{call.tool!r} 是别名，请使用规范名 {spec.alias_of!r}"])
        if not call.decision_summary.strip():
            issues.append("tool_call.decision_summary 不能为空")
        if not call.purpose.strip():
            issues.append("tool_call.purpose 不能为空：必须说明这一步为什么要执行")
        if not call.expected_information.strip():
            issues.append("tool_call.expected_information 不能为空：必须说明预期获得什么信息")

        try:
            spec.arguments_model.model_validate(call.arguments or {})
        except Exception as exc:  # noqa: BLE001 - the refusal is the report
            issues.append(f"参数未通过 {call.tool} 的 Schema：{_short(exc)}")

        if state.call_already_ran(call.tool, call.arguments or {}):
            issues.append(
                f"完全相同的 {call.tool} 调用已经执行过；只有研究状态发生变化、"
                f"参数有明确改变或出现新证据时才允许再次尝试同一方向")

        for evidence_id in call.relevant_evidence_ids:
            if evidence_id not in state.evidence_ids:
                issues.append(
                    f"引用了本次研究未持有的 evidence_id {evidence_id!r}；"
                    f"只能引用已有的证据 id")
        known_hypotheses = {h.get("hypothesis_id") for h in state.hypotheses}
        for hypothesis_id in call.related_hypothesis_ids:
            if hypothesis_id not in known_hypotheses:
                issues.append(
                    f"引用了不存在的 hypothesis_id {hypothesis_id!r}")

        if issues:
            return ValidationOutcome(
                outcome=DecisionOutcome.REJECTED,
                detail="the tool call cannot run as written", issues=issues)
        return ValidationOutcome(
            outcome=DecisionOutcome.ACCEPTED,
            detail=f"{call.tool} 通过注册表与参数校验")

    # -- hypotheses ---------------------------------------------------------

    def _validate_hypothesis(self, decision: ResearchDecision,
                             state: ResearchState) -> ValidationOutcome:
        update = decision.hypothesis_update
        assert update is not None
        issues: list[str] = []
        if not update.hypothesis.strip():
            issues.append("hypothesis_update.hypothesis 不能为空")
        known = {h.get("hypothesis_id") for h in state.hypotheses}
        if update.hypothesis_id and update.hypothesis_id not in known:
            issues.append(
                f"hypothesis_id {update.hypothesis_id!r} 不存在于本研究上下文中")
        for evidence_id in update.supporting_evidence_ids:
            if evidence_id not in state.evidence_ids:
                issues.append(
                    f"支持证据 {evidence_id!r} 不在本次研究已持有的证据中；"
                    f"不得引用不存在的证据")
        for evidence_id in update.contradicting_evidence_ids:
            if evidence_id not in state.evidence_ids:
                issues.append(
                    f"反驳证据 {evidence_id!r} 不在本次研究已持有的证据中")
        if (update.proposed_status is HypothesisStatus.SUPPORTED
                and not update.supporting_evidence_ids):
            issues.append(
                "标为 supported 必须给出真实的 supporting_evidence_ids；"
                "重复与自信都不是证据")
        if issues:
            return ValidationOutcome(
                outcome=DecisionOutcome.REJECTED,
                detail="the hypothesis update cannot be applied as written",
                issues=issues)
        return ValidationOutcome(outcome=DecisionOutcome.ACCEPTED)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class ResearchIntelligence:
    """The one research decision loop.

    Constructed over a `ToolRegistry` (the M1.1 surface) and an LLM. The
    study context is optional: without it the run still works, it simply
    does not accumulate across sessions.
    """

    def __init__(self, registry: Any, llm: LLMProvider | None = None, *,
                 study_context: Any = None, study_id: str = "",
                 budget: ResearchBudget | None = None,
                 validator: DecisionValidator | None = None,
                 temperature: float = 0.0) -> None:
        self._registry = registry
        self._llm = llm
        self._study_context = study_context
        self._study_id = study_id
        self._budget = budget if budget is not None else ResearchBudget()
        self._validator = (validator if validator is not None
                           else DecisionValidator(registry))
        self._temperature = temperature
        self._cancelled = False

    # -- public API ---------------------------------------------------------

    def cancel(self) -> None:
        """Ask the run to stop before its next decision.

        Checked between turns: a tool already running finishes and is
        observed honestly; no new decision is requested afterwards. This
        is a cooperative stop -- the loop cannot interrupt a synchronous
        tool mid-call, and says so rather than claiming otherwise.
        """
        self._cancelled = True

    def with_study_context(self, service: Any) -> "ResearchIntelligence":
        """Point this run at a (possibly new) study-context handle.

        A storage handle belongs to whoever opened it, and an HTTP entry
        serves each request on its own worker. The study id never changes
        -- only the door onto it. Returns self so a caller can write
        `intelligence.with_study_context(service).resume(...)`.
        """
        self._study_context = service
        return self

    def run(self, goal: str, *, intent: ResearchIntent | None = None,
            session_id: str = "", approvals: list[str] | None = None,
            context: dict[str, Any] | None = None,
            allow_model: bool = True) -> IntelligenceRunResult:
        """Research one goal to a terminal state or a pause.

        `intent` is the M1.6 structured request; when absent the goal is
        understood through `InputUnderstandingService`. `approvals` seeds
        steps the caller has already approved. `allow_model=False` refuses
        to call a model at all -- the run then stops `blocked` with that
        stated, rather than pretending to be autonomous.
        """
        self._cancelled = False
        if not session_id:
            session_id = "sess-" + content_hash(
                f"{self._study_id}|{goal}|{time.time_ns()}", 12)
        started_at = utc_now_iso()
        clock = time.perf_counter()

        if intent is None:
            intent = self._understand(goal)
        state = self._assemble_state(goal, session_id, intent)
        if context:
            state.direction = state.direction or goal

        records: list[DecisionRecord] = []
        decisions: list[ResearchDecision] = []
        steps: list[ResearchStep] = []
        observations: list[Observation] = []
        interpretations: list[ScientificInterpretation] = []
        tool_results: list[ToolResult] = []
        diagnostics: list[str] = []
        failures: list[str] = []
        unresolved: list[str] = []
        hypothesis_ids: list[str] = []
        approval_set = set(approvals or [])
        no_progress = 0
        fix_attempts = 0
        model_calls = 0
        tool_calls = 0
        input_tokens: int | None = None
        output_tokens: int | None = None
        awaiting: IntelligenceAwaiting | None = None
        cancelled = False
        completion: Any = None
        blocked: Any = None
        stop_reason = ""

        if self._llm is None:
            blocked = _blocked_payload(
                "未配置 LLM Provider，无法进行 AI 科研决策", "model_unavailable")
            stop_reason = "未配置 LLM Provider"
        elif not allow_model:
            blocked = _blocked_payload(
                "调用方禁止本次运行调用模型", "model_unavailable")
            stop_reason = "调用方禁止调用模型"

        index = 0
        last_issues: list[str] = []
        while stop_reason == "" and index < MAX_INTELLIGENCE_ITERATIONS:
            index += 1
            spent = self._budget.exhausted(
                decisions=len(records), tool_calls=tool_calls,
                model_calls=model_calls,
                elapsed=time.perf_counter() - clock)
            if spent:
                blocked = _blocked_payload(spent, "budget_exhausted")
                stop_reason = spent
                break
            if self._cancelled:
                cancelled = True
                stop_reason = "调用方取消了本次研究"
                break

            decision, record, model_calls, input_tokens, output_tokens = \
                self._decide(state, session_id, len(records) + 1, model_calls,
                             input_tokens, output_tokens, last_issues)
            records.append(record)
            if decision is None:
                if record.validation_result is DecisionOutcome.MODEL_UNAVAILABLE:
                    blocked = _blocked_payload(
                        f"模型不可用：{record.validation_detail}",
                        "model_unavailable")
                    stop_reason = f"模型不可用：{record.validation_detail}"
                    failures.append(stop_reason)
                else:
                    last_issues = list(record.warnings)
                    fix_attempts += 1
                    if fix_attempts > self._budget.max_fix_attempts:
                        blocked = _blocked_payload(
                            "模型连续返回无法使用的决策，修复次数已达上限",
                            "model_output_invalid")
                        stop_reason = "模型输出无法解析为研究决策"
                        failures.append(stop_reason)
                    # otherwise: the repair request is already in the thread
                continue

            # Rejected decisions are reported, never executed.
            if record.validation_result is DecisionOutcome.REJECTED:
                last_issues = list(record.warnings)
                fix_attempts += 1
                diagnostics.append(
                    f"决策被拒绝：{record.validation_detail}；"
                    f"{'；'.join(record.warnings[:4])}")
                if fix_attempts > self._budget.max_fix_attempts:
                    blocked = _blocked_payload(
                        "模型连续提出无法执行的决策，修复次数已达上限",
                        "model_output_invalid")
                    stop_reason = "模型决策未通过校验"
                continue

            fix_attempts = 0
            last_issues = []
            decisions.append(decision)
            self._note_decision(state, decision, session_id)

            if decision.kind is DecisionKind.ASK_USER:
                ask = decision.ask_user
                assert ask is not None
                awaiting = IntelligenceAwaiting(
                    question=ask.question, why=ask.why,
                    expected_information=ask.expected_information,
                    options=list(ask.options))
                unresolved.append(ask.question)
                stop_reason = f"需要用户回答：{ask.question}"
                break
            if decision.kind is DecisionKind.COMPLETE:
                completion = decision.completion
                stop_reason = completion.completion_reason
                break
            if decision.kind is DecisionKind.BLOCKED:
                blocked = decision.blocked
                stop_reason = blocked.reason
                if blocked.blocker not in ("budget_exhausted",):
                    failures.append(stop_reason)
                break

            if decision.kind is DecisionKind.UPDATE_HYPOTHESIS:
                moved = self._apply_hypothesis(decision, session_id,
                                               hypothesis_ids, diagnostics)
                hypothesis_ids.extend(
                    hid for hid in moved if hid not in hypothesis_ids)
                no_progress = 0 if moved else no_progress + 1
                continue

            if decision.kind is DecisionKind.UPDATE_DIRECTION:
                state.direction = decision.direction_update.new_direction
                no_progress = 0
                continue

            # CONTINUE_WITH_TOOL: execute exactly once, through the registry.
            tool_calls += 1
            step, observation = self._execute(
                decision, state, steps, observations, tool_results,
                session_id, approval_set, diagnostics, clock)
            if records:
                # The audit row names the step this decision produced, so a
                # reader can join a decision to its observation.
                records[-1].associated_step_id = step.step_id
            self._interpret(decision, observation, interpretations)
            progressed = self._absorbed(
                decision, observation, state, session_id, diagnostics)
            no_progress = 0 if progressed else no_progress + 1
            if no_progress >= self._budget.max_consecutive_no_progress:
                blocked = _blocked_payload(
                    f"连续 {no_progress} 轮没有研究进展（无新证据、无新工具调用、"
                    f"无假设变化），研究循环停止",
                    "no_progress")
                stop_reason = blocked.reason
                break

        ended_at = utc_now_iso()
        elapsed_ms = int((time.perf_counter() - clock) * 1000)
        if not stop_reason:
            stop_reason = "达到迭代上限"
            blocked = blocked or _blocked_payload(stop_reason, "no_progress")

        result = self._assemble(
            goal=goal, session_id=session_id, started_at=started_at,
            ended_at=ended_at, elapsed_ms=elapsed_ms, records=records,
            decisions=decisions, steps=steps, observations=observations,
            interpretations=interpretations, tool_results=tool_results,
            diagnostics=diagnostics, failures=failures, unresolved=unresolved,
            hypothesis_ids=hypothesis_ids, awaiting=awaiting,
            cancelled=cancelled, stop_reason=stop_reason, completion=completion,
            blocked=blocked, model_calls=model_calls, tool_calls=tool_calls,
            input_tokens=input_tokens, output_tokens=output_tokens,
            state=state)
        self._close_session(result)
        return result

    def resume(self, result: IntelligenceRunResult, *,
               answers: dict[str, Any] | None = None,
               approvals: list[str] | None = None
               ) -> IntelligenceRunResult:
        """Continue a paused run with what the person supplied.

        Answers are merged into the study context's facts as `USER_STATED`
        -- the user's own material, never `TOOL_VERIFIED` -- and the loop
        starts again from the current state. An identical tool call that
        was refused before is allowed again after an answer: the state
        genuinely changed.
        """
        if result.awaiting is None:
            raise ValueError(
                f"只有等待用户输入的研究可以恢复（当前 outcome: "
                f"{result.outcome}）")
        self._cancelled = False
        if self._study_context is not None and self._study_id and answers:
            for key, value in (answers or {}).items():
                try:
                    self._study_context.add_fact(
                        self._study_id, fact_type=f"user_answer.{key}",
                        payload={"value": value}, summary=str(value)[:300],
                        source="user",
                        verification=FactVerification.USER_STATED,
                        session_id=result.session_id)
                except CreaError as exc:
                    # A context write that fails is a diagnostic, not a
                    # research failure; the answer still reaches the loop.
                    result.diagnostics.append(
                        f"study context write failed: {type(exc).__name__}: "
                        f"{exc}")
        return self.run(
            result.goal, session_id=result.session_id,
            approvals=list(approvals or []),
            context={"resumed_from_session": result.session_id,
                     **(answers or {})})

    # -- one turn -----------------------------------------------------------

    def _decide(self, state: ResearchState, session_id: str, turn: int,
                model_calls: int, input_tokens: int | None,
                output_tokens: int | None, repair_issues: list[str]
                ) -> tuple[ResearchDecision | None, DecisionRecord, int,
                           int | None, int | None]:
        """Ask the model, parse, validate. Never executes anything."""
        provider = getattr(self._llm, "name", "") or ""
        model = getattr(self._llm, "model", "") or ""
        base = dict(
            decision_id="dec-" + content_hash(
                f"{session_id}|{turn}|{time.time_ns()}", 16),
            session_id=session_id, study_id=self._study_id, turn=turn,
            model_provider=provider, model_name=model,
            prompt_version=INTELLIGENCE_PROMPT_VERSION,
        )
        first = build_user_message(state, budget_note=self._budget.note(
            decisions=turn - 1, tool_calls=len(state.executed_calls),
            model_calls=model_calls))
        if repair_issues:
            messages = [
                {"role": "user", "content": first},
                {"role": "assistant", "content": "(上一次回复未能通过校验)"},
                {"role": "user", "content": repair_message(repair_issues)},
            ]
        else:
            messages = [{"role": "user", "content": first}]

        started = time.perf_counter()
        try:
            response = self._llm.complete(LLMRequest(
                system=SYSTEM, messages=messages,
                max_output_tokens=self._budget.max_output_tokens,
                temperature=self._temperature,
                metadata={"prompt_version": INTELLIGENCE_PROMPT_VERSION,
                          "session_id": session_id, "turn": turn}))
        except Exception as exc:  # noqa: BLE001 - a dead model is a result
            duration = int((time.perf_counter() - started) * 1000)
            detail = f"{type(exc).__name__}: {exc}"
            return (None, DecisionRecord(
                **base, duration_ms=duration,
                validation_result=DecisionOutcome.MODEL_UNAVAILABLE,
                validation_detail=detail,
                warnings=["模型不可用；本次运行不会退化为固定流程后仍声称是 AI 研究"]),
                model_calls + 1, input_tokens, output_tokens)

        model_calls += 1
        duration = int((time.perf_counter() - started) * 1000)
        input_tokens = _sum(input_tokens, response.input_tokens)
        output_tokens = _sum(output_tokens, response.output_tokens)

        decision, issues = parse_decision(response.text or "")
        if decision is None:
            return (None, DecisionRecord(
                **base, duration_ms=duration,
                validation_result=DecisionOutcome.UNPARSEABLE,
                validation_detail="回复不是可用的研究决策",
                warnings=issues,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens),
                model_calls, input_tokens, output_tokens)

        outcome = self._validator.validate(
            decision, state, budget=self._budget,
            tool_calls_used=len(state.executed_calls))
        record = DecisionRecord(
            **base,
            decision_type=decision.kind,
            decision_summary=_summary_of(decision),
            selected_tool=(decision.tool_call.tool
                           if decision.tool_call is not None else ""),
            validation_result=outcome.outcome,
            validation_detail=outcome.detail,
            warnings=list(outcome.issues),
            duration_ms=duration,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            decision=decision if outcome.accepted else None)
        return (decision if outcome.accepted else None, record,
                model_calls, input_tokens, output_tokens)

    def _execute(self, decision: ResearchDecision, state: ResearchState,
                 steps: list[ResearchStep], observations: list[Observation],
                 tool_results: list[ToolResult], session_id: str,
                 approvals: set[str], diagnostics: list[str],
                 clock: float) -> tuple[ResearchStep, Observation]:
        """One tool call, through the registry, exactly once."""
        call = decision.tool_call
        assert call is not None
        step_id = f"step-{len(steps) + 1}"
        step = ResearchStep(
            step_id=step_id, index=len(steps), action=ActionKind.TOOL_CALL,
            tool=call.tool, arguments=dict(call.arguments or {}),
            note=call.purpose,
            llm_provider=getattr(self._llm, "name", "") or "",
            llm_model=getattr(self._llm, "model", "") or "")
        steps.append(step)

        result = self._registry.execute(
            call.tool, dict(call.arguments or {}), step_id=step_id,
            approved=step_id in approvals or call.tool in approvals)
        observation = observe_from_registry(self._registry, call.tool, result)
        if not observation.step_id:
            observation.step_id = step_id
        step.result_status = result.status
        step.error = result.error
        elapsed = time.perf_counter() - clock
        if (elapsed > self._budget.max_tool_call_seconds):
            diagnostics.append(
                f"步骤 {step_id} 结束时总运行时间已超过单步预算参考值 "
                f"({self._budget.max_tool_call_seconds:.0f}s)；该工具为同步调用，"
                f"循环无法中途打断，此处只如实记录")

        observations.append(observation)
        tool_results.append(result)
        state.note_observation(observation, arguments=call.arguments)
        return step, observation

    def _interpret(self, decision: ResearchDecision, observation: Observation,
                   interpretations: list[ScientificInterpretation]) -> None:
        if not decision.observation_interpretation:
            return
        interpretations.append(ScientificInterpretation(
            step_id=observation.step_id, tool=observation.tool,
            observation_kind=observation.kind.value,
            interpretation=decision.observation_interpretation,
            related_hypothesis_ids=list(
                decision.tool_call.related_hypothesis_ids
                if decision.tool_call is not None else []),
            evidence_ids=list(observation.evidence_ids)))

    # -- state writes -------------------------------------------------------

    def _absorbed(self, decision: ResearchDecision, observation: Observation,
                  state: ResearchState, session_id: str,
                  diagnostics: list[str]) -> bool:
        """Persist what an execution established, and whether it progressed.

        Progress means a new tool call with a new result, or new evidence.
        A second identical NO_RESULT is not progress, which is what stops
        the loop repeating a fruitless query forever.
        """
        if self._study_context is None or not self._study_id:
            return observation.kind is not ObservationKind.NO_RESULT
        try:
            if observation.kind is ObservationKind.SUCCESS and observation.tool:
                for name in _declared_facts(self._registry, observation):
                    payload = (observation.data
                               if isinstance(observation.data, dict)
                               else {"value": observation.data})
                    self._study_context.add_fact(
                        self._study_id, fact_type=name, payload=payload,
                        summary=observation.summary, source=observation.tool,
                        verification=FactVerification.TOOL_VERIFIED,
                        provenance=observation.provenance,
                        evidence_ids=observation.evidence_ids,
                        session_id=session_id)
            elif observation.kind in (ObservationKind.UNAVAILABLE,
                                      ObservationKind.ERROR,
                                      ObservationKind.NEEDS_INPUT):
                self._study_context.record_unresolved_issue(
                    self._study_id,
                    f"{observation.tool}: {observation.summary}",
                    kind=_ISSUE_KIND.get(observation.kind, "user_input"),
                    session_id=session_id)
        except CreaError as exc:
            diagnostics.append(
                f"study context write failed: {type(exc).__name__}: {exc}")
        return (observation.kind in (ObservationKind.SUCCESS,
                                     ObservationKind.PARTIAL)
                or bool(observation.evidence_ids))

    def _apply_hypothesis(self, decision: ResearchDecision, session_id: str,
                          hypothesis_ids: list[str],
                          diagnostics: list[str]) -> list[str]:
        """Move or create one hypothesis. `SUPPORTED` is not ours to grant."""
        update = decision.hypothesis_update
        assert update is not None
        if self._study_context is None or not self._study_id:
            # No study context: the hypothesis exists only on the record.
            diagnostics.append(
                "未提供研究上下文，假设仅记录在本次运行的决策轨迹中")
            return []
        try:
            if update.hypothesis_id:
                moved = self._study_context.update_hypothesis(
                    self._study_id, update.hypothesis_id,
                    update.proposed_status, note=update.rationale_summary,
                    supporting_evidence_ids=update.supporting_evidence_ids,
                    contradicting_evidence_ids=update.contradicting_evidence_ids)
            else:
                created = self._study_context.add_hypothesis(
                    self._study_id, update.hypothesis,
                    session_id=session_id,
                    note=update.uncertainty or update.rationale_summary)
                # A brand-new hypothesis is always `proposed` on creation --
                # that is the study context's rule, not ours. The evidence
                # and the requested status are applied by the follow-up
                # update, which is still the same door and still refuses
                # `SUPPORTED` without named evidence.
                moved = created
                if (update.proposed_status is not HypothesisStatus.PROPOSED
                        or update.supporting_evidence_ids
                        or update.contradicting_evidence_ids):
                    moved = self._study_context.update_hypothesis(
                        self._study_id, created.hypothesis_id,
                        update.proposed_status,
                        note=update.rationale_summary,
                        supporting_evidence_ids=update.supporting_evidence_ids,
                        contradicting_evidence_ids=(
                            update.contradicting_evidence_ids))
            if moved.hypothesis_id not in hypothesis_ids:
                return [moved.hypothesis_id]
        except CreaError as exc:
            diagnostics.append(
                f"假设更新被研究上下文拒绝：{type(exc).__name__}: {exc}")
        return []

    def _note_decision(self, state: ResearchState, decision: ResearchDecision,
                       session_id: str) -> None:
        entry = {
            "kind": decision.kind.value,
            "summary": _summary_of(decision),
        }
        state.decisions.append(entry)
        if self._study_context is None or not self._study_id:
            return
        try:
            self._study_context.record_decision(
                self._study_id,
                decision=f"{decision.kind.value}: {_summary_of(decision)[:300]}",
                reason=decision.observation_interpretation[:300],
                session_id=session_id)
        except CreaError:
            # A decision record that will not persist is not a research
            # failure; the run's own DecisionRecord still holds it.
            pass

    def _close_session(self, result: IntelligenceRunResult) -> None:
        if (self._study_context is None or not self._study_id
                or result.awaiting is not None):
            return
        try:
            self._study_context.record_session_summary(
                self._study_id, StudySessionSummary(
                    session_id=result.session_id, status=result.status.value,
                    outcome=result.outcome, ended_at=result.ended_at,
                    step_count=len(result.steps),
                    evidence_ids=list(result.evidence_ids),
                    note=result.reason[:500]))
        except CreaError as exc:
            result.diagnostics.append(
                f"study context write failed: {type(exc).__name__}: {exc}")

    # -- assembly -----------------------------------------------------------

    def _understand(self, goal: str) -> ResearchIntent | None:
        """Understand the goal through M1.6. A failure is not a run failure.

        If input understanding itself refuses, the run proceeds with the
        bare goal and records why -- the intelligence may still ask the
        person the question the parser could not answer.
        """
        try:
            from crea.services.input_understanding import (
                InputUnderstandingService)
            return InputUnderstandingService().understand(goal)
        except CreaError:
            # A refusal here is a question the run can still ask the
            # person; it is not a reason to stop researching.
            return None

    def _assemble_state(self, goal: str, session_id: str,
                        intent: ResearchIntent | None) -> ResearchState:
        state = ResearchState(goal=goal, session_id=session_id,
                              study_id=self._study_id, intent=intent,
                              catalog=self._catalog())
        if self._study_context is None or not self._study_id:
            return state
        try:
            context = self._study_context.get_context(self._study_id)
        except CreaError:
            return state
        state.constraints.extend(context.constraints)
        state.facts = [
            {"fact_id": f.fact_id, "fact_type": f.fact_type,
             "summary": f.summary, "source": f.source,
             "verification": f.verification.value}
            for f in context.facts
        ]
        state.hypotheses = [
            {"hypothesis_id": h.hypothesis_id, "content": h.content,
             "status": h.status.value,
             "supporting_evidence_ids": list(h.supporting_evidence_ids),
             "contradicting_evidence_ids": list(h.contradicting_evidence_ids)}
            for h in context.hypotheses
        ]
        for ref in context.evidence_refs:
            if ref.evidence_id not in state.evidence_ids:
                state.evidence_ids.append(ref.evidence_id)
        state.decisions = [
            {"decision": d.decision, "reason": d.reason,
             "at": d.created_at}
            for d in context.decisions
        ]
        state.issues = [
            {"issue_id": issue.issue_id, "kind": issue.kind,
             "content": issue.content}
            for issue in context.issues
        ]
        # Facts the tools established earlier are handed back to the
        # registry's guard exactly as the runtime does: they were
        # established by real tool runs, and this does not bypass the gate.
        facts = getattr(self._registry, "facts", None)
        if facts is not None:
            for fact in context.facts:
                if (fact.verification is FactVerification.TOOL_VERIFIED
                        and fact.fact_type):
                    try:
                        facts.add(fact.fact_type)
                    except Exception:  # noqa: BLE001 - facts without add
                        pass
        return state

    def _catalog(self) -> list[dict]:
        """What the model may name: canonical tools, and what they declare."""
        out = []
        for entry in self._registry.describe():
            if entry.get("alias_of"):
                continue
            out.append({
                "name": entry["name"],
                "description": entry["description"],
                "arguments": entry["input_schema"],
                "requires_facts": entry.get("requires") or [],
                "needs_human_approval": entry.get("approval") == "required",
                "effects": entry.get("effects"),
            })
        return out

    def _assemble(self, *, goal: str, session_id: str, started_at: str,
                  ended_at: str, elapsed_ms: int, records, decisions, steps,
                  observations, interpretations, tool_results, diagnostics,
                  failures, unresolved, hypothesis_ids, awaiting, cancelled,
                  stop_reason, completion, blocked, model_calls, tool_calls,
                  input_tokens, output_tokens, state
                  ) -> IntelligenceRunResult:
        evidence_ids = list(state.evidence_ids)
        has_results = any(o.kind in (ObservationKind.SUCCESS,
                                     ObservationKind.PARTIAL)
                          for o in observations)
        misses = sum(1 for o in observations
                     if o.kind is ObservationKind.NO_RESULT)
        hard = [o.step_id for o in observations
                if o.kind in (ObservationKind.ERROR, ObservationKind.NEEDS_INPUT,
                              ObservationKind.UNAVAILABLE)]

        #: Something the run actually produced: a tool result, or a
        #: research decision the model was entitled to make. A run that
        #: only ever had its replies thrown away has produced nothing.
        produced = has_results or bool(decisions) or bool(observations)

        if awaiting is not None:
            status, outcome = TaskStatus.RUNNING, "input_required"
        elif cancelled:
            status = (TaskStatus.PARTIAL if produced else TaskStatus.FAILED)
            outcome = "partial" if produced else "failed"
        elif blocked is not None:
            status = TaskStatus.PARTIAL if produced else TaskStatus.FAILED
            outcome = "partial" if produced else "failed"
        elif completion is not None and not hard:
            if has_results:
                status, outcome = TaskStatus.COMPLETED, "completed"
            elif misses:
                status, outcome = TaskStatus.COMPLETED, "no_results"
            else:
                status, outcome = TaskStatus.COMPLETED, "completed"
        else:
            status = TaskStatus.PARTIAL if produced else TaskStatus.FAILED
            outcome = "partial" if produced else "failed"

        if hard:
            unresolved.extend(
                f"{o.tool}: {o.summary}" for o in observations
                if o.kind is ObservationKind.NEEDS_INPUT
                and o.summary not in unresolved)

        return IntelligenceRunResult(
            status=status, outcome=outcome, session_id=session_id,
            study_id=self._study_id, reason=stop_reason, goal=goal,
            started_at=started_at, ended_at=ended_at,
            duration_ms=elapsed_ms, decision_records=records,
            decisions=decisions, steps=steps, observations=observations,
            interpretations=interpretations, tool_results=tool_results,
            awaiting=awaiting, cancelled=cancelled,
            evidence_ids=evidence_ids, hypothesis_ids=hypothesis_ids,
            unresolved_items=list(dict.fromkeys(unresolved)),
            failures=failures, diagnostics=diagnostics,
            model_provider=getattr(self._llm, "name", "") or "",
            model_name=getattr(self._llm, "model", "") or "",
            budget=self._budget, decisions_used=len(records),
            tool_calls_used=tool_calls, model_calls_used=model_calls,
            input_tokens=input_tokens, output_tokens=output_tokens,
            completion=completion, blocked=blocked)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


_ISSUE_KIND = {
    ObservationKind.UNAVAILABLE: "tool_unavailable",
    ObservationKind.ERROR: "tool_error",
    ObservationKind.NEEDS_INPUT: "user_input",
}


def _blocked_payload(reason: str, blocker: str) -> BlockedStatement:
    return BlockedStatement(reason=reason, blocker=blocker)


def _signature(tool: str, arguments: dict) -> str:
    try:
        encoded = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True,
                             default=str)
    except (TypeError, ValueError):
        encoded = str(arguments)
    return f"{tool}|{encoded}"


def _bounded(data: Any, limit: int = 1500) -> Any:
    """A result preview for the prompt: bounded, and never silently whole."""
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(data)
    if len(text) <= limit:
        return data
    return {"truncated": True, "preview": text[:limit]}


def _declared_facts(registry: Any, observation: Observation) -> list[str]:
    getter = getattr(registry, "get", None)
    spec = getter(observation.tool) if callable(getter) else None
    if spec is None:
        return []
    names = list(getattr(spec, "sets_facts", ()) or ())
    rule = getattr(spec, "fact_rule", None)
    if callable(rule):
        try:
            names.extend(rule(observation.data) or ())
        except Exception:  # noqa: BLE001 - a fact rule is not a run failure
            pass
    return list(dict.fromkeys(n for n in names if n))


def _summary_of(decision: ResearchDecision) -> str:
    if decision.tool_call is not None:
        return decision.tool_call.decision_summary
    if decision.hypothesis_update is not None:
        return decision.hypothesis_update.rationale_summary
    if decision.direction_update is not None:
        return decision.direction_update.reason
    if decision.ask_user is not None:
        return decision.ask_user.question
    if decision.completion is not None:
        return decision.completion.completion_reason
    if decision.blocked is not None:
        return decision.blocked.reason
    return ""


def _sum(total: int | None, add: int | None) -> int | None:
    if total is None and add is None:
        return None
    return (total or 0) + (add or 0)


def _short(exc: Exception, limit: int = 240) -> str:
    text = str(exc).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def build_plan_from_decisions(goal: str,
                              decisions: list[ResearchDecision]
                              ) -> ResearchPlan:
    """The plan this run actually followed, reconstructed from its decisions.

    An AI-driven run has no plan authored in advance; what it has is the
    sequence of tools it chose and why. This renders that sequence as the
    existing `ResearchPlan` so the trace, the report layer and the golden
    case runner all read one plan vocabulary.
    """
    steps: list[ResearchPlanStep] = []
    for index, decision in enumerate(decisions, start=1):
        if decision.kind is not DecisionKind.CONTINUE_WITH_TOOL:
            continue
        call = decision.tool_call
        assert call is not None
        steps.append(ResearchPlanStep(
            action=_action_for_tool(call.tool),
            step_id=f"step-{index}",
            tool=call.tool,
            arguments=dict(call.arguments or {}),
            reason=call.purpose,
            expected_output=call.expected_information))
    return ResearchPlan(goal=goal, steps=steps, revision=max(
        0, len(decisions) - len(steps)))


def _action_for_tool(tool: str):
    from crea.agent.planner import _ACTION_FOR_TOOL
    from crea.core.models.knowledge import ResearchPlanAction
    return _ACTION_FOR_TOOL.get(tool, ResearchPlanAction.REPORT)


__all__ = [
    "DecisionValidator",
    "IntelligenceAwaiting",
    "IntelligenceRunResult",
    "MAX_INTELLIGENCE_ITERATIONS",
    "ResearchBudget",
    "ResearchIntelligence",
    "ResearchMode",
    "ResearchState",
    "ValidationOutcome",
    "build_plan_from_decisions",
]

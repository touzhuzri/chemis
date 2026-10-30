"""M1.3 -- the planner: what to do next, and why.

The planner is the research *decision* layer. It reads the goal, proposes a
`ResearchPlan`, validates every step against the registry's declared tool
surfaces, and consumes M1.2 `Observation`s to decide whether to continue,
adjust the plan, ask the user, or stop. It never executes a tool, never
re-classifies a result, and never turns a model's suggestion into a fact.

Three boundaries this module holds:

* **The plan is the existing model.** `ResearchPlan` / `ResearchPlanStep`
  are extended additively (ids, dependencies, arguments, expected output,
  completion condition, a declared fallback). No parallel plan model exists.
* **Observation is the only reading of a tool call.** Decisions branch on
  `ObservationKind` and `missing_requirements`; a `ToolResult` is never
  re-parsed here.
* **The model may propose, never decide alone.** An LLM can draft or revise
  a plan, but every tool name, argument and dependency is validated against
  the registry before it is accepted. An unusable reply falls back to the
  deterministic plan instead of being silently adopted.

Execution stays with the existing agents and `ToolRegistry`; this module only
returns `PlanDecision` objects.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from crea.agent.observer import Observation, ObservationKind
from crea.core.json_parse import parse_json_object
from crea.core.models.knowledge import (
    ResearchPlan,
    ResearchPlanAction,
    ResearchPlanStep,
)
from crea.llm.base import LLMProvider, LLMRequest

#: Conservative defaults. A plan that needs more than this is not a plan the
#: current tool surface can execute; the budget is a decision, not a token
#: accounting system.
MAX_PLAN_STEPS = 8
MAX_PLAN_REVISIONS = 3
MAX_PLAN_DECISIONS = 16

#: A step is done when its outcome is one of these -- the work may have found
#: nothing (NO_RESULT) or only part (PARTIAL), but it did run.
_DEPENDENCY_SATISFIED = frozenset({
    ObservationKind.SUCCESS,
    ObservationKind.NO_RESULT,
    ObservationKind.PARTIAL,
})

#: Outcomes that mean the branch did *not* run to completion.
_HARD_FAILURES = frozenset({
    ObservationKind.NEEDS_INPUT,
    ObservationKind.UNAVAILABLE,
    ObservationKind.ERROR,
})

#: Facts the M1.1 surface declares, and the canonical tool that establishes
#: each. A planner may plan the prerequisite instead of asking the user.
_FACT_TOOLS = {
    "identity_resolved": "resolve_chemical_identity",
    "routes_held": "retrosynthesize",
}

#: Which plan action a tool implements. Used only to fill in an absent
#: `action` for a model-proposed step; never to invent a tool.
_ACTION_FOR_TOOL = {
    "get_existing_knowledge": ResearchPlanAction.ASSESS_KNOWLEDGE,
    "search_knowledge": ResearchPlanAction.ASSESS_KNOWLEDGE,
    "resolve_chemical_identity": ResearchPlanAction.RESOLVE_IDENTITY,
    "name_structure": ResearchPlanAction.RESOLVE_IDENTITY,
    "analyze_molecule": ResearchPlanAction.ANALYZE_STRUCTURE,
    "search_literature": ResearchPlanAction.SEARCH_LITERATURE,
    "search_reaction_evidence": ResearchPlanAction.SEARCH_LITERATURE,
    "search_reactions": ResearchPlanAction.SEARCH_LITERATURE,
    "get_document_evidence": ResearchPlanAction.INSPECT_DOCUMENT,
    "search_local_document": ResearchPlanAction.INSPECT_DOCUMENT,
    "extract_reactions": ResearchPlanAction.EXTRACT_REACTION_EVIDENCE,
    "get_reaction_details": ResearchPlanAction.EXTRACT_REACTION_EVIDENCE,
    "retrosynthesize": ResearchPlanAction.RETROSYNTHESIZE,
    "explore_routes": ResearchPlanAction.RETROSYNTHESIZE,
    "validate_route_step": ResearchPlanAction.VALIDATE_ROUTES,
    "validate_reaction": ResearchPlanAction.VALIDATE_ROUTES,
    "route_evidence_report": ResearchPlanAction.VALIDATE_ROUTES,
    "synthesize_evidence": ResearchPlanAction.SYNTHESIZE_EVIDENCE,
}


class PlanDecisionKind(str, Enum):
    """What the planner wants the run to do next.

    Deliberately not derived from `ObservationKind`: an observation says what
    a *tool call* produced, a decision says what the *run* should do. The
    decision names the observation that triggered it, so the two never blur.
    """

    CONTINUE = "continue"
    ASK_USER = "ask_user"
    COMPLETE = "complete"
    BLOCKED = "blocked"


class PlanChangeKind(str, Enum):
    ADD_STEP = "add_step"
    RETARGET_TOOL = "retarget_tool"
    UPDATE_ARGUMENTS = "update_arguments"
    DROP_STEP = "drop_step"


class PlanChange(BaseModel):
    """One auditable edit to the plan."""

    kind: PlanChangeKind
    step_id: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    reason: str = ""


class PlanDecision(BaseModel):
    """The planner's answer: a decision, its reason, and what changed."""

    kind: PlanDecisionKind
    reason: str
    next_step_id: str = ""
    #: The step whose observation triggered this decision (None for a start).
    executed_step_id: str = ""
    changes: list[PlanChange] = Field(default_factory=list)
    needs_user_input: bool = False
    missing_requirements: list[str] = Field(default_factory=list)
    #: The next step's tool declares a persistent effect requiring approval.
    requires_approval: bool = False
    #: The run has reliable results but at least one branch is incomplete.
    partial: bool = False
    trigger: Observation | None = None
    revision: int = 0
    decisions_used: int = 0


class PlanProposal(BaseModel):
    """A generated plan and how it was produced.

    `source` is one of `deterministic`, `model`, `model_rejected`. A rejected
    model reply is reported, never silently replaced.
    """

    plan: ResearchPlan
    source: str = "deterministic"
    warnings: list[str] = Field(default_factory=list)


class PlanSession(BaseModel):
    """One plan and the run's progress against it.

    Outcomes are M1.2 `ObservationKind`s -- no second result vocabulary. The
    plan itself stays a statement of intent; this is where execution state
    lives, so a plan can be re-read without pretending it already happened.
    """

    plan: ResearchPlan
    outcomes: dict[str, ObservationKind] = Field(default_factory=dict)
    observations: dict[str, Observation] = Field(default_factory=dict)
    #: Tool + arguments signatures already run. A NO_RESULT never re-runs one.
    executed_calls: list[str] = Field(default_factory=list)
    #: Steps whose declared fallback was already used.
    fallback_used: list[str] = Field(default_factory=list)
    approvals: list[str] = Field(default_factory=list)
    #: Session facts declared by `ToolSpec.sets_facts` / `fact_rule`, plus any
    #: the caller supplies. Checked against `ToolSpec.requires`.
    facts: list[str] = Field(default_factory=list)
    #: Caller-provided values the planner may use to complete arguments.
    context: dict[str, Any] = Field(default_factory=dict)
    revisions: int = 0
    decisions: int = 0
    notes: list[str] = Field(default_factory=list)

    def approve(self, step_id: str) -> None:
        if step_id and step_id not in self.approvals:
            self.approvals.append(step_id)

    def done(self, step_id: str) -> bool:
        return step_id in self.outcomes


class PlanValidationError(ValueError):
    """A plan that cannot be executed as written."""

    def __init__(self, issues: list[str]) -> None:
        self.issues = list(issues)
        super().__init__("; ".join(self.issues) or "invalid plan")


class _BudgetExceeded(Exception):
    """The planner refused an edit because a declared budget is spent."""


# ---------------------------------------------------------------------------
# Deterministic planning
# ---------------------------------------------------------------------------


def _looks_complex(goal: str) -> bool:
    """A deterministic hint, not a judgement about the research.

    Long, multi-clause goals are where a model's decomposition can help. A
    short single-question goal is planned without a model call, so an ordinary
    tool execution never pays for one.
    """
    text = (goal or "").strip()
    if len(text) > 120:
        return True
    connectors = ("以及", "并且", "同时", "然后", "比较", "分别",
                  " and ", " then ", " compare ", " versus ", ";")
    lowered = f" {text.lower()} "
    return any(token in lowered for token in connectors)


class Planner:
    """Deterministic base planning with constrained model assistance."""

    def __init__(self, registry: Any, llm: LLMProvider | None = None, *,
                 max_steps: int = MAX_PLAN_STEPS,
                 max_revisions: int = MAX_PLAN_REVISIONS,
                 max_decisions: int = MAX_PLAN_DECISIONS) -> None:
        self._registry = registry
        self._llm = llm
        self.max_steps = max(1, int(max_steps))
        self.max_revisions = max(0, int(max_revisions))
        self.max_decisions = max(1, int(max_decisions))

    # -- planning -----------------------------------------------------------

    def initial_plan(self, goal: str, *, identity_input: dict[str, Any] | None = None,
                     smiles: str = "", document_id: str = "",
                     wants_routes: bool = False,
                     use_llm: bool | None = None,
                     context: dict[str, Any] | None = None) -> PlanProposal:
        """The starting plan.

        `use_llm=None` means "use the model only when one is configured and the
        goal is complex"; `False` forces the deterministic plan, so a caller
        can guarantee no model call. A model reply that fails validation is
        never used -- the deterministic plan is returned and the rejection is
        reported in `warnings`.
        """
        base = self._base_plan(goal, identity_input=identity_input,
                               smiles=smiles, document_id=document_id,
                               wants_routes=wants_routes, context=context)
        if use_llm is None:
            use_llm = self._llm is not None and _looks_complex(goal)
        if not use_llm or self._llm is None:
            return PlanProposal(plan=base, source="deterministic")

        try:
            response = self._llm.complete(LLMRequest(
                system=_PLANNER_SYSTEM,
                messages=[{"role": "user",
                           "content": self._planning_prompt(goal)}],
                temperature=0.0,
            ))
        except Exception as exc:  # noqa: BLE001 - a dead model is not a plan
            return PlanProposal(
                plan=base, source="model_rejected",
                warnings=[f"llm provider failed: {type(exc).__name__}: {exc}"])

        payload = parse_json_object(response.text or "", required_key="steps")
        if payload is None:
            return PlanProposal(
                plan=base, source="model_rejected",
                warnings=["the model did not return a JSON object with 'steps'"])

        plan, issues, warnings = self._plan_from_payload(payload, goal)
        if issues:
            return PlanProposal(
                plan=base, source="model_rejected",
                warnings=warnings + issues)
        return PlanProposal(plan=plan, source="model", warnings=warnings)

    def _base_plan(self, goal: str, *, identity_input: dict[str, Any] | None,
                   smiles: str, document_id: str, wants_routes: bool,
                   context: dict[str, Any] | None) -> ResearchPlan:
        steps: list[ResearchPlanStep] = []
        identity_id = ""

        steps.append(ResearchPlanStep(
            action=ResearchPlanAction.ASSESS_KNOWLEDGE,
            step_id="assess-knowledge",
            tool="get_existing_knowledge",
            reason="先读已有知识：它决定本次研究要补什么，而不是重复已知。",
            expected_output="已有知识或明确的“没有知识库/没有记录”。",
            completion="知识状态已读出（哪怕结论是未知）。",
        ))

        if identity_input:
            identity_id = "resolve-identity"
            steps.append(ResearchPlanStep(
                action=ResearchPlanAction.RESOLVE_IDENTITY,
                step_id=identity_id,
                tool="resolve_chemical_identity",
                arguments=dict(identity_input),
                reason="先把用户给的名称/结构变成确定的化学身份。",
                expected_output="已解析结构，或明确的未解析状态与候选。",
                completion="身份状态为 resolved；否则必须由用户确认。",
            ))
        elif smiles:
            if wants_routes:
                # Retrosynthesis needs an established identity; a SMILES is a
                # deterministic input for that, not an established fact.
                identity_id = "resolve-identity"
                steps.append(ResearchPlanStep(
                    action=ResearchPlanAction.RESOLVE_IDENTITY,
                    step_id=identity_id,
                    tool="resolve_chemical_identity",
                    arguments={"kind": "SMILES", "value": smiles},
                    reason="逆合成依赖确定的化学身份，先解析这个 SMILES。",
                    expected_output="已解析结构或明确未解析状态。",
                    completion="身份状态为 resolved。",
                ))
            else:
                steps.append(ResearchPlanStep(
                    action=ResearchPlanAction.ANALYZE_STRUCTURE,
                    step_id="analyze-structure",
                    tool="analyze_molecule",
                    arguments={"smiles": smiles},
                    reason="用户直接给了结构，先做确定性计算（计算不是实验事实）。",
                    expected_output="规范 SMILES、分子式、分子量等计算结果。",
                    completion="计算返回。",
                ))

        query = ((identity_input or {}).get("value") or smiles or goal).strip()
        steps.append(ResearchPlanStep(
            action=ResearchPlanAction.SEARCH_LITERATURE,
            step_id="search-literature",
            tool="search_literature",
            arguments={"query": query},
            depends_on=[identity_id] if identity_id else [],
            reason="有界文献发现：只用于定位文献，不构成实验证据。",
            expected_output="候选论文与检索结果状态（命中/未命中/不可用）。",
            completion="检索完成；无命中也是完成，不是失败。",
        ))

        if document_id:
            steps.append(ResearchPlanStep(
                action=ResearchPlanAction.INSPECT_DOCUMENT,
                step_id="inspect-document",
                tool="get_document_evidence",
                arguments={"document_id": document_id},
                reason="用户提供了本地文档，先在其正文中定位证据。",
                expected_output="文档中的证据片段与 id。",
                completion="文档被读取；没有命中则记录为未命中。",
            ))
            steps.append(ResearchPlanStep(
                action=ResearchPlanAction.EXTRACT_REACTION_EVIDENCE,
                step_id="extract-reactions",
                tool="extract_reactions",
                depends_on=["inspect-document"],
                arguments={"document_id": document_id},
                reason="只从已读到的文档证据中抽取反应条件。",
                expected_output="带出处与冲突标记的结构化反应记录。",
                completion="抽取完成（包括“没有可抽取内容”）。",
            ))

        if wants_routes:
            steps.append(ResearchPlanStep(
                action=ResearchPlanAction.RETROSYNTHESIZE,
                step_id="retrosynthesize",
                tool="retrosynthesize",
                arguments={"target_smiles": smiles} if smiles else {},
                depends_on=[identity_id] if identity_id else [],
                reason="目标需要候选路线，且路线必须建立在已解析身份上。",
                expected_output="候选路线及其出处；不是可行性结论。",
                completion="路线检索完成（无路线也不等于化学上不可能）。",
            ))

        steps.append(ResearchPlanStep(
            action=ResearchPlanAction.REPORT,
            step_id="report",
            reason="把已完成的、未命中的、不可用的部分分开报告。",
            expected_output="研究结果、缺口与限制的说明。",
            completion="报告生成。",
        ))

        if len(steps) > self.max_steps:
            # Keep the report: a truncated run must still be allowed to say
            # what it did not do.
            keep = steps[: max(0, self.max_steps - 1)]
            if steps[-1].step_id == "report" and all(
                    s.step_id != "report" for s in keep):
                keep.append(steps[-1])
            steps = keep[: self.max_steps]

        return ResearchPlan(goal=goal, steps=steps, wants_latest=False)

    # -- validation ---------------------------------------------------------

    def validate_plan(self, plan: ResearchPlan) -> list[str]:
        """Every reason this plan cannot be executed as written."""
        return self._validate(plan, self.max_steps)

    def adopt_plan(self, plan: ResearchPlan) -> tuple[ResearchPlan, list[str]]:
        """Normalize an externally built plan (missing ids, alias tools)."""
        adopted = plan.model_copy(deep=True)
        warnings: list[str] = []
        for index, step in enumerate(adopted.steps, start=1):
            if not step.step_id:
                step.step_id = f"step-{index}"
            canonical = self._canonical(step.tool) if step.tool else None
            if canonical and canonical != step.tool:
                warnings.append(
                    f"{step.step_id}: alias {step.tool!r} canonicalized to "
                    f"{canonical!r}")
                step.tool = canonical
            fallback = (self._canonical(step.fallback_tool)
                        if step.fallback_tool else None)
            if fallback and fallback != step.fallback_tool:
                warnings.append(
                    f"{step.step_id}: fallback alias {step.fallback_tool!r} "
                    f"canonicalized to {fallback!r}")
                step.fallback_tool = fallback
        return adopted, warnings

    def _validate(self, plan: ResearchPlan, max_steps: int,
                  existing_ids: set[str] | frozenset[str] = frozenset()
                  ) -> list[str]:
        issues: list[str] = []
        if not plan.steps:
            issues.append("a plan needs at least one step")
        if len(plan.steps) > max_steps:
            issues.append(
                f"plan has {len(plan.steps)} steps; the maximum is {max_steps}")

        ids = [step.step_id for step in plan.steps]
        if any(not step_id for step_id in ids):
            issues.append("every step needs a non-empty step_id")
        duplicates = {i for i in ids if i and ids.count(i) > 1}
        if duplicates:
            issues.append(f"duplicate step ids: {', '.join(sorted(duplicates))}")

        known = {step.step_id for step in plan.steps if step.step_id}
        known |= set(existing_ids)
        for step in plan.steps:
            label = step.step_id or "<unnamed>"
            if step.tool:
                spec = self._spec(step.tool)
                if spec is None:
                    issues.append(f"{label}: unregistered tool {step.tool!r}")
                else:
                    if getattr(spec, "alias_of", None):
                        issues.append(
                            f"{label}: tool {step.tool!r} is an alias of "
                            f"{spec.alias_of!r}; use the canonical name")
                    issues.extend(
                        f"{label}: {issue}"
                        for issue in self._argument_issues(spec, step.arguments))
            for dep in step.depends_on:
                if dep == step.step_id:
                    issues.append(f"{label}: depends on itself")
                elif dep not in known:
                    issues.append(
                        f"{label}: dependency {dep!r} does not exist")
            if step.depends_on and step.depends_on[0] == step.step_id:
                continue  # already reported; cycle check would loop
            if step.fallback_tool:
                fspec = self._spec(step.fallback_tool)
                if fspec is None:
                    issues.append(
                        f"{label}: unregistered fallback tool "
                        f"{step.fallback_tool!r}")
                else:
                    issues.extend(
                        f"{label} fallback: {issue}"
                        for issue in self._argument_issues(
                            fspec, step.fallback_arguments))

        issues.extend(_cycle_issues(plan))
        return issues

    def _argument_issues(self, spec: Any,
                         arguments: dict[str, Any]) -> list[str]:
        """Strict schema check: unknown keys, bad types, missing required.

        An empty argument object is allowed at plan time -- it declares that
        the step is waiting for input, and the decision gate asks for the
        missing field rather than letting an invalid call through.
        """
        if not arguments:
            return []
        schema = getattr(spec, "input_schema", {}) or {}
        properties = set(schema.get("properties", {}))
        unknown = sorted(set(arguments) - properties)
        if unknown:
            return [f"unknown argument(s): {', '.join(unknown)}"]
        try:
            spec.arguments_model.model_validate(arguments)
        except Exception as exc:  # noqa: BLE001 - a schema check, not a crash
            return [f"invalid arguments: {exc}"]
        return []

    # -- model-assisted planning -------------------------------------------

    def _plan_from_payload(self, payload: dict, goal: str,
                           existing_ids: set[str] | frozenset[str] = frozenset()
                           ) -> tuple[ResearchPlan, list[str], list[str]]:
        issues: list[str] = []
        warnings: list[str] = []
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            return (ResearchPlan(goal=goal), ["'steps' must be a non-empty list"],
                    warnings)
        if len(raw_steps) > self.max_steps:
            return (ResearchPlan(goal=goal),
                    [f"the model proposed {len(raw_steps)} steps; the maximum "
                     f"is {self.max_steps}"], warnings)

        steps: list[ResearchPlanStep] = []
        for index, raw in enumerate(raw_steps, start=1):
            if not isinstance(raw, dict):
                issues.append(f"step {index}: not an object")
                continue
            label = str(raw.get("step_id") or f"step {index}")
            tool_raw = str(raw.get("tool") or "")
            canonical = self._canonical(tool_raw) if tool_raw else None
            if tool_raw and canonical is None:
                issues.append(f"{label}: unregistered tool {tool_raw!r}")
                continue
            if canonical and canonical != tool_raw:
                warnings.append(
                    f"{label}: alias {tool_raw!r} canonicalized to {canonical!r}")

            action_raw = raw.get("action") or _ACTION_FOR_TOOL.get(
                canonical or "", "")
            try:
                action = (action_raw if isinstance(action_raw, ResearchPlanAction)
                          else ResearchPlanAction(str(action_raw)))
            except ValueError:
                issues.append(
                    f"{label}: action {action_raw!r} is not a research action")
                continue

            arguments = raw.get("arguments") or {}
            if not isinstance(arguments, dict):
                issues.append(f"{label}: arguments must be an object")
                continue
            depends_on = raw.get("depends_on") or []
            if not isinstance(depends_on, list) or not all(
                    isinstance(d, str) for d in depends_on):
                issues.append(f"{label}: depends_on must be a list of strings")
                continue
            fallback_raw = str(raw.get("fallback_tool") or "")
            fallback = self._canonical(fallback_raw) if fallback_raw else ""
            fallback_arguments = raw.get("fallback_arguments") or {}
            if not isinstance(fallback_arguments, dict):
                issues.append(f"{label}: fallback_arguments must be an object")
                continue
            if fallback_raw and fallback is None:
                issues.append(
                    f"{label}: unregistered fallback tool {fallback_raw!r}")
                continue

            steps.append(ResearchPlanStep(
                action=action,
                step_id=str(raw.get("step_id") or ""),
                tool=canonical or "",
                arguments=arguments,
                depends_on=list(depends_on),
                reason=str(raw.get("reason") or ""),
                expected_output=str(raw.get("expected_output") or ""),
                completion=str(raw.get("completion") or ""),
                fallback_tool=fallback or "",
                fallback_arguments=fallback_arguments,
            ))

        plan = ResearchPlan(goal=goal, steps=steps)
        issues.extend(self._validate(plan, self.max_steps, existing_ids))
        return plan, issues, warnings

    def _planning_prompt(self, goal: str) -> str:
        tools = []
        for entry in self._registry.describe():
            if entry.get("alias_of"):
                continue
            schema = entry.get("input_schema") or {}
            tools.append({
                "name": entry["name"],
                "description": (entry.get("description") or "")[:200],
                "arguments": sorted(schema.get("properties", {})),
                "required": list(schema.get("required", [])),
            })
        return (
            f"Research goal:\n{goal}\n\n"
            f"Registered canonical tools (JSON):\n"
            f"{json.dumps(tools, ensure_ascii=False, indent=2)}\n\n"
            f"Propose at most {self.max_steps} steps. Return ONLY a JSON "
            f"object: {{\"steps\": [{{\"step_id\": str, \"action\": str, "
            f"\"tool\": str, \"reason\": str, \"arguments\": object, "
            f"\"depends_on\": [str], \"expected_output\": str, "
            f"\"completion\": str, \"fallback_tool\": str, "
            f"\"fallback_arguments\": object}}]}}. Use only the listed tools "
            f"and valid argument names. Dependencies must reference existing "
            f"step ids; no cycles. Never state a chemical fact."
        )

    # -- decisions ----------------------------------------------------------

    def start(self, session: PlanSession) -> PlanDecision:
        """The first decision: which step may run now."""
        return self.decide(session)

    def decide(self, session: PlanSession, step_id: str | None = None,
               observation: Observation | None = None, *,
               allow_model_revision: bool = False) -> PlanDecision:
        """Read one observation (or none) and choose the next move.

        `allow_model_revision` is off by default so an ordinary tool result
        never triggers an extra model call.
        """
        session.decisions += 1
        if session.decisions > self.max_decisions:
            return self._finish(
                session, PlanDecisionKind.BLOCKED,
                f"decision budget exhausted ({self.max_decisions})")

        try:
            if observation is not None:
                if not step_id:
                    return self._finish(
                        session, PlanDecisionKind.BLOCKED,
                        "an observation needs the step it belongs to")
                step = self._step(session, step_id)
                if step is None:
                    return self._finish(
                        session, PlanDecisionKind.BLOCKED,
                        f"unknown step {step_id!r}")
                self._record(session, step, observation)
                decision = self._after(session, step, observation,
                                       allow_model_revision)
            else:
                decision = self._next(session, trigger=None)
        except _BudgetExceeded as exc:
            return self._finish(session, PlanDecisionKind.BLOCKED, str(exc))

        if observation is not None and not decision.executed_step_id:
            decision.executed_step_id = step.step_id
        decision.revision = session.plan.revision
        decision.decisions_used = session.decisions
        return decision

    def _after(self, session: PlanSession, step: ResearchPlanStep,
               observation: Observation,
               allow_model_revision: bool) -> PlanDecision:
        kind = observation.kind

        if kind is ObservationKind.SUCCESS:
            if step.action is ResearchPlanAction.REPORT:
                return self._final(session, observation, step)
            return self._next(session, trigger=observation)

        if kind is ObservationKind.NEEDS_INPUT:
            decision = self._needs_input(session, step, observation)
            return self._revision_or(decision, session, step, observation,
                                     allow_model_revision)

        if kind is ObservationKind.NO_RESULT:
            decision = self._use_fallback(
                session, step, observation,
                f"{step.step_id} 查询完成但无命中；按计划声明的替代工具调整一次")
            if decision is not None:
                return decision
            return self._next(session, trigger=observation,
                              context="查询完成但无命中（这不是服务不可用，"
                                      "也不是执行失败）")

        if kind is ObservationKind.PARTIAL:
            missing = _missing_branches(observation)
            if missing:
                note = (f"{step.step_id}: 部分结果；缺失分支："
                        f"{'; '.join(missing)}")
                if note not in session.notes:
                    session.notes.append(note)
            decision = self._next(
                session, trigger=observation,
                context="已有部分可靠结果，保留并记录缺失分支")
            if decision.kind is PlanDecisionKind.COMPLETE:
                decision.partial = True
                decision.reason += f"；部分完成：{'; '.join(missing)}" if missing else ""
            return decision

        if kind is ObservationKind.UNAVAILABLE:
            decision = self._use_fallback(
                session, step, observation,
                f"{step.step_id} 的工具/数据源不可用；按计划尝试替代数据源")
            if decision is not None:
                return decision
            decision = self._next(
                session, trigger=observation,
                context="工具或数据源不可用（UNAVAILABLE，不是没有结果）")
            return self._revision_or(decision, session, step, observation,
                                     allow_model_revision)

        if kind is ObservationKind.ERROR:
            decision = self._use_fallback(
                session, step, observation,
                f"{step.step_id} 执行出错；不重试，改用计划声明的安全替代")
            if decision is not None:
                return decision
            decision = self._next(
                session, trigger=observation,
                context="工具执行失败（ERROR，不是没有结果）")
            return self._revision_or(decision, session, step, observation,
                                     allow_model_revision)

        return self._finish(session, PlanDecisionKind.BLOCKED,
                            f"unhandled observation kind {kind.value}",
                            observation, step)

    def _needs_input(self, session: PlanSession, step: ResearchPlanStep,
                     observation: Observation) -> PlanDecision:
        missing = list(observation.missing_requirements)
        codes = set(missing)

        # Approval and user selection are the two blockers no other step can
        # resolve for the user.
        if "approval" in codes:
            return self._finish(
                session, PlanDecisionKind.ASK_USER,
                f"{step.step_id} 需要用户授权后才会执行（持久化副作用）。",
                observation, step, needs_user_input=True,
                missing=missing, requires_approval=True,
                next_step_id=step.step_id)
        if "user_selection" in codes:
            return self._finish(
                session, PlanDecisionKind.ASK_USER,
                f"{step.step_id} 需要用户明确选择，计划不能替用户选。",
                observation, step, needs_user_input=True, missing=missing)

        # A missing fact can be established by another tool: plan it first.
        identity_codes = {"resolved_identity", "identity_resolved"}
        if codes & identity_codes:
            return self._plan_identity(session, step, observation)
        if "routes_held" in codes:
            return self._plan_routes(session, step, observation)

        # Arguments can sometimes be completed from what the session knows.
        argument_codes = {"valid_arguments", "invalid_arguments"}
        explicit_fields = [m for m in missing
                           if m not in _KNOWN_REQUIREMENTS]
        if codes & argument_codes or explicit_fields:
            return self._plan_arguments(session, step, observation,
                                        explicit_fields or missing)

        if codes & {"network_access", "model_access"}:
            decision = self._use_fallback(
                session, step, observation,
                f"{step.step_id} 被策略禁止使用网络/模型；按计划改用本地替代")
            if decision is not None:
                return decision
            return self._finish(
                session, PlanDecisionKind.BLOCKED,
                f"{step.step_id} 需要的访问权限被策略禁止，且计划中没有可用的"
                f"替代数据源（环境/策略受限）。",
                observation, step, missing=missing)

        if "valid_query" in codes:
            return self._finish(
                session, PlanDecisionKind.ASK_USER,
                f"{step.step_id} 的检索条件无效；计划不会凭空改写查询，需要用户"
                f"或调用方提供有效条件。",
                observation, step, needs_user_input=True, missing=missing)

        return self._finish(
            session, PlanDecisionKind.ASK_USER,
            f"{step.step_id} 需要补充信息后才能继续。",
            observation, step, needs_user_input=True, missing=missing)

    def _plan_identity(self, session: PlanSession,
                       step: ResearchPlanStep,
                       observation: Observation) -> PlanDecision:
        existing = self._pending_step_using(session, "resolve_chemical_identity")
        if existing is not None:
            return self._guard(session, existing, observation)
        if self._canonical("resolve_chemical_identity") is None:
            return self._finish(
                session, PlanDecisionKind.ASK_USER,
                f"{step.step_id} 需要确定的化学身份，但当前注册表中没有身份"
                f"解析工具；需要用户补充。",
                observation, step, needs_user_input=True,
                missing=list(observation.missing_requirements))

        arguments = _identity_arguments(session, step)
        change = self._add_step(
            session,
            ResearchPlanStep(
                action=ResearchPlanAction.RESOLVE_IDENTITY,
                step_id=self._unique_id(session, "resolve-identity"),
                tool="resolve_chemical_identity",
                arguments=arguments,
                reason="当前步骤依赖确定的化学身份，先规划身份解析。",
                expected_output="已解析结构或明确的未解析状态。",
                completion="身份状态为 resolved；否则请用户确认。",
            ),
            reason="missing resolved identity",
        )
        new_step = self._step(session, change.step_id)
        if new_step is None:  # pragma: no cover - the add just succeeded
            raise _BudgetExceeded("identity step could not be added")
        return self._guard(session, new_step, observation, changes=[change])

    def _plan_routes(self, session: PlanSession,
                     step: ResearchPlanStep,
                     observation: Observation) -> PlanDecision:
        existing = self._pending_step_using(session, "retrosynthesize")
        if existing is not None:
            return self._guard(session, existing, observation)
        if self._canonical("retrosynthesize") is None:
            return self._finish(
                session, PlanDecisionKind.ASK_USER,
                f"{step.step_id} 需要候选路线，但当前注册表中没有逆合成工具；"
                f"需要用户或环境提供。",
                observation, step, needs_user_input=True,
                missing=list(observation.missing_requirements))
        change = self._add_step(
            session,
            ResearchPlanStep(
                action=ResearchPlanAction.RETROSYNTHESIZE,
                step_id=self._unique_id(session, "retrosynthesize"),
                tool="retrosynthesize",
                arguments={},
                reason="当前步骤需要候选路线，先规划逆合成。",
                expected_output="候选路线及其出处。",
                completion="路线检索完成。",
            ),
            reason="missing routes",
        )
        new_step = self._step(session, change.step_id)
        if new_step is None:  # pragma: no cover
            raise _BudgetExceeded("route step could not be added")
        return self._guard(session, new_step, observation, changes=[change])

    def _plan_arguments(self, session: PlanSession, step: ResearchPlanStep,
                        observation: Observation,
                        missing: list[str],
                        changes: list[PlanChange] | None = None) -> PlanDecision:
        changes = list(changes or [])
        spec = self._spec(step.tool) if step.tool else None
        if spec is None:
            return self._finish(
                session, PlanDecisionKind.ASK_USER,
                f"{step.step_id} 的参数不完整，且没有可用的工具声明。",
                observation, step, needs_user_input=True, missing=missing,
                changes=changes)

        filled, missing_fields = _fill_from_context(
            spec, step.arguments, session.context)
        if not missing_fields and filled != step.arguments:
            change = self._update_arguments(session, step, filled,
                                             "从会话上下文补齐参数")
            decision = self._guard(session, step, observation,
                                   changes + [change])
            decision.reason = (f"{step.step_id} 的参数已从会话上下文补齐；"
                               f"{decision.reason}")
            return decision
        if not missing_fields and self._arguments_valid(spec, filled):
            # The step never ran with these arguments; run it now.
            return self._guard(session, step, observation, changes)
        return self._finish(
            session, PlanDecisionKind.ASK_USER,
            f"{step.step_id} 需要补充参数：{', '.join(missing_fields or missing)}",
            observation, step, needs_user_input=True,
            missing=missing_fields or missing, changes=changes)

    # -- selection ----------------------------------------------------------

    def _next(self, session: PlanSession, *, trigger: Observation | None,
              context: str = "",
              changes: list[PlanChange] | None = None) -> PlanDecision:
        ready = self._ready_steps(session)
        if ready:
            decision = self._guard(session, ready[0], trigger)
            if changes:
                decision.changes.extend(changes)
            if context:
                decision.reason = f"{context}；{decision.reason}"
            return decision

        pending = [s for s in session.plan.steps if not session.done(s.step_id)]
        if pending:
            blocked = ", ".join(
                s.step_id for s in pending
                if any(session.outcomes.get(d) in _HARD_FAILURES
                       for d in s.depends_on))
            names = blocked or ", ".join(s.step_id for s in pending)
            return self._finish(
                session, PlanDecisionKind.BLOCKED,
                _join(context, f"没有可执行步骤：{names} 的前置步骤未成功"),
                trigger)

        decision = self._final(session, trigger, context=context)
        return decision

    def _final(self, session: PlanSession, trigger: Observation | None,
               executed: ResearchPlanStep | None = None,
               context: str = "") -> PlanDecision:
        """Complete or blocked, decided from the recorded outcomes.

        A hard failure anywhere is never reported as COMPLETE, even when the
        report step itself succeeded -- the report can still exist, but the
        run ended with a limitation.
        """
        hard = [sid for sid, kind in session.outcomes.items()
                if kind in _HARD_FAILURES]
        if hard:
            return self._finish(
                session, PlanDecisionKind.BLOCKED,
                _join(context, f"计划已无剩余可执行步骤，但以下步骤未成功："
                               f"{', '.join(sorted(hard))}"),
                trigger, executed)

        partial = any(kind is ObservationKind.PARTIAL
                      for kind in session.outcomes.values())
        misses = sum(1 for kind in session.outcomes.values()
                     if kind is ObservationKind.NO_RESULT)
        reason = _join(context, "计划中的所有步骤都已执行完毕")
        if misses:
            reason += f"；{misses} 个查询完成但无命中（这是结果，不是失败）"
        decision = self._finish(session, PlanDecisionKind.COMPLETE, reason,
                                trigger, executed)
        decision.partial = partial
        return decision

    def _guard(self, session: PlanSession, step: ResearchPlanStep,
               trigger: Observation | None,
               changes: list[PlanChange] | None = None) -> PlanDecision:
        """Check the declared policy before handing a step to the executor."""
        spec = self._spec(step.tool) if step.tool else None
        changes = list(changes or [])

        if step.tool and spec is None:
            return self._finish(
                session, PlanDecisionKind.BLOCKED,
                f"{step.step_id}: 工具 {step.tool!r} 未注册，不能执行。",
                trigger, changes=changes)

        if spec is not None:
            missing = [fact for fact in (getattr(spec, "requires", ()) or ())
                       if not self._fact_held(session, fact)]
            if missing:
                fact = missing[0]
                tool = _FACT_TOOLS.get(fact)
                existing = (self._pending_step_using(session, tool)
                            if tool else None)
                if existing is not None:
                    return self._guard(session, existing, trigger,
                                       changes=changes)
                if tool and self._canonical(tool):
                    change = self._add_step(
                        session,
                        ResearchPlanStep(
                            action=_ACTION_FOR_TOOL.get(
                                tool, ResearchPlanAction.ASSESS_KNOWLEDGE),
                            step_id=self._unique_id(
                                session, tool.replace("_", "-")),
                            tool=tool,
                            reason=f"{step.step_id} 需要事实 {fact}，先补齐。",
                            expected_output=f"建立 {fact}。",
                            completion=f"会话事实包含 {fact}。",
                        ),
                        reason=f"missing fact {fact}",
                    )
                    new_step = self._step(session, change.step_id)
                    if new_step is not None:
                        return self._guard(session, new_step, trigger,
                                           changes=changes + [change])
                return self._finish(
                    session, PlanDecisionKind.ASK_USER,
                    f"{step.step_id} 需要前置事实 {fact}，计划中没有补齐它的"
                    f"工具，需要用户或环境提供。",
                    trigger, needs_user_input=True, missing=missing,
                    changes=changes)

        if (spec is not None
                and getattr(self._registry, "enforce_policy", False)):
            policy = getattr(self._registry, "policy", None)
            access = set(getattr(spec, "access", ()) or ())
            denied: list[str] = []
            if policy is not None:
                if ("network" in access and not policy.allow_network
                        and not getattr(spec, "network_optional", False)):
                    denied.append("network_access")
                if ("model" in access and not policy.allow_model_calls
                        and not getattr(spec, "model_optional", False)):
                    denied.append("model_access")
            if denied:
                if trigger is not None:
                    decision = self._use_fallback(
                        session, step, trigger,
                        f"{step.step_id} 的资源访问被策略禁止；按计划改用替代")
                    if decision is not None:
                        decision.changes = changes + decision.changes
                        return decision
                return self._finish(
                    session, PlanDecisionKind.BLOCKED,
                    f"{step.step_id} 需要的访问权限被当前执行策略禁止"
                    f"（{', '.join(denied)}），计划不会绕过该限制。",
                    trigger, missing=denied, changes=changes)

        if spec is not None and getattr(spec, "approval", "none") == "required" \
                and step.step_id not in session.approvals:
            return self._finish(
                session, PlanDecisionKind.ASK_USER,
                f"{step.step_id} 的工具 {step.tool} 会修改持久化数据，"
                f"执行前需要用户授权。",
                trigger, needs_user_input=True, missing=["approval"],
                requires_approval=True, next_step_id=step.step_id,
                changes=changes)

        if spec is not None and not self._arguments_valid(spec, step.arguments):
            # Empty arguments are a declared gap, not an invalid plan.
            return self._plan_arguments(
                session, step,
                trigger or _placeholder_observation(step),
                ["valid_arguments"], changes)

        return self._finish(
            session, PlanDecisionKind.CONTINUE,
            f"下一步：{step.step_id}（{step.action.value}）",
            trigger, next_step_id=step.step_id, changes=changes)

    def _ready_steps(self, session: PlanSession) -> list[ResearchPlanStep]:
        ready = []
        for step in session.plan.steps:
            if session.done(step.step_id):
                continue
            if self._call_signature(step.tool,
                                    step.arguments) in session.executed_calls:
                continue  # this exact call already happened; never re-run it
            if all(session.outcomes.get(dep) in _DEPENDENCY_SATISFIED
                   for dep in step.depends_on):
                ready.append(step)
        return ready

    def _pending_step_using(self, session: PlanSession,
                            tool: str | None) -> ResearchPlanStep | None:
        if not tool:
            return None
        for step in session.plan.steps:
            if step.tool == tool and not session.done(step.step_id):
                return step
        return None

    # -- recording ----------------------------------------------------------

    def _record(self, session: PlanSession, step: ResearchPlanStep,
                observation: Observation) -> None:
        session.outcomes[step.step_id] = observation.kind
        session.observations[step.step_id] = observation
        if step.tool:
            signature = self._call_signature(step.tool, step.arguments)
            if signature not in session.executed_calls:
                session.executed_calls.append(signature)
        if observation.kind is ObservationKind.SUCCESS:
            spec = self._spec(step.tool) if step.tool else None
            for fact in (getattr(spec, "sets_facts", ()) or ()):
                self._add_fact(session, fact)
            rule = getattr(spec, "fact_rule", None)
            if callable(rule):
                try:
                    for fact in rule(observation.data):
                        self._add_fact(session, fact)
                except Exception:  # noqa: BLE001 - a fact rule is not a failure
                    pass

    @staticmethod
    def _add_fact(session: PlanSession, fact: str) -> None:
        if fact and fact not in session.facts:
            session.facts.append(fact)

    def _fact_held(self, session: PlanSession, fact: str) -> bool:
        if fact in session.facts:
            return True
        registry_facts = getattr(self._registry, "facts", None)
        if registry_facts is not None:
            try:
                return fact in registry_facts
            except TypeError:  # pragma: no cover - defensive
                return False
        return False

    # -- plan edits ---------------------------------------------------------

    def _add_step(self, session: PlanSession, step: ResearchPlanStep,
                  *, reason: str) -> PlanChange:
        if len(session.plan.steps) >= self.max_steps:
            raise _BudgetExceeded(
                f"step budget exhausted ({self.max_steps}); the plan cannot "
                f"grow further")
        if session.revisions >= self.max_revisions:
            raise _BudgetExceeded(
                f"revision budget exhausted ({self.max_revisions}); the plan "
                f"will not be changed again")
        if any(existing.step_id == step.step_id
               for existing in session.plan.steps):
            step.step_id = self._unique_id(session, step.step_id)
        session.plan.steps.append(step)
        self._revised(session)
        return PlanChange(kind=PlanChangeKind.ADD_STEP, step_id=step.step_id,
                          after=step.model_dump(mode="json"), reason=reason)

    def _update_arguments(self, session: PlanSession, step: ResearchPlanStep,
                          arguments: dict[str, Any], reason: str) -> PlanChange:
        if session.revisions >= self.max_revisions:
            raise _BudgetExceeded(
                f"revision budget exhausted ({self.max_revisions})")
        before = dict(step.arguments)
        step.arguments = dict(arguments)
        self._revised(session)
        return PlanChange(kind=PlanChangeKind.UPDATE_ARGUMENTS,
                          step_id=step.step_id, before=before,
                          after=dict(arguments), reason=reason)

    @staticmethod
    def _revised(session: PlanSession) -> None:
        session.revisions += 1
        session.plan.revision += 1

    def _fallback(self, session: PlanSession, step: ResearchPlanStep,
                  observation: Observation) -> PlanChange | None:
        """Try the step's declared fallback, once, if it is runnable."""
        if not step.fallback_tool:
            return None
        if step.step_id in session.fallback_used:
            return None
        canonical = self._canonical(step.fallback_tool)
        if canonical is None:
            return None
        spec = self._spec(canonical)
        if spec is None:
            return None
        arguments = dict(step.fallback_arguments)
        if arguments:
            if self._argument_issues(spec, arguments):
                return None
            if not self._arguments_valid(spec, arguments):
                return None
        if self._call_signature(canonical,
                                arguments) in session.executed_calls:
            return None
        new_id = self._unique_id(session, f"{step.step_id}-alt")
        session.fallback_used.append(step.step_id)
        fallback = ResearchPlanStep(
            action=step.action,
            step_id=new_id,
            tool=canonical,
            arguments=arguments,
            depends_on=list(step.depends_on),
            reason=f"{step.step_id} 未得到可用结果；按计划声明的替代工具重试一次。",
            expected_output=step.expected_output,
            completion=step.completion,
        )
        return self._add_step(
            session, fallback,
            reason=f"fallback after {observation.kind.value} from "
                   f"{step.tool}")

    def _use_fallback(self, session: PlanSession, step: ResearchPlanStep,
                      observation: Observation, context: str) -> PlanDecision | None:
        """Add the declared fallback and hand it through the same guard."""
        change = self._fallback(session, step, observation)
        if change is None:
            return None
        new_step = self._step(session, change.step_id)
        if new_step is None:  # pragma: no cover - _add_step just appended it
            return None
        decision = self._guard(session, new_step, observation, [change])
        decision.reason = _join(context, decision.reason)
        return decision

    def _unique_id(self, session: PlanSession, base: str) -> str:
        existing = {step.step_id for step in session.plan.steps}
        candidate = base
        index = 2
        while candidate in existing:
            candidate = f"{base}-{index}"
            index += 1
        return candidate

    @staticmethod
    def _step(session: PlanSession, step_id: str) -> ResearchPlanStep | None:
        for step in session.plan.steps:
            if step.step_id == step_id:
                return step
        return None

    # -- model revision -----------------------------------------------------

    def _revision_or(self, decision: PlanDecision, session: PlanSession,
                     step: ResearchPlanStep, observation: Observation,
                     allowed: bool) -> PlanDecision:
        """Escalate a BLOCKED decision to one constrained model revision."""
        if not allowed or decision.kind is not PlanDecisionKind.BLOCKED:
            return decision
        changes = self._model_revision(session, step, observation)
        if not changes:
            return decision
        revised = self._next(session, trigger=observation)
        revised.changes = changes + revised.changes
        return revised

    def _model_revision(self, session: PlanSession,
                        step: ResearchPlanStep,
                        observation: Observation) -> list[PlanChange]:
        """One constrained model call, only when a caller explicitly allows it.

        The model may add steps; it may not edit or drop existing ones. Every
        proposed step goes through the same validation as the initial plan.
        """
        if self._llm is None or session.revisions >= self.max_revisions:
            return []
        try:
            response = self._llm.complete(LLMRequest(
                system=_PLANNER_SYSTEM,
                messages=[{"role": "user", "content": self._revision_prompt(
                    session, step, observation)}],
                temperature=0.0,
            ))
        except Exception:  # noqa: BLE001 - a dead model adds nothing
            return []
        payload = parse_json_object(response.text or "", required_key="steps")
        if payload is None:
            return []
        proposed, issues, _ = self._plan_from_payload(
            payload, session.plan.goal,
            {s.step_id for s in session.plan.steps})
        if issues:
            return []
        # Rename collisions first, so dependencies between the revised steps
        # stay intact.
        reserved = {s.step_id for s in session.plan.steps}
        renamed: dict[str, str] = {}
        for candidate in proposed.steps:
            if candidate.step_id in reserved:
                base = candidate.step_id
                index = 2
                while f"{base}-{index}" in reserved:
                    index += 1
                renamed[base] = f"{base}-{index}"
                candidate.step_id = renamed[base]
            reserved.add(candidate.step_id)
        for candidate in proposed.steps:
            candidate.depends_on = [renamed.get(d, d)
                                    for d in candidate.depends_on]

        changes: list[PlanChange] = []
        for candidate in proposed.steps:
            if len(session.plan.steps) >= self.max_steps:
                break
            if session.revisions >= self.max_revisions:
                break
            try:
                changes.append(self._add_step(
                    session, candidate,
                    reason=f"model revision after {observation.kind.value}"))
            except _BudgetExceeded:
                break
        return changes

    def _revision_prompt(self, session: PlanSession, step: ResearchPlanStep,
                         observation: Observation) -> str:
        plan = json.dumps(session.plan.model_dump(mode="json"),
                          ensure_ascii=False, default=str)
        trigger = json.dumps(observation.to_context(), ensure_ascii=False,
                             default=str)[:2000]
        return (
            f"Research goal:\n{session.plan.goal}\n\n"
            f"Current plan:\n{plan}\n\n"
            f"Step {step.step_id} just returned:\n{trigger}\n\n"
            f"Propose at most {max(0, self.max_steps - len(session.plan.steps))} "
            f"ADDITIONAL steps that address the blocker. Use only registered "
            f"tools and valid argument names. Do not restate facts. Return "
            f"ONLY {{\"steps\": [...]}} in the same shape as the original "
            f"plan."
        )

    # -- helpers ------------------------------------------------------------

    def _spec(self, name: str) -> Any:
        getter = getattr(self._registry, "get", None)
        return getter(name) if callable(getter) else None

    def _canonical(self, name: str) -> str | None:
        spec = self._spec(name)
        if spec is None:
            return None
        return getattr(spec, "alias_of", None) or name

    @staticmethod
    def _call_signature(tool: str, arguments: dict[str, Any]) -> str:
        return f"{tool}|{json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False)}"

    def _arguments_valid(self, spec: Any,
                         arguments: dict[str, Any]) -> bool:
        if not arguments:
            schema = getattr(spec, "input_schema", {}) or {}
            return not schema.get("required")
        if self._argument_issues(spec, arguments):
            return False
        return True

    def _finish(self, session: PlanSession, kind: PlanDecisionKind, reason: str,
                trigger: Observation | None = None,
                executed: ResearchPlanStep | None = None, *,
                next_step_id: str = "", changes: list[PlanChange] | None = None,
                needs_user_input: bool = False,
                missing: list[str] | None = None,
                requires_approval: bool = False) -> PlanDecision:
        return PlanDecision(
            kind=kind,
            reason=reason,
            next_step_id=next_step_id,
            executed_step_id=executed.step_id if executed else "",
            changes=list(changes or []),
            needs_user_input=needs_user_input,
            missing_requirements=list(dict.fromkeys(missing or [])),
            requires_approval=requires_approval,
            trigger=trigger,
            revision=session.plan.revision,
            decisions_used=session.decisions,
        )


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------

#: Tokens the observer uses for "a person has to act", as opposed to a plain
#: argument field name.
_KNOWN_REQUIREMENTS = frozenset({
    "approval", "user_selection", "resolved_identity", "identity_resolved",
    "routes_held", "network_access", "model_access", "valid_arguments",
    "valid_query", "reactants_or_products",
})


def _identity_arguments(session: PlanSession,
                        step: ResearchPlanStep) -> dict[str, Any]:
    for key in ("identity_input",):
        value = session.context.get(key)
        if isinstance(value, dict) and value:
            return dict(value)
    if "kind" in session.context and "value" in session.context:
        return {"kind": session.context["kind"], "value": session.context["value"]}
    # The failed step's own arguments may already carry a structure.
    for key in ("smiles", "target_smiles"):
        if step.arguments.get(key):
            return {"kind": "SMILES", "value": step.arguments[key]}
    return {}


def _fill_from_context(spec: Any, arguments: dict[str, Any],
                       context: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    schema = getattr(spec, "input_schema", {}) or {}
    required = list(schema.get("required", []))
    filled = dict(arguments)
    missing: list[str] = []
    for name in required:
        if name in filled:
            continue
        if name in context:
            filled[name] = context[name]
        else:
            missing.append(name)
    return filled, missing


def _cycle_issues(plan: ResearchPlan) -> list[str]:
    edges = {step.step_id: [d for d in step.depends_on if d != step.step_id]
             for step in plan.steps}
    visiting: set[str] = set()
    visited: set[str] = set()
    found: list[str] = []

    def visit(node: str, path: list[str]) -> None:
        if node in visiting:
            found.append(" -> ".join(path + [node]))
            return
        if node in visited:
            return
        visiting.add(node)
        for dep in edges.get(node, []):
            visit(dep, path + [node])
        visiting.discard(node)
        visited.add(node)

    for node in edges:
        visit(node, [])
    return [f"dependency cycle: {cycle}" for cycle in found]


def _missing_branches(observation: Observation) -> list[str]:
    data = observation.data if isinstance(observation.data, dict) else {}
    for key in ("limitations", "warnings", "notes"):
        value = data.get(key)
        if isinstance(value, list) and value:
            return [str(item) for item in value if item]
    return []


def _placeholder_observation(step: ResearchPlanStep) -> Observation:
    """A trigger for a decision made before any call ran.

    The decision still records *why* it exists (arguments are incomplete);
    the observation is honestly marked as not having come from a tool result.
    """
    return Observation(
        kind=ObservationKind.NEEDS_INPUT,
        tool=step.tool or step.step_id,
        step_id=step.step_id,
        summary=f"{step.step_id} has incomplete arguments",
        raw_status="not_executed",
        error_code="valid_arguments",
        missing_requirements=["valid_arguments"],
        needs_user_input=True,
    )


def _join(prefix: str, suffix: str) -> str:
    return f"{prefix}；{suffix}" if prefix else suffix


_PLANNER_SYSTEM = (
    "You are the planning layer of a chemistry research system. You propose "
    "structured plans; you never execute tools and never state chemical facts. "
    "Every tool name, argument and dependency you use must come from the "
    "provided registry. Answer with JSON only."
)


__all__ = [
    "MAX_PLAN_DECISIONS",
    "MAX_PLAN_REVISIONS",
    "MAX_PLAN_STEPS",
    "PlanChange",
    "PlanChangeKind",
    "PlanDecision",
    "PlanDecisionKind",
    "PlanProposal",
    "PlanSession",
    "PlanValidationError",
    "Planner",
]

"""M2.0 -- the unified research decision protocol.

One model describes every move an AI Research Intelligence may make. The
three pre-M2.0 agent loops each invented its own reply shape (`AgentAction`
under two different system prompts, and a bare JSON object for evidence
work); this is the single contract that replaces them, and the only shape a
research decision may take.

Four rules the models hold, rather than a prompt asking nicely:

* **A decision is a closed sum, not a free object.** `ResearchDecision`
  carries one payload and its `kind` must name that payload. There is no
  field a model can fill that the validator does not look at, and
  `extra="forbid"` refuses a smuggled one. Prose is never a decision: a
  reply that does not parse into this model is rejected with a reason and
  never executed.
* **A tool call names a registered tool and a JSON object.** There is no
  code field, no expression, no path by which model text becomes an
  executable. The registry validates the arguments against the tool's own
  declared schema; the decision only *proposes*.
* **Evidence ids are references, never claims.** A decision may cite ids
  it has been shown; nothing here mints one, and the validator checks
  every cited id against the ids this run actually holds.
* **A hypothesis never becomes a fact here.** `HypothesisUpdate` moves a
  statement along the M1.5 `HypothesisStatus` line, and `SUPPORTED` still
  requires named supporting evidence -- enforced by the study context
  service, which is the only writer.

`DecisionRecord` is the audit row: what was decided, by which model under
which prompt and schema version, how the validator ruled, how long it took
and what it cost. It never contains the model's hidden reasoning -- only
the decision summary, the purpose and the cited ids, because a chain of
thought is not a research record.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from crea.core.models.study_context import HypothesisStatus
from crea.core.provenance import utc_now_iso

#: Moves only when the *meaning* of a field changes. The schema version is
#: recorded on every decision so a stored record stays readable.
RESEARCH_DECISION_SCHEMA_VERSION = "research-decision-v1"

#: The prompt contract a run was made under. Bump when the system prompt's
#: instructions change what a good decision looks like.
INTELLIGENCE_PROMPT_VERSION = "m2.0-intelligence-1"


class DecisionKind(str, Enum):
    """The six moves. Nothing else is a research decision."""

    CONTINUE_WITH_TOOL = "continue_with_tool"
    ASK_USER = "ask_user"
    UPDATE_HYPOTHESIS = "update_hypothesis"
    UPDATE_DIRECTION = "update_direction"
    COMPLETE = "complete"
    BLOCKED = "blocked"


class EvidenceSufficiency(str, Enum):
    """How far the gathered evidence goes. Declared, never inferred."""

    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class ToolCallRequest(BaseModel):
    """One proposed tool call: which tool, on what, and why.

    `purpose` and `expected_information` are the research reasons; a call
    with neither is a call the model cannot justify, and the validator
    says so rather than running it anyway.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str
    arguments: dict = Field(default_factory=dict)
    decision_summary: str
    purpose: str
    expected_information: str
    related_hypothesis_ids: list[str] = Field(default_factory=list)
    relevant_evidence_ids: list[str] = Field(default_factory=list)


class HypothesisUpdate(BaseModel):
    """One hypothesis statement: new or moved along the status line.

    `hypothesis_id` empty means "this is a new hypothesis"; a non-empty id
    must name one this study already holds. `proposed_status` is what the
    model asks for -- the study context still refuses `SUPPORTED` without
    named evidence, so an ambitious model cannot launder confidence into
    a fact.
    """

    model_config = ConfigDict(extra="forbid")

    #: Empty for a new hypothesis.
    hypothesis_id: str = ""
    hypothesis: str
    rationale_summary: str
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    uncertainty: str = ""
    next_verification_action: str = ""
    proposed_status: HypothesisStatus = HypothesisStatus.PROPOSED


class DirectionUpdate(BaseModel):
    """A change of research direction, with the result that forced it.

    Recorded as a decision even when no tool follows: a run that abandons
    a search line because three queries came back empty has changed course,
    and the trace has to show it.
    """

    model_config = ConfigDict(extra="forbid")

    previous_direction: str
    new_direction: str
    reason: str
    #: Tool results (by step id) that forced the change.
    triggering_step_ids: list[str] = Field(default_factory=list)
    relevant_evidence_ids: list[str] = Field(default_factory=list)
    #: Steps or lines of inquiry this change abandons, in the model's words.
    abandoned: list[str] = Field(default_factory=list)


class AskUserRequest(BaseModel):
    """A question only a person can answer.

    `options` is optional: some questions are open ("which of these two
    routes should we prioritise") and some are a choice. Either way the run
    pauses; it never answers its own question.
    """

    model_config = ConfigDict(extra="forbid")

    question: str
    why: str
    expected_information: str = ""
    options: list[str] = Field(default_factory=list)


class CompletionStatement(BaseModel):
    """Why the research stops, and what it is entitled to claim."""

    model_config = ConfigDict(extra="forbid")

    completion_reason: str
    achieved_objectives: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    evidence_sufficiency: EvidenceSufficiency = EvidenceSufficiency.INSUFFICIENT
    #: What this research did not establish, in the model's own words.
    limitations: list[str] = Field(default_factory=list)


class BlockedStatement(BaseModel):
    """Why no further research move is possible right now."""

    model_config = ConfigDict(extra="forbid")

    reason: str
    #: "identity_unresolved" | "tool_unavailable" | "no_tool_can_help" |
    #: "budget_exhausted" | "model_output_invalid" | ...
    blocker: str = "no_tool_can_help"
    details: str = ""
    relevant_evidence_ids: list[str] = Field(default_factory=list)


class ResearchDecision(BaseModel):
    """Exactly one move, with exactly the payload that move needs."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = RESEARCH_DECISION_SCHEMA_VERSION
    kind: DecisionKind

    tool_call: ToolCallRequest | None = None
    hypothesis_update: HypothesisUpdate | None = None
    direction_update: DirectionUpdate | None = None
    ask_user: AskUserRequest | None = None
    completion: CompletionStatement | None = None
    blocked: BlockedStatement | None = None

    #: What the most recent tool result means scientifically. Recorded per
    #: step; never replaces the `ToolResult` and never re-states it as fact.
    observation_interpretation: str = ""
    #: The step id the interpretation is about (empty on a first turn).
    interpretation_of_step: str = ""

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> "ResearchDecision":
        payloads = {
            DecisionKind.CONTINUE_WITH_TOOL: self.tool_call,
            DecisionKind.ASK_USER: self.ask_user,
            DecisionKind.UPDATE_HYPOTHESIS: self.hypothesis_update,
            DecisionKind.UPDATE_DIRECTION: self.direction_update,
            DecisionKind.COMPLETE: self.completion,
            DecisionKind.BLOCKED: self.blocked,
        }
        expected = payloads[self.kind]
        if expected is None:
            raise ValueError(
                f"kind {self.kind.value!r} needs its own payload; a decision "
                f"without one cannot be executed or audited")
        others = [key.value for key, value in payloads.items()
                  if key is not self.kind and value is not None]
        if others:
            raise ValueError(
                f"kind {self.kind.value!r} carries {', '.join(others)}; a "
                f"decision is one move, not several stitched together")
        return self


class DecisionOutcome(str, Enum):
    """How the validator ruled on one model reply. There is no "maybe"."""

    ACCEPTED = "accepted"
    #: Parsed, but the move is not legal here (unknown tool, bad arguments,
    #: a cited id this run never held). The decision was not executed.
    REJECTED = "rejected"
    #: The reply was not a `ResearchDecision` at all.
    UNPARSEABLE = "unparseable"
    #: The model call itself failed; no reply exists.
    MODEL_UNAVAILABLE = "model_unavailable"


class DecisionRecord(BaseModel):
    """One audited turn: what the model proposed and what the gate did.

    This is the observability contract (§ eight of the M2.0 brief). It
    carries provider, model, prompt version, schema version, the decision
    type and summary, the selected tool, the validator's ruling, timing,
    token usage when the provider reports it, and the observation it led
    to. It does not carry the model's private reasoning.
    """

    model_config = ConfigDict(extra="forbid")

    decision_id: str
    session_id: str = ""
    study_id: str = ""
    #: Turn index within the run, starting at 1.
    turn: int = 0
    model_provider: str = ""
    model_name: str = ""
    prompt_version: str = INTELLIGENCE_PROMPT_VERSION
    schema_version: str = RESEARCH_DECISION_SCHEMA_VERSION
    decision_type: DecisionKind | None = None
    #: What this turn proposed, in one sentence a reader can check.
    decision_summary: str = ""
    selected_tool: str = ""
    validation_result: DecisionOutcome = DecisionOutcome.UNPARSEABLE
    #: Why the gate ruled as it did -- the issues, or the reason it passed.
    validation_detail: str = ""
    at: str = Field(default_factory=utc_now_iso)
    duration_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    warnings: list[str] = Field(default_factory=list)
    #: The step this decision produced, when it executed a tool.
    associated_step_id: str = ""
    #: The full accepted decision, so a reader can re-check the gate.
    decision: ResearchDecision | None = None


class ScientificInterpretation(BaseModel):
    """One tool result, read by the model -- kept beside, never instead of,
    the result itself.

    `observation_kind` is the M1.2 reading (the machine's); `interpretation`
    is the model's scientific reading. They travel together so a later
    reader can see where the model added meaning and where it merely
    restated a status. Nothing here may be stored as a fact: an
    interpretation is a hypothesis-shaped statement about a tool result.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str
    tool: str
    observation_kind: str
    interpretation: str = ""
    #: Hypotheses this reading bears on, by id.
    related_hypothesis_ids: list[str] = Field(default_factory=list)
    #: Evidence ids the tool result actually carried.
    evidence_ids: list[str] = Field(default_factory=list)
    at: str = Field(default_factory=utc_now_iso)


__all__ = [
    "AskUserRequest",
    "BlockedStatement",
    "CompletionStatement",
    "DecisionKind",
    "DecisionOutcome",
    "DecisionRecord",
    "DirectionUpdate",
    "EvidenceSufficiency",
    "HypothesisUpdate",
    "INTELLIGENCE_PROMPT_VERSION",
    "RESEARCH_DECISION_SCHEMA_VERSION",
    "ResearchDecision",
    "ScientificInterpretation",
    "ToolCallRequest",
]

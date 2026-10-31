"""M2.0.1 -- the persistent audit trace of one AI research run.

A `DecisionRecord` is written per turn inside the process that ran the
research; this module is the *storage shape* of that trail, so a second
process can read it back, re-render the report and re-check the gate
without the first process still being alive. Two records, both keyed by
study first (isolation is the schema's job):

* `ResearchTurnTrace` -- one audited turn: the `DecisionRecord` plus the
  step it produced, the tool's arguments, the raw `ToolResult`, the M1.2
  `Observation` (as its serialization, without the in-memory `raw` handle)
  and the model's `ScientificInterpretation`. Tool result and
  interpretation travel in separate fields and are never merged: a reader
  must be able to see where the machine stopped and the model began.
* `ResearchRunTrace` -- the run's terminal summary: status, outcome, stop
  reason, budget actually consumed, model identity (never a credential),
  the awaiting/completion/blocked payloads and the honest diagnostics.

Neither record contains the model's hidden reasoning, and neither may be
written as a substitute for the live result: the trace is the audit copy,
assembled from what actually happened. Secret-looking keys are scrubbed
by the store on the way in (§87/§88); nothing here mints evidence.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from crea.core.models.research import ResearchStep, ToolResult
from crea.core.models.research_decision import (
    DecisionRecord,
    ScientificInterpretation,
)
from crea.core.provenance import utc_now_iso

#: Moves only when the stored shape changes meaning. A reader refuses a
#: version it does not speak rather than guessing (same rule as the schema).
RESEARCH_TRACE_SCHEMA_VERSION = "research-trace-v1"


class ResearchTurnTrace(BaseModel):
    """One turn of one session, as the audit trail keeps it."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = RESEARCH_TRACE_SCHEMA_VERSION
    study_id: str
    session_id: str
    #: Turn index within the run segment that produced it, starting at 1.
    turn: int = 0
    decision_id: str = ""
    #: Append order within (study, session). Assigned by storage, so a
    #: resumed session continues after its earlier turns rather than
    #: colliding with them.
    position: int = 0
    created_at: str = Field(default_factory=utc_now_iso)
    #: The gate's audit row, in full -- ruling, timing, tokens, decision.
    record: DecisionRecord
    #: The execution half, present only when this turn ran a tool. The
    #: arguments are what the model proposed (already policy-checked).
    step: ResearchStep | None = None
    step_id: str = ""
    tool_name: str = ""
    arguments: dict = Field(default_factory=dict)
    tool_result: ToolResult | None = None
    #: The M1.2 observation as JSON (the in-memory `raw` handle is excluded
    #: from `Observation` serialization and is not stored).
    observation: dict | None = None
    #: The model's scientific reading, stored beside -- never inside --
    #: the observation it reads.
    interpretation: ScientificInterpretation | None = None


class ResearchRunTrace(BaseModel):
    """One run segment's terminal summary, for reading it back later."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = RESEARCH_TRACE_SCHEMA_VERSION
    study_id: str
    session_id: str
    goal: str = ""
    #: "ai_research_intelligence" | "deterministic_fallback".
    mode: str = ""
    status: str = ""
    outcome: str = ""
    reason: str = ""
    started_at: str = ""
    ended_at: str = ""
    duration_ms: int = 0
    cancelled: bool = False
    #: Provider and model *names* only. A credential never reaches a row.
    model_provider: str = ""
    model_name: str = ""
    prompt_version: str = ""
    budget: dict = Field(default_factory=dict)
    decisions_used: int = 0
    tool_calls_used: int = 0
    model_calls_used: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    hypothesis_ids: list[str] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    awaiting: dict | None = None
    completion: dict | None = None
    blocked: dict | None = None
    #: How many turn rows this run wrote -- a reader can tell a complete
    #: trail from one whose process died between turns.
    record_count: int = 0
    updated_at: str = Field(default_factory=utc_now_iso)


__all__ = [
    "RESEARCH_TRACE_SCHEMA_VERSION",
    "ResearchRunTrace",
    "ResearchTurnTrace",
]

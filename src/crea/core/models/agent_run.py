"""Evidence-aware agent run (CREA Stage 6.4).

A bounded, read-only, tool-using run: a question goes in, the agent calls
deterministic tools, and it returns a **grounded** answer whose every
referenced id resolves against what the run actually gathered. Facts come from
tool results; the `summary` and the `rationale` are the model's words, and a
claim the data cannot support is marked HYPOTHESIS, never stated as fact
(§二十九/§三十一/§六十三).

Everything here is transient (§十): no agent session, run or message is stored.
No `confidence` / `success_probability` / `yield` / `best_route` field exists
(§三十四/§六十四).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.models.modification import ModelMetadata


class ToolPermission(str, Enum):
    """What a tool may do. Stage 6.4 ships READ_ONLY tools only (§八/§四十五)."""

    READ_ONLY = "read_only"
    WRITE = "write"


class AgentStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"        # tool data was not enough for a full answer
    LIMIT_REACHED = "limit_reached"
    FAILED = "failed"


class StopReason(str, Enum):
    FINAL_ANSWER = "final_answer"
    MAX_ITERATIONS = "max_iterations"
    MAX_TOOL_CALLS = "max_tool_calls"
    TIMEOUT = "timeout"
    REPEATED_TOOL_CALL = "repeated_tool_call"
    PROVIDER_ERROR = "provider_error"
    INVALID_ACTION = "invalid_action"


class ClaimType(str, Enum):
    FACT = "fact"                    # backed by a tool result
    INTERPRETATION = "interpretation"  # the model's reading of the facts
    HYPOTHESIS = "hypothesis"        # proposed, not established (§三十一)


class ToolCallRecord(BaseModel):
    """One tool invocation, kept as an auditable trace (§十六). No raw prompt."""

    tool_call_id: str
    tool_name: str
    arguments: dict = Field(default_factory=dict)
    status: str = ""
    output_summary: str = ""
    #: Ids the result carried, so the answer can be grounded against them.
    output_ids: list[str] = Field(default_factory=list)


class AgentClaim(BaseModel):
    """One statement in the answer, with its kind and its evidence (§三十三)."""

    text: str
    claim_type: ClaimType = ClaimType.FACT
    #: True for anything that is not established by a tool result.
    not_experimentally_verified: bool = False
    reaction_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    literature_ids: list[str] = Field(default_factory=list)
    route_ids: list[str] = Field(default_factory=list)
    molecule_ids: list[str] = Field(default_factory=list)
    rationale: str = ""
    grounded: bool = False


class AgentAnswer(BaseModel):
    summary: str = ""
    claims: list[AgentClaim] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    sources: list[dict] = Field(default_factory=list)


class EvidenceAgentResult(BaseModel):
    run_id: str
    question: str = ""
    status: AgentStatus = AgentStatus.FAILED
    stop_reason: StopReason = StopReason.INVALID_ACTION
    answer: AgentAnswer = Field(default_factory=AgentAnswer)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    iterations: int = 0
    #: Ids the run actually saw, so grounding is auditable after the fact.
    gathered: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    model_metadata: ModelMetadata = Field(default_factory=ModelMetadata)


class EvidenceAgentRequest(BaseModel):
    question: str = ""
    molecule_smiles: str = ""
    molecule_id: str = ""


__all__ = [
    "AgentAnswer",
    "AgentClaim",
    "AgentStatus",
    "ClaimType",
    "EvidenceAgentRequest",
    "EvidenceAgentResult",
    "StopReason",
    "ToolCallRecord",
    "ToolPermission",
]

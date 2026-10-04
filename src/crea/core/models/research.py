"""Research domain model: a task, the steps taken, and the report produced.

Everything an agent does leaves a record here, and the record is what makes a
report checkable afterwards. A claim that cannot be traced back through
`ResearchStep` -> `ToolResult` -> `Evidence` is a claim nobody should read.

Deliberately absent: a confidence score, a "thought" field, and any place a
model's narration could be mistaken for a tool result. The trace records what was
*run*, and the model is recorded separately as the thing that asked for it.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from crea.core.models.evidence import Claim, Evidence
from crea.core.models.knowledge import KnowledgeReport
from crea.core.models.reaction import ReactionRecord
from crea.core.models.reaction_linking import RouteEvidenceReport
from crea.core.models.reaction_knowledge import Reaction, RouteStepReactionLink
from crea.core.models.route import CandidateRoute
from crea.core.provenance import utc_now_iso


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"          # hard limit reached; report what exists
    FAILED = "failed"            # nothing usable came back


class ActionKind(str, Enum):
    TOOL_CALL = "tool_call"
    FINAL = "final"
    CLARIFY = "clarify"


class ToolStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    UNAVAILABLE = "unavailable"   # the tool exists but cannot run here
    REJECTED = "rejected"         # arguments failed validation; never executed


class ClaimSpec(BaseModel):
    """A claim the agent should assemble once its evidence exists.

    `from_tool` names a tool whose results supply the evidence ids. A scripted
    planner cannot know an id before the tool has run, and inventing one would
    be exactly the fabrication this project refuses. If the named tool produced
    nothing, the claim resolves to no evidence and is reported as insufficient.
    """

    text: str
    claim_type: str = "experimental_fact"
    from_tool: str = ""


class AgentAction(BaseModel):
    """What the model asked for. Never anything executable.

    There is no code field, and no path by which a model can supply Python.
    The action names a registered tool and a JSON object of arguments; the
    registry validates the arguments against that tool's schema before anything
    runs.
    """

    action: ActionKind = ActionKind.TOOL_CALL
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    claims: list[Claim] = Field(default_factory=list)
    claim_specs: list[ClaimSpec] = Field(default_factory=list)
    summary: str = ""
    question: str = ""


class ToolResult(BaseModel):
    """The only thing that may enter agent state as a fact.

    Produced by executing a registered tool. There is no path by which a model's
    own account of what a tool returned can be mistaken for one, because
    nothing writes a `ToolResult` except `ToolRegistry.execute`.

    CREA 4.0 Stage C additive fields: `diagnostics` (what was degraded,
    skipped or unavailable -- never silent) and `provenance` (which
    providers/backends actually produced the answer). Together with
    `status`/`data`/`error_type` this is the one ToolResult contract the
    Chemistry Tool Layer uses (§5 of Stage C).
    """

    tool: str
    status: ToolStatus = ToolStatus.OK
    data: Any = None
    error_type: str | None = None
    error: str | None = None
    duration_ms: int = 0
    step_id: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] | None = None


class AgentResult(BaseModel):
    """The ChemistryAgent's answer to one message (CREA 4.0 Stage E §4/§11).

    Reuses the ONE `ToolResult` contract for every call it made, and lifts
    what those tools produced into three aggregates -- evidence, provenance,
    diagnostics -- so the final answer is never a bare string with the tool
    results discarded. There is no second result vocabulary: `tool_calls`
    holds the same `ToolResult`s the registry returned, and the aggregates
    are lifts of what the tools *said*, never the model's account of them.

    `status` is the run's own outcome, not a confidence: `answered` reached
    a final answer, `limit_reached` hit the hard iteration cap, `model_failed`
    means the LLM produced no usable turn. An `answered` run whose tools were
    `unavailable` still says so in `diagnostics`.
    """

    answer: str = ""
    tool_calls: list[ToolResult] = Field(default_factory=list)
    #: Evidence the tools actually returned (ids or payloads), each tagged
    #: with the tool that produced it. Never a model's citation.
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    #: Per-call provenance, each tagged with its tool. Traceability, not a score.
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    iterations: int = 0
    #: "answered" | "limit_reached" | "model_failed".
    status: str = "answered"


class ResearchStep(BaseModel):
    step_id: str
    index: int
    at: str = Field(default_factory=utc_now_iso)
    action: ActionKind = ActionKind.TOOL_CALL
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    result_status: ToolStatus | None = None
    error: str | None = None
    llm_provider: str = ""
    llm_model: str = ""
    note: str = ""


class Finding(BaseModel):
    """One statement in the report, and what it rests on."""

    text: str
    kind: Literal["fact", "computed", "evidence_backed", "inference", "insufficient"]
    claim: Claim | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    note: str = ""


class SourceRef(BaseModel):
    """A cited work or file, as a reader would recognise it."""

    kind: Literal["paper", "document"]
    identifier: str
    label: str = ""
    url: str | None = None
    doi: str | None = None
    document_id: str | None = None


class ResearchReport(BaseModel):
    question: str
    interpretation: str = ""
    findings: list[Finding] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    #: Structured experimental conditions, when a procedure was read. Additive:
    #: everything the report already carried still carries.
    reaction_details: list[ReactionRecord] = Field(default_factory=list)
    #: Candidate synthesis routes, and what was actually found for each step.
    routes: list[CandidateRoute] = Field(default_factory=list)
    route_notes: list[str] = Field(default_factory=list)
    #: The reaction-level evidence chain per route: which rung each step
    #: reached and on what evidence. Kept beside `routes` rather than folded
    #: into them, because `CandidateRoute.steps[i].status` and
    #: `evidence_level` answer different questions and merging them for
    #: display is how a candidate gets described as verified.
    route_evidence: list["RouteEvidenceReport"] = Field(default_factory=list)
    #: The knowledge-layer reactions this run established (Phase 3.10), and
    #: the measured links between route steps and those reactions. Additive:
    #: every field above still carries what it carried.
    reactions: list["Reaction"] = Field(default_factory=list)
    reaction_links: list["RouteStepReactionLink"] = Field(default_factory=list)
    #: What the knowledge base knew and what this run changed (Phase 3.12,
    #: §100). `None` when no knowledge base took part -- the report is then
    #: exactly the pre-3.12 rendering.
    knowledge: "KnowledgeReport | None" = None
    sources: list[SourceRef] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)
    trace_summary: str = ""
    generated_at: str = Field(default_factory=utc_now_iso)
    status: TaskStatus = TaskStatus.PENDING

    def unsupported_findings(self) -> list[Finding]:
        """Findings that assert something no Evidence supports."""
        return [f for f in self.findings
                if f.kind in ("fact", "evidence_backed") and not f.evidence_refs]


class ResearchTask(BaseModel):
    task_id: str
    question: str
    status: TaskStatus = TaskStatus.PENDING
    created_at: str = Field(default_factory=utc_now_iso)
    finished_at: str | None = None
    steps: list[ResearchStep] = Field(default_factory=list)
    report: ResearchReport | None = None
    errors: list[str] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)

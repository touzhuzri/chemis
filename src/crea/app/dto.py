"""Application DTOs: the shapes the workbench speaks.

Phase 4 (§3/§60). Request and response records for the application use
cases. Everything here is transport-neutral: the HTTP layer serializes
these, the UI reads them, and neither touches a domain model directly
nor a database at all.

Principles kept from Phase 3: ids over embedded objects where a relation
exists (§60: "尽量引用 ID"), no confidence numbers anywhere, statuses
stay the domain's words, and search results carry their match kind
(`EXACT` / `SCREENING`) -- a similarity hit is never rendered as a
confirmed match (§99).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    """A structured error. Never an RDKit/SQLite/LLM exception (§56)."""

    code: str
    message: str
    detail: str = ""


class StartResearchRequest(BaseModel):
    """One research question, one structure, optionally documents."""

    question: str = Field(min_length=1, max_length=2000)
    #: SMILES | INCHI | NAME. Absent means no structure input -- the run
    #: reports "Identity unresolved" rather than guessing (§56).
    structure_kind: Literal["SMILES", "INCHI", "NAME"] | None = None
    structure_value: str = ""
    document_ids: list[str] = Field(default_factory=list)


class JobView(BaseModel):
    """Research job status. The unit of asynchronous work (§10)."""

    job_id: str
    status: str  # queued | running | completed | partial | cancelled | interrupted
    question: str = ""
    phase: str = ""
    session_id: str = ""
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    cancel_requested: bool = False
    error: str = ""
    structure_kind: str | None = None
    structure_value: str = ""
    document_ids: list[str] = Field(default_factory=list)


class FacetView(BaseModel):
    facet: str
    state: str  # known | partially_known | unknown
    detail: str = ""
    object_ids: list[str] = Field(default_factory=list)


class GapView(BaseModel):
    facet: str
    reason: str
    suggested_action: str = ""


class ConflictObservationView(BaseModel):
    value: float | str | None = None
    raw_value: str = ""
    unit: str = ""
    raw_unit: str = ""
    evidence_id: str = ""
    paper_id: str = ""
    doi: str = ""
    reaction_id: str = ""
    record_id: str = ""


class ConflictView(BaseModel):
    field: str
    kind: str = "conflict"
    note: str = ""
    observations: list[ConflictObservationView] = Field(default_factory=list)


class KnowledgeStateView(BaseModel):
    """Existing knowledge, gaps and conflicts for one target (§20/§21)."""

    entity_id: str | None = None
    facets: list[FacetView] = Field(default_factory=list)
    gaps: list[GapView] = Field(default_factory=list)
    conflicts: list[ConflictView] = Field(default_factory=list)
    note: str = ("既有知识来自已记录来源（含 provenance/retrieved_at），"
                 "不代表当前最新")


class EntityView(BaseModel):
    entity_id: str
    identity_key: str = ""
    identity_basis: str = ""
    canonical_smiles: str = ""
    inchi: str = ""
    inchikey: str = ""
    molecular_formula: str | None = None
    molecular_weight: float | None = None
    names: list[str] = Field(default_factory=list)
    external_identifiers: list[dict] = Field(default_factory=list)
    provenance: dict | None = None
    created_at: str = ""


class MentionView(BaseModel):
    mention_id: str
    raw_text: str
    source: str = ""
    source_id: str = ""
    resolution_status: str = ""
    entity_id: str | None = None
    context_before: str = ""
    context_after: str = ""


class PaperView(BaseModel):
    paper_id: str
    doi: str | None = None
    title: str | None = None
    journal: str | None = None
    year: int | None = None
    url: str | None = None


class EvidenceView(BaseModel):
    """Read-only by design (§93): there is no update endpoint."""

    evidence_id: str
    evidence_type: str
    strength: str
    source: str
    source_id: str
    retrieved_at: str = ""
    raw_quote: str
    paper_id: str | None = None
    doi: str | None = None
    locator: dict | None = None
    provenance: dict | None = None


class ReactionComponentView(BaseModel):
    role: str
    name: str = ""
    entity_id: str | None = None
    identity_key: str = ""
    canonical_smiles: str = ""
    mention_id: str | None = None


class ReactionView(BaseModel):
    """Read-only experimental knowledge (§94)."""

    reaction_id: str
    reaction_key: str = ""
    status: str = ""
    record_id: str = ""
    paper_id: str = ""
    doi: str = ""
    source_id: str = ""
    document_id: str = ""
    components: list[ReactionComponentView] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    unresolved_evidence_refs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RecordView(BaseModel):
    """The experimental basis of a reaction, as written (§26-§27)."""

    record_id: str
    reaction_id: str = ""
    source: str = ""
    document_id: str | None = None
    paper_id: str | None = None
    reaction_description: str = ""
    conditions: dict = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    extractor: str = ""


class RouteStepView(BaseModel):
    step_id: str
    step_index: int = 0
    status: str = ""
    support_level: str = ""
    reactant_smiles: list[str] = Field(default_factory=list)
    product_smiles: list[str] = Field(default_factory=list)
    linked_reactions: list[dict] = Field(default_factory=list)


class RouteView(BaseModel):
    route_id: str
    target_smiles: str = ""
    score: float | None = None
    score_source: str = ""
    source: str = ""
    steps: list[RouteStepView] = Field(default_factory=list)


class SearchResultView(BaseModel):
    kind: str
    id: str
    label: str = ""
    #: EXACT | SCREENING -- a screen is never shown as a confirmed match
    #: (§99/§100).
    match: str = "EXACT"
    object: dict = Field(default_factory=dict)


class SessionView(BaseModel):
    """One research run, by reference (§60)."""

    session_id: str
    question: str = ""
    status: str = ""
    created_at: str = ""
    phases: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    mention_ids: list[str] = Field(default_factory=list)
    reaction_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    route_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class SessionDetail(SessionView):
    """The workspace's full picture of one run (§16/§60)."""

    #: The identity card (§17): what the identity layer actually
    #: established -- never inferred from anything else.
    identity: dict | None = None
    #: The computed molecular analysis (§8/§4.B), as the report states it.
    analysis: list[str] = Field(default_factory=list)
    knowledge_before: KnowledgeStateView | None = None
    knowledge_gaps: list[GapView] = Field(default_factory=list)
    knowledge_reused: dict[str, list[str]] = Field(default_factory=dict)
    knowledge_added: dict[str, list[str]] = Field(default_factory=dict)
    conflicts: list[ConflictView] = Field(default_factory=list)
    report_markdown: str = ""
    job_id: str = ""


class ProvenanceChain(BaseModel):
    """One walk down the provenance chain (§97): ids and their links."""

    kind: str
    id: str
    label: str = ""
    links: dict[str, list[str]] = Field(default_factory=dict)


__all__ = [
    "ConflictObservationView",
    "ConflictView",
    "EntityView",
    "ErrorBody",
    "EvidenceView",
    "FacetView",
    "GapView",
    "JobView",
    "KnowledgeStateView",
    "MentionView",
    "PaperView",
    "ProvenanceChain",
    "ReactionComponentView",
    "ReactionView",
    "RecordView",
    "RouteStepView",
    "RouteView",
    "SearchResultView",
    "SessionDetail",
    "SessionView",
    "StartResearchRequest",
]

"""AI Context (CREA Stage 6.1).

A structured, versioned assembly of **already-known** facts, shaped for a
future AI layer to consume. It is fact assembly, not reasoning (§1): there is
deliberately no `ai_answer` / `recommendation` / `reasoning` / `confidence` /
`prediction` field -- this is input, never output.

Every sub-record reuses the existing vocabulary (ChemicalEntity identity,
ReactionTemplate, ReactionEvidence, Paper) and keeps the relations between
them: an evidence record names the literature it came from, so the AI can tell
*why* a paper is present.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from crea.core.provenance import utc_now_iso

#: The explicit, stable schema version (§5). Future schema changes bump it.
CONTEXT_VERSION = "crea-ai-context-v1"


class ContextWarning(BaseModel):
    """Something the context is missing, stated rather than hidden (§36)."""

    code: str
    message: str
    ref: str = ""


class ContextMolecule(BaseModel):
    entity_id: str
    identity_key: str
    standard_inchikey: str
    smiles: str
    molecular_formula: str = ""
    molecular_weight: float | None = None
    formal_charge: int = 0
    atom_count: int = 0
    heavy_atom_count: int = 0
    ring_count: int = 0
    names: list[str] = Field(default_factory=list)
    #: "available" only when the entity carries a real name; never invented (§8).
    name_status: str = "unavailable"
    functional_groups: list[str] = Field(default_factory=list)
    scaffold: str = ""


class ContextReaction(BaseModel):
    template_id: str
    name: str
    class_family: str
    reaction_class: str = ""
    transformation: dict = Field(default_factory=dict)
    mechanism_class: str = ""
    reagents: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    selectivity: dict = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    substrate_patterns: list[str] = Field(default_factory=list)
    product_patterns: list[str] = Field(default_factory=list)
    reaction_smarts: str = ""
    evidence_count: int = 0


class ContextReactionMatch(BaseModel):
    template_id: str
    match_level: str
    matched_substrate_atoms: int = 0


class ContextEvidence(BaseModel):
    evidence_id: str
    reaction_template_id: str
    evidence_type: str
    source_type: str
    support_scope: str = ""
    status: str = ""
    #: The local literature id this evidence rests on (empty for curated); the
    #: relation is kept, not flattened (§13).
    literature_id: str = ""
    locator: dict = Field(default_factory=dict)


class ContextLiterature(BaseModel):
    paper_id: str
    title: str = ""
    year: int | None = None
    journal: str = ""
    doi: str = ""
    oa_status: str = "unknown"
    #: Kept separate from evidence (§14): UNKNOWN permission says nothing about
    #: whether the evidence holds.
    fulltext_permission: str = "unknown"
    #: Present only when the Paper carries one; never a generated summary (§12).
    abstract: str = ""


class ContextGraph(BaseModel):
    root: str
    nodes: list[dict] = Field(default_factory=list)
    edges: list[dict] = Field(default_factory=list)


class ContextRouteStep(BaseModel):
    step_index: int
    reaction_template_id: str
    input_smiles: str
    output_smiles: str


class ContextRoute(BaseModel):
    route_id: str
    status: str
    steps: list[ContextRouteStep] = Field(default_factory=list)
    blocked_reason: str = ""


class AIContext(BaseModel):
    context_version: str = CONTEXT_VERSION
    created_at: str = Field(default_factory=utc_now_iso)
    query: str = ""
    #: What was asked for -- the scope this context covers (§24/§26).
    source_scope: dict = Field(default_factory=dict)
    molecules: list[ContextMolecule] = Field(default_factory=list)
    reactions: list[ContextReaction] = Field(default_factory=list)
    matches: list[ContextReactionMatch] = Field(default_factory=list)
    evidence: list[ContextEvidence] = Field(default_factory=list)
    literature: list[ContextLiterature] = Field(default_factory=list)
    graphs: list[ContextGraph] = Field(default_factory=list)
    routes: list[ContextRoute] = Field(default_factory=list)
    warnings: list[ContextWarning] = Field(default_factory=list)
    #: Honest statements about what this context does not contain.
    limitations: list[str] = Field(default_factory=list)


class AIContextRequest(BaseModel):
    molecule_ids: list[str] = Field(default_factory=list)
    molecule_smiles: list[str] = Field(default_factory=list)
    reaction_template_ids: list[str] = Field(default_factory=list)
    include_graph: bool = False
    include_routes: bool = False
    route_target_smiles: str = ""
    query: str = ""
    max_molecules: int = 10
    max_reactions: int = 20


__all__ = [
    "AIContext",
    "AIContextRequest",
    "CONTEXT_VERSION",
    "ContextEvidence",
    "ContextGraph",
    "ContextLiterature",
    "ContextMolecule",
    "ContextReaction",
    "ContextReactionMatch",
    "ContextRoute",
    "ContextRouteStep",
    "ContextWarning",
]

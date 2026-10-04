"""Reaction comparison (CREA Stage 5.2).

A transient response DTO: two or more `ReactionTemplate`s side by side on the
facts they already carry -- transformation, reagents, conditions, mechanism,
selectivity, limitations, and evidence. It is **not** persisted and it makes
no judgement.

There is deliberately no `best_reaction` / `recommended_reaction` / `score` /
`confidence` / `success_probability` / `predicted_yield` field (§20): the tool
helps a chemist compare known reaction facts, and never predicts experimental
success. `yield_available` is False because no yield model exists yet (§14).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from crea.core.models.reaction_template import Transformation


class ReactionEvidenceItem(BaseModel):
    """One linked evidence record, compact, for the comparison view (§11-§13).

    Literature-linked evidence carries the paper's own metadata (read from the
    `Paper`, never copied into the evidence model) so the view can link
    through to Literature Detail. The wording is "literature evidence", never
    "successful examples".
    """

    evidence_type: str
    source_type: str
    support_scope: str = ""
    status: str = ""
    #: Present only for literature-linked evidence.
    paper_id: str = ""
    title: str = ""
    year: int | None = None
    journal: str = ""
    doi: str = ""


class ReactionComparisonEntry(BaseModel):
    """One reaction, compared on facts alone (§5-§9)."""

    template_id: str
    name: str
    class_family: str
    reaction_class: str = ""
    transformation: Transformation
    #: "" when not evaluated; else transformation_match / substrate_match /
    #: no_match -- reused verbatim from Transformation Search (5.1).
    substrate_compatibility: str = ""
    matched_substrate_atoms: int = 0
    reagents: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    mechanism_class: str = ""
    selectivity: dict[str, str] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    evidence_count: int = 0
    evidence_summary: dict[str, int] = Field(default_factory=dict)
    literature: list[ReactionEvidenceItem] = Field(default_factory=list)
    #: Always False for now: no structured experimental-yield model exists.
    yield_available: bool = False


class ReactionComparisonResult(BaseModel):
    substrate: str = ""
    target: str = ""
    #: In input order -- deterministic and explicit, never dict/database order.
    entries: list[ReactionComparisonEntry] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


__all__ = [
    "ReactionComparisonEntry",
    "ReactionComparisonResult",
    "ReactionEvidenceItem",
]

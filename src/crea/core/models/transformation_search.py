"""Transformation search results (CREA Stage 5.1).

Mapping a substrate (and optionally a target) onto the existing
`ReactionTemplate`s. The result says how far a template matched, and never
more than that:

* `SUBSTRATE_MATCH` -- the substrate SMARTS matched. The molecule fits this
  template's input pattern, nothing more (and a template with no product
  pattern can only ever reach this level).
* `TRANSFORMATION_MATCH` -- the substrate *and* the product SMARTS matched.
  The template's structural pattern is consistent with the given
  substrate -> target change. Still **not** experimental success.

There is no success probability, no yield, no "best reaction" (§7/§9).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.models.reaction_template import ReactionTemplate, Transformation


class TransformationMatchLevel(str, Enum):
    """How far a template matched (§6)."""

    TRANSFORMATION_MATCH = "transformation_match"
    SUBSTRATE_MATCH = "substrate_match"


class TransformationCandidate(BaseModel):
    """One candidate template, and how far it matched (§7)."""

    reaction_template: ReactionTemplate
    match_level: TransformationMatchLevel
    transformation: Transformation
    matched_substrate_atoms: list[list[int]] = Field(default_factory=list)
    matched_product_atoms: list[list[int]] = Field(default_factory=list)
    evidence_count: int = 0
    #: A breakdown by source kind, e.g. {"curated": 2, "literature": 1}.
    evidence_summary: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class TransformationSearchResult(BaseModel):
    """The whole answer to a transformation search."""

    substrate: str = ""
    target: str = ""
    candidates: list[TransformationCandidate] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.candidates)


__all__ = [
    "TransformationCandidate",
    "TransformationMatchLevel",
    "TransformationSearchResult",
]

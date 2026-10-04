"""Chemical exploration results (CREA 4.0 Stage G).

Stage G explores the structure space *around* a target -- not a synthesis
route. The result vocabulary therefore keeps three things distinct that a
retrosynthesis system would fuse (§1):

* a **relationship** (similar / same scaffold / fragment-related) is a
  structural fact from the chemistry layer, never synthetic feasibility;
* a **candidate** is a resolved `ChemicalEntity` plus its relationship,
  never a "validated precursor";
* **reaction evidence** says what the reaction knowledge actually contains
  -- DIRECT (a stored reaction connects candidate to target), RELATED (the
  candidate has reaction history but no direct link), NO (searched, found
  nothing) or UNAVAILABLE (the source could not be queried). These four are
  never conflated (§14).

One result vocabulary: `ExplorationResult` reuses `ChemicalEntity` for its
target and candidates; there is no `ExplorationEntity2` and no second
relationship/reaction-evidence naming system.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.models.entity import ChemicalEntity


class RelationshipKind(str, Enum):
    """What a candidate IS to the target, structurally (§11)."""

    SIMILAR = "similar"
    SAME_SCAFFOLD = "same_scaffold"
    FRAGMENT_RELATED = "fragment_related"


class ReactionEvidenceStatus(str, Enum):
    """How far the reaction knowledge supports candidate -> target (§14)."""

    DIRECT_REACTION_EVIDENCE = "direct_reaction_evidence"
    RELATED_REACTION_EVIDENCE = "related_reaction_evidence"
    NO_REACTION_EVIDENCE = "no_reaction_evidence"
    REACTION_SEARCH_UNAVAILABLE = "reaction_search_unavailable"


class Candidate(BaseModel):
    """One explored structure, as an entity plus how it relates to the target.

    `similarity` is the screening Tanimoto (a number, not a verdict, §12);
    `scaffold_relation` is the descriptive scaffold answer ("same" /
    "related" / "different"); `relationship` is the primary structural
    label. `reaction_evidence` is the four-level status above, and `reactions`
    carries the stored reactions (with their evidence/provenance) when any
    were found -- never a ranking and never a route.
    """

    entity: ChemicalEntity
    relationship: RelationshipKind = RelationshipKind.SIMILAR
    similarity: float | None = None
    scaffold_relation: str = "different"
    fragment_related: bool = False
    identity_status: str = "resolved"
    reaction_evidence: ReactionEvidenceStatus = (
        ReactionEvidenceStatus.NO_REACTION_EVIDENCE)
    reactions: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    provenance: dict | None = None
    diagnostics: list[str] = Field(default_factory=list)


class ExplorationResult(BaseModel):
    """What exploring the space around a target found (§5).

    `source` says where candidates came from ("local" knowledge base, a
    provider, a fixture) -- never a provider name faked over local data
    (§9). `returned_candidates` is what came back under `max_candidates`,
    `total_candidates` is how many distinct candidates the space held before
    the cap (§23) -- a partial view is stated, not hidden.
    """

    target: ChemicalEntity
    candidates: list[Candidate] = Field(default_factory=list)
    reaction_evidence: dict = Field(default_factory=dict)
    evidence: list[dict] = Field(default_factory=list)
    provenance: list[dict] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    source: str = "local"
    returned_candidates: int = 0
    total_candidates: int = 0


__all__ = [
    "Candidate",
    "ExplorationResult",
    "ReactionEvidenceStatus",
    "RelationshipKind",
]

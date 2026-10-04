"""Evidence-backed transformations (CREA 4.0 Stage H).

A `Transformation` is the capability-layer answer to "what one-hop
transformation connects these two entities?" It *references* an existing
`Reaction` (Stage D) -- it is not a second reaction model (§4). The Reaction
stays the chemical fact; the Transformation adds the query-facing facts: the
direction of the query relative to the recorded reaction, and the evidence
level of the claim (documented / derived / inferred).

The line this module exists to hold (§10/§24): a stored `Reaction` that
directly records `candidate -> target` is `DOCUMENTED`; anything inferred
from structure or similarity is `INFERRED` and must never be dressed as
documented. Similarity, scaffold and fragment relations are NOT
transformations, and no stored reaction is "this transformation does not
exist" -- it is "no evidence in the queried source" (§6.3).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.models.reaction_knowledge import Reaction


class TransformationStatus(str, Enum):
    """The outcome of a transformation query (§6/§8).

    The first two are findings; the last three are honest non-findings and
    must never be conflated: `NO_TRANSFORMATION_EVIDENCE` is "searched, found
    nothing" (not "does not exist"), and `TRANSFORMATION_SEARCH_UNAVAILABLE`
    is "could not search" (not "found nothing").
    """

    DIRECT_TRANSFORMATION = "direct_transformation"
    RELATED_TRANSFORMATION = "related_transformation"
    NO_TRANSFORMATION_EVIDENCE = "no_transformation_evidence"
    TRANSFORMATION_SEARCH_UNAVAILABLE = "transformation_search_unavailable"
    INVALID = "invalid"


class TransformationDirection(str, Enum):
    """Which way the query runs relative to the recorded reaction (§9).

    `FORWARD` means the query's reactant side matches the reaction's
    reactants and the product side its products. `REVERSE` means the query
    asks the recorded reaction backwards -- a real reaction, reported under
    its own name, never presented as the forward record.
    """

    FORWARD = "forward"
    REVERSE = "reverse"


class TransformationEvidenceLevel(str, Enum):
    """What the transformation claim rests on (§11)."""

    DOCUMENTED = "documented"   # a stored Reaction / trusted source records it
    DERIVED = "derived"         # derived deterministically from reaction data
    INFERRED = "inferred"       # inferred from structure or a model


class Transformation(BaseModel):
    """One-hop transformation between entities, over an existing Reaction.

    `reaction` is the Stage D `Reaction`, reused unchanged; `direction` says
    how the query relates to it; `evidence_level` says what the claim rests
    on. `evidence` is the reaction's own citations (ids travel, resolved by
    a caller with a knowledge base), `provenance` the reaction's own trace.
    """

    transformation_id: str
    reaction: Reaction
    direction: TransformationDirection = TransformationDirection.FORWARD
    evidence_level: TransformationEvidenceLevel = (
        TransformationEvidenceLevel.DOCUMENTED)
    evidence: list[dict] = Field(default_factory=list)
    provenance: dict | None = None
    diagnostics: list[str] = Field(default_factory=list)


class TransformationSearchResult(BaseModel):
    """The whole answer to a transformation query (§8)."""

    status: TransformationStatus = TransformationStatus.NO_TRANSFORMATION_EVIDENCE
    transformations: list[Transformation] = Field(default_factory=list)
    matched_reactions: list[str] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    provenance: list[dict] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


__all__ = [
    "Transformation",
    "TransformationDirection",
    "TransformationEvidenceLevel",
    "TransformationSearchResult",
    "TransformationStatus",
]

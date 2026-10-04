"""Modification analysis (CREA Stage 6.2).

A user asks how to change a molecule; the AI proposes candidate modifications
that are **grounded in the AI Context** -- every candidate must reference a
reaction template (and optionally evidence / literature) that actually exists
in the context. A candidate the context cannot support is moved to `warnings`,
never shown as chemistry (§13/§28).

The result is structured, not prose (§十), and it keeps CREA facts separate
from AI interpretation (§51): the fact fields come from the context, the
`rationale` is the model's own words. It is transient -- nothing is persisted
(§22). There is no yield, no new reaction template, no invented citation.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class GroundingStatus(str, Enum):
    """How well a candidate's references resolve against the AI Context."""

    GROUNDED = "grounded"                 # every reference resolves
    PARTIALLY_GROUNDED = "partially_grounded"  # some resolve, some do not
    UNSUPPORTED = "unsupported"           # nothing resolves -> not shown


class StructuralChange(BaseModel):
    """A feature-to-feature change, as far as it can be structured (§11).

    Free enough for a natural-language request ("alcohol -> alkyl_halide"),
    never a forced atom map.
    """

    from_feature: str = ""
    to_feature: str = ""
    scope: str = ""


class ModificationCandidate(BaseModel):
    """One grounded candidate modification (§11).

    `reaction_template_ids` / `evidence_ids` / `literature_ids` are CREA facts
    (verified against the context); `rationale` is AI interpretation.
    """

    candidate_id: str
    description: str = ""
    structural_change: StructuralChange = Field(default_factory=StructuralChange)
    reaction_template_ids: list[str] = Field(default_factory=list)
    match_status: dict[str, str] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    literature_ids: list[str] = Field(default_factory=list)
    evidence_summary: dict[str, int] = Field(default_factory=dict)
    rationale: str = ""
    grounding_status: GroundingStatus = GroundingStatus.UNSUPPORTED


class ModelMetadata(BaseModel):
    """AI execution metadata -- never a chemical fact (§34)."""

    provider: str = ""
    model: str = ""
    temperature: float | None = None
    request_id: str = ""


class ModificationAnalysisRequest(BaseModel):
    molecule_smiles: str = ""
    molecule_id: str = ""
    target_smiles: str = ""
    query: str = ""
    max_candidates: int = 5


class ModificationAnalysis(BaseModel):
    analysis_id: str
    context_version: str = ""
    input_molecule: str = ""
    input_molecule_id: str = ""
    user_query: str = ""
    candidates: list[ModificationCandidate] = Field(default_factory=list)
    #: Candidates the context could not support, kept as warnings not facts.
    unsupported: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    model_metadata: ModelMetadata = Field(default_factory=ModelMetadata)


__all__ = [
    "GroundingStatus",
    "ModelMetadata",
    "ModificationAnalysis",
    "ModificationAnalysisRequest",
    "ModificationCandidate",
    "StructuralChange",
]

"""The explainable matching report -- the actual product of Scenario A.

Design rule enforced throughout: every statement in a report must point at the
artifact that justifies it. `Screen` carries an explicit caveat; `CoreMatch`
carries atom indices; `Fragment`/`AttachmentMatch` carry the connection point.
There is no free-text field anywhere in this model.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from crea.core.models.fragments import AttachmentMatch, Fragment, FragmentMatch
from crea.core.models.matching import CoreMatch, ScaffoldMatch, SimilarityScreen
from crea.core.models.molecule import Molecule
from crea.core.provenance import Provenance

Verdict = Literal[
    "IDENTICAL",
    "REGIO_ISOMER",
    "STEREOISOMER",
    "SCAFFOLD_SHARED",
    "PARTIAL_OVERLAP",
    "DISSIMILAR",
]


class MatchInput(BaseModel):
    label: str
    molecule: Molecule


class StructureMatchReport(BaseModel):
    """Input -> Processing -> Output, with the processing made inspectable."""

    a: MatchInput
    b: MatchInput

    # Layer 1
    screen: SimilarityScreen

    # Layer 3a
    scaffold: ScaffoldMatch

    # Layer 2
    core: CoreMatch

    # Layer 3b / 4
    fragments_a: list[Fragment] = Field(default_factory=list)
    fragments_b: list[Fragment] = Field(default_factory=list)
    fragment_matches: list[FragmentMatch] = Field(default_factory=list)
    attachment_matches: list[AttachmentMatch] = Field(default_factory=list)

    # Conclusion
    verdict: Verdict = "DISSIMILAR"
    verdict_reasons: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    generated_at: str
    provenance: Provenance | None = None

    def similarity_value(self) -> float:
        """Best available scalar. Screening number, never a conclusion."""
        return self.screen.tanimoto_ecfp4

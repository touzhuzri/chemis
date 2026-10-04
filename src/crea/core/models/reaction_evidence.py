"""Reaction evidence: what supports a ReactionTemplate, and how (Stage 3.4).

This is the *knowledge-evidence* layer, distinct from two neighbours:

* **Provenance** answers "where did this object/field come from?".
* **ReactionEvidence** answers "which part of the ReactionTemplate does this
  source support?" (§4). A source supports the *transformation*, or the
  *substrate scope*, or the *selectivity* -- and says so in `support_scope`.

It is deliberately more abstract than `core.models.evidence.Evidence` (the
literature citation model, which requires a quote and a real source record).
A `ReactionEvidence` may legitimately have `source_type = CURATED` and no DOI
and no paper (§7/§15). Literature fields (`AbstractSource`, `FulltextPermission`,
`OAStatus`) belong to the literature layer and are not copied here (§23).

`evidence_id` is a CREA-internal record id, never a DOI or a standard
identifier (§6); external source identity travels in the source reference.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.models.reaction_template import EvidenceStatus
from crea.core.provenance import Provenance, utc_now_iso


class ReactionEvidenceType(str, Enum):
    """What part of the template this evidence supports (§7).

    A source is not "literature" or "not" -- that is `EvidenceSourceType`.
    This enum says *what the source backs up*: the transformation, the
    substrate scope, the conditions, and so on.
    """

    CURATED_KNOWLEDGE = "curated_knowledge"
    TRANSFORMATION_SUPPORT = "transformation_support"
    SUBSTRATE_SCOPE_SUPPORT = "substrate_scope_support"
    PRODUCT_SUPPORT = "product_support"
    CONDITION_SUPPORT = "condition_support"
    SELECTIVITY_SUPPORT = "selectivity_support"
    MECHANISM_SUPPORT = "mechanism_support"
    LIMITATION_SUPPORT = "limitation_support"
    CHARACTERIZATION_SUPPORT = "characterization_support"


class EvidenceSourceType(str, Enum):
    """What kind of source this is (§8/§15)."""

    CURATED = "curated"
    LITERATURE = "literature"
    PROVIDER = "provider"
    USER_PROVIDED = "user_provided"
    EXPERIMENTAL = "experimental"
    UNKNOWN = "unknown"


class EvidenceLocator(BaseModel):
    """Where in a source the evidence sits (Stage 4.4 §4).

    A *position*, never the evidence body: no raw quote, no extracted text, no
    PDF/OCR. Every field is optional; a literature evidence record may carry no
    locator at all.
    """

    page: int | None = None
    section: str | None = None
    figure: str | None = None
    table: str | None = None
    scheme: str | None = None
    supporting_information: str | None = None


class EvidenceSourceReference(BaseModel):
    """A minimal reference to the source, generic across kinds (§8/§16).

    No Crossref/OpenAlex/Unpaywall-specific fields (§22): a literature source
    is `source_type=LITERATURE` with the local `Paper`'s id in `source_id`, and
    `locator` positions the evidence within it. Bibliographic metadata (title,
    DOI, journal, year) is never copied here -- it is read from the `Paper`.
    """

    source_type: EvidenceSourceType = EvidenceSourceType.UNKNOWN
    source_id: str = ""
    title: str = ""
    url: str = ""
    locator: EvidenceLocator = Field(default_factory=EvidenceLocator)
    retrieved_at: str = Field(default_factory=utc_now_iso)


class ReactionEvidence(BaseModel):
    """One source's support for one ReactionTemplate (§5).

    `support_scope` says what exactly is supported -- free text for now
    ("primary alcohol → aldehyde"), deliberately not a complex ontology (§11).
    `status` reuses the existing `EvidenceStatus` (§10); `evidence_type` says
    what is supported. No confidence score, no ranking (§17).
    """

    evidence_id: str
    reaction_template_id: str
    evidence_type: ReactionEvidenceType
    source: EvidenceSourceReference = Field(default_factory=EvidenceSourceReference)
    support_scope: str = ""
    status: EvidenceStatus = EvidenceStatus.UNKNOWN
    note: str = ""
    provenance: Provenance | None = None


__all__ = [
    "EvidenceLocator",
    "EvidenceSourceReference",
    "EvidenceSourceType",
    "ReactionEvidence",
    "ReactionEvidenceType",
]

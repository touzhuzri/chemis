"""Literature data model. Pure data, no I/O, no policy.

Three distinctions in here are load-bearing and easy to lose later:

1. An abstract is typed by *where it came from*, because a text reassembled
   from OpenAlex's inverted index is a reconstruction and not a publisher
   original. `AbstractRecord.is_reconstructed` carries that, and downstream
   policy reads it.
2. OA status, access status and full-text permission are three separate
   questions with three separate enumerations. Merging them is how a system
   ends up treating "free to read" as "we may download".
3. A licence is a licence *of a particular thing*. A Crossref licence and an
   Unpaywall licence describe different artefacts and are both retained,
   distinguished by `LicenseInfo.applies_to`.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# abstracts
# ---------------------------------------------------------------------------


class AbstractSource(str, Enum):
    CROSSREF = "crossref"
    OPENALEX_RECONSTRUCTED = "openalex_reconstructed"
    PUBLISHER = "publisher"
    USER_PROVIDED = "user_provided"
    UNKNOWN = "unknown"


#: preference order when a Paper carries more than one abstract. Non-reconstructed
#: sources win: a reassembled abstract is a good screening artefact and a poor
#: substitute for what the publisher said.
ABSTRACT_PREFERENCE: tuple[AbstractSource, ...] = (
    AbstractSource.PUBLISHER,
    AbstractSource.CROSSREF,
    AbstractSource.OPENALEX_RECONSTRUCTED,
    AbstractSource.USER_PROVIDED,
    AbstractSource.UNKNOWN,
)


class EvidenceStrength(str, Enum):
    """How much a record may be leaned on.

    Phase 2 creates no Evidence. This enum exists so that the notion is
    correctly named now and is not invented later at whatever shape happens to
    be convenient.
    """

    PRIMARY_DOCUMENT_EVIDENCE = "primary_document_evidence"
    SECONDARY_METADATA_EVIDENCE = "secondary_metadata_evidence"
    DISCOVERY_EVIDENCE = "discovery_evidence"
    USER_PROVIDED_EVIDENCE = "user_provided_evidence"


class AbstractRecord(BaseModel):
    text: str
    source: AbstractSource
    retrieved_at: str
    raw_excerpt: str | None = None

    @property
    def is_reconstructed(self) -> bool:
        return self.source is AbstractSource.OPENALEX_RECONSTRUCTED

    @property
    def evidence_strength(self) -> EvidenceStrength:
        if self.source is AbstractSource.USER_PROVIDED:
            return EvidenceStrength.USER_PROVIDED_EVIDENCE
        if self.is_reconstructed:
            return EvidenceStrength.DISCOVERY_EVIDENCE
        return EvidenceStrength.SECONDARY_METADATA_EVIDENCE


# ---------------------------------------------------------------------------
# licences
# ---------------------------------------------------------------------------


class LicenseInfo(BaseModel):
    """UNKNOWN is the default and must stay reachable.

    An absent licence is a fact about our knowledge, not a fact about the work.
    Collapsing the two is how a system comes to treat a paywalled article as
    freely reusable.
    """

    spdx_id: str | None = None
    name: str = "UNKNOWN"
    url: str | None = None
    content_version: str = "UNKNOWN"
    applies_to: Literal["published_article", "oa_copy", "unknown"] = "unknown"
    is_known: bool = False
    stated_by: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# access
# ---------------------------------------------------------------------------


class OAStatus(str, Enum):
    """What the open-access ecosystem says about the work. A property of the work."""

    UNKNOWN = "unknown"
    GOLD = "gold"
    GREEN = "green"
    HYBRID = "hybrid"
    BRONZE = "bronze"
    # Verified present in live OpenAlex responses on 2026-09-30. A diamond
    # journal charges neither authors nor readers. That is a description of the
    # business model, not a licence, and it grants nothing on its own.
    DIAMOND = "diamond"
    CLOSED = "closed"


class AccessStatus(str, Enum):
    """What is reachable from here, now, by us. A property of the situation."""

    NOT_CHECKED = "not_checked"
    AVAILABLE = "available"
    SUBSCRIPTION = "subscription"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class FulltextPermission(str, Enum):
    """Whether we may retrieve, keep or redistribute. Never inferred from a URL."""

    UNKNOWN = "unknown"
    MAY_RETRIEVE = "may_retrieve"
    OA_NO_DOWNLOAD = "oa_no_download"
    SUBSCRIPTION_ONLY = "subscription_only"
    NOT_PERMITTED = "not_permitted"


class PermissionBasis(str, Enum):
    """Why the permission field says what it says. Recorded so it can be audited."""

    UNKNOWN = "unknown"
    LICENSE = "license"                    # an explicit, permissive licence was found
    OA_STATUS_ONLY = "oa_status_only"      # only an OA classification was available
    OA_STATUS_CLOSED = "oa_status_closed"  # the work is known to be closed


class LiteratureType(str, Enum):
    """The kind of work, as one level of the bibliographic hierarchy (§18).

    A typed view of the source's own `type` string (kept verbatim in
    `UpstreamFacts.type`); it is a normalization of a stated fact, never an
    inference. "article" and "journal article" are deliberately not the same
    value.
    """

    JOURNAL_ARTICLE = "journal_article"
    CONFERENCE_PAPER = "conference_paper"
    PREPRINT = "preprint"
    BOOK_CHAPTER = "book_chapter"
    BOOK = "book"
    DATASET = "dataset"
    REVIEW = "review"
    OTHER = "other"
    UNKNOWN = "unknown"


#: Crossref's own work types -> the typed view. Anything unmapped is OTHER (a
#: real but unclassified work), and a missing type is UNKNOWN.
_LITERATURE_TYPE_BY_UPSTREAM: dict[str, LiteratureType] = {
    "journal-article": LiteratureType.JOURNAL_ARTICLE,
    "proceedings-article": LiteratureType.CONFERENCE_PAPER,
    "posted-content": LiteratureType.PREPRINT,
    "book-chapter": LiteratureType.BOOK_CHAPTER,
    "book": LiteratureType.BOOK,
    "dataset": LiteratureType.DATASET,
    "review": LiteratureType.REVIEW,
    "review-article": LiteratureType.REVIEW,
}


class AccessEvidence(BaseModel):
    """One piece of evidence bearing on the permission decision (Stage 4.3).

    A structured reason, so "why MAY_RETRIEVE?" is answerable with the source
    and the retrieved_at. It never carries a confidence score -- the decision
    is deterministic policy, not a probability.
    """

    kind: str = ""       # open_license | oa_location | oa_status | closed | tdm_grant | conflict | url_only
    detail: str = ""
    source: str = ""
    retrieved_at: str = ""


class AccessAssessment(BaseModel):
    oa_status: OAStatus = OAStatus.UNKNOWN
    #: The upstream string before it was mapped, kept verbatim. A structured
    #: field rather than a note: notes are for humans, and a machine that has
    #: to grep them for a value is one wording change away from a silent bug.
    oa_status_raw: str | None = None
    access_status: AccessStatus = AccessStatus.NOT_CHECKED
    permission: FulltextPermission = FulltextPermission.UNKNOWN
    permission_basis: PermissionBasis = PermissionBasis.UNKNOWN

    source: str = ""                        # which provider reported this
    source_url: str | None = None           # the endpoint that produced it
    license: LicenseInfo = Field(default_factory=LicenseInfo)
    fulltext_url: str | None = None
    url_type: Literal["landing_page", "pdf", "unknown"] = "unknown"
    oa_version: str = "UNKNOWN"

    checked_at: str | None = None
    notes: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    #: Stage 4.3: which policy produced `permission`, the human-readable
    #: reasons behind it, and the structured evidence for an audit trail.
    policy_version: str = ""
    reasons: list[str] = Field(default_factory=list)
    access_evidence: list[AccessEvidence] = Field(default_factory=list)


class RetrievalOutcome(str, Enum):
    """The result of a retrieval *attempt*. Deliberately not an AccessStatus:

    a failed download says nothing about whether the work is open."""

    SUCCESS = "success"
    NO_URL = "no_url"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    NETWORK_ERROR = "network_error"
    NOT_ATTEMPTED = "not_attempted"


class RetrievalAttempt(BaseModel):
    paper_id: str
    attempted_at: str
    outcome: RetrievalOutcome = RetrievalOutcome.NOT_ATTEMPTED
    http_status: int | None = None
    note: str = ""


# ---------------------------------------------------------------------------
# works
# ---------------------------------------------------------------------------


class Author(BaseModel):
    given: str = ""
    family: str = ""
    name: str = ""
    orcid: str | None = None
    affiliation: str | None = None
    # Crossref supplies an explicit ordering hint. Recorded, not re-derived:
    # a missing affiliation must not cost us the author, and `sequence` is
    # something the source stated rather than something we inferred.
    sequence: str | None = None


class Identifiers(BaseModel):
    """External references, one generic bag (§12). DOI is the identity key;
    the rest are pointers to other systems. No provider-specific JSON blob."""

    doi: str | None = None
    openalex_id: str | None = None
    crossref_id: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    mag_id: str | None = None
    isbn: str | None = None


class UpstreamFacts(BaseModel):
    """Facts the source stated that have no typed field on `Paper`.

    Held apart from everything CREA interpreted. A publisher name, a work type
    and a citation count are upstream facts; a permission, a similarity verdict
    or a "relevance" ranking would be ours, and they live elsewhere. Keeping
    the two in different places is what stops a reported fact from being read
    as a conclusion.
    """

    publisher: str | None = None
    type: str | None = None            # Crossref `type`, e.g. "journal-article"
    is_referenced_by_count: int | None = None
    publication_date: str | None = None
    publication_date_type: str | None = None   # which upstream field it came from
    #: Every publication date the source published, keyed by its own field name.
    #: The selected one is a CREA choice; the others are still facts, and a
    #: reader must be able to see what was passed over.
    publication_dates_raw: dict[str, str] = Field(default_factory=dict)
    #: Which CREA policy made the selection, versioned so a future change can
    #: be told apart from this one.
    date_policy_version: str | None = None
    issn: list[str] = Field(default_factory=list)
    #: The language the source stated for the work, verbatim (e.g. "en").
    language: str | None = None
    notes: list[str] = Field(default_factory=list)


class Paper(BaseModel):
    """The literature record (Stage 4.1).

    `paper_id` is CREA's internal record identity -- never a DOI and never an
    international identifier (§4/§5). The DOI lives in `identifiers.doi` and is
    optional: a work with only a title/author/year/URL is still a record (§8).
    """

    paper_id: str
    # Nullable on purpose: a Crossref work can genuinely carry no title, and
    # inventing a placeholder string would make "untitled" indistinguishable
    # from "titled with an empty name".
    title: str | None = None
    authors: list[Author] = Field(default_factory=list)
    journal: str | None = None
    year: int | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None

    abstracts: list[AbstractRecord] = Field(default_factory=list)
    identifiers: Identifiers = Field(default_factory=Identifiers)
    access: AccessAssessment = Field(default_factory=AccessAssessment)
    url: str | None = None

    # provenance -- required, never defaulted away
    upstream: UpstreamFacts = Field(default_factory=UpstreamFacts)

    sources: list[str] = Field(default_factory=list)
    source_ids: dict[str, str] = Field(default_factory=dict)
    retrieved_at: str
    raw_hashes: dict[str, str] = Field(default_factory=dict)
    merge_conflicts: list[str] = Field(default_factory=list)

    @property
    def abstract(self) -> AbstractRecord | None:
        for source in ABSTRACT_PREFERENCE:
            for record in self.abstracts:
                if record.source is source:
                    return record
        return self.abstracts[0] if self.abstracts else None

    @property
    def doi(self) -> str | None:
        return self.identifiers.doi

    @property
    def normalized_doi(self) -> str | None:
        """The canonical DOI form used as the identity/dedup key (§7)."""
        from crea.core.ids import normalize_doi

        return normalize_doi(self.identifiers.doi)

    @property
    def literature_type(self) -> LiteratureType:
        """The typed view of the source's own work type (§18)."""
        raw = (self.upstream.type or "").strip().lower()
        if not raw:
            return LiteratureType.UNKNOWN
        return _LITERATURE_TYPE_BY_UPSTREAM.get(raw, LiteratureType.OTHER)


class SearchResult(BaseModel):
    query: str
    papers: list[Paper] = Field(default_factory=list)
    providers_used: list[str] = Field(default_factory=list)
    providers_unavailable: list[ProviderStatusRef] = Field(default_factory=list)
    retrieved_at: str


class ProviderStatusRef(BaseModel):
    """Deliberately a small record, not an import of the provider layer.

    `core` must not depend on `providers`, so the shape is restated here and
    the provider layer constructs it.
    """

    name: str
    availability: str
    reason: str = ""

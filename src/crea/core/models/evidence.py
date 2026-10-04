"""Evidence and Claim: a citable unit and a statement resting on citable units.

A `Paper` answers "what is this work". An `Evidence` answers "which specific
piece of it says what". A `Claim` answers "what do we assert", and is not
expressible without naming the Evidence it rests on.

The separation matters for one reason: a Claim is only as good as the things it
points at, so the pointer has to be checked, not assumed. `Claim.evidence_refs`
is therefore `min_length=1` -- an unevidenced claim is a type error rather than
a code review question.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from crea.core.ids import content_hash
from crea.core.models.literature import EvidenceStrength, Paper
from crea.core.provenance import Provenance

#: Bumped when the shape of `Locator` changes meaning. A locator produced under
#: one version is not silently interchangeable with another.
LOCATOR_VERSION = "locator-v2"

#: Bumped when the rule for deriving `evidence_id` changes, which would change
#: the identity of already-cited evidence.
EVIDENCE_ID_VERSION = "evidence-id-v1"


class EvidenceType(str, Enum):
    """What kind of thing was cited.

    The distinctions are load-bearing. An `ABSTRACT` saying "compound X showed
    activity against Y" is not an `EXPERIMENTAL` record of a synthesis at a
    temperature, and collapsing the two would let a paper's marketing sentence
    stand in for its method.
    """

    METADATA = "metadata"                    # title, DOI, year, journal
    ABSTRACT = "abstract"                    # the abstract, whatever its provenance
    STRUCTURED_DATABASE = "structured_database"  # a curated record, e.g. a compound entry
    FULLTEXT = "fulltext"                    # body text of the work
    EXPERIMENTAL = "experimental"            # the experimental section specifically
    USER_PROVIDED_DOCUMENT = "user_provided_document"   # a file the user supplied


class Locator(BaseModel):
    """Where inside a source the cited text sits.

    Deliberately shallow: one flat record with optional parts, versioned. A
    metadata field needs `field`; a PDF passage needs `page` and `section`; a
    user document needs `document_id` and a character range. Filling those in
    later must not require redesigning the Evidence model, and it must not
    require stringly-typed location either -- which is why this is a record and
    not a free-text note.

    `canonical()` is what the evidence ID hashes, so its output is part of the
    frozen ID contract.
    """

    locator_version: str = LOCATOR_VERSION
    locator_type: Literal[
        "metadata_field", "abstract", "document_region", "structured_record"
    ] = "metadata_field"
    field: str | None = None
    document_id: str | None = None
    section: str | None = None
    #: Single page, for a locator that names one. A region spanning pages uses
    #: `page_start` / `page_end`; both are part of the hashed identity, so a
    #: document locator produced now is comparable with one produced later.
    page: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    char_range: tuple[int, int] | None = None

    def canonical(self) -> str:
        """Stable rendering for hashing. Order and spelling are frozen."""
        parts = [
            f"v={self.locator_version}",
            f"t={self.locator_type}",
            f"f={self.field or ''}",
            f"d={self.document_id or ''}",
            f"s={self.section or ''}",
            f"p={self.page if self.page is not None else ''}",
            f"ps={self.page_start if self.page_start is not None else ''}",
            f"pe={self.page_end if self.page_end is not None else ''}",
            f"c={self.char_range[0]}-{self.char_range[1]}"
            if self.char_range else "c=",
        ]
        return "|".join(parts)


def evidence_identity(
    source: str,
    source_id: str,
    evidence_type: EvidenceType | str,
    locator: Locator,
    raw_quote: str,
) -> str:
    """The deterministic identity of a piece of evidence.

    `retrieved_at` is deliberately **not** an input. Re-fetching the same work
    and finding the same sentence is the same piece of evidence, not a new one.
    Including the timestamp would make every `Claim.evidence_refs` entry rot the
    first time a citation was re-resolved, and would make two runs of the same
    pipeline disagree about whether they had ever cited the same thing.
    """
    payload = "|".join([
        EVIDENCE_ID_VERSION,
        source or "",
        source_id or "",
        str(evidence_type),
        locator.canonical(),
        raw_quote or "",
    ])
    return content_hash(payload, 20)


class Evidence(BaseModel):
    """One citable thing, traceable to a source, a record and a place in it.

    Exactly one source. A statement supported by three sources is a Claim with
    three `evidence_refs`, not an Evidence with three sources -- otherwise
    there is no way to say which one said what, and "two of them agree" becomes
    unrepresentable.
    """

    evidence_id: str
    evidence_type: EvidenceType
    strength: EvidenceStrength

    source: str                 # "openalex" | "crossref" | "unpaywall" | "user_provided"
    source_id: str              # that source's identifier for the record
    source_url: str | None = None
    retrieved_at: str

    #: The cited text itself. A quotable string, never a model repr: `str(paper)`
    #: is not evidence of anything a reader could check.
    raw_quote: str
    locator: Locator = Field(default_factory=Locator)
    provenance: Provenance | None = None

    paper_id: str | None = None
    doi: str | None = None

    @field_validator("source", "source_id")
    @classmethod
    def _source_is_named(cls, value: str) -> str:
        # A citation to nowhere is not a citation. Both are required, and an
        # Evidence cannot be constructed without them.
        if not value or not value.strip():
            raise ValueError(
                "Evidence.source and Evidence.source_id must both be non-empty; "
                "evidence must be attributable to a source record"
            )
        return value

    @field_validator("raw_quote")
    @classmethod
    def _quote_is_not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError(
                "Evidence.raw_quote must be the cited text; an empty string "
                "cites nothing"
            )
        return value

    @classmethod
    def build(
        cls,
        *,
        evidence_type: EvidenceType,
        strength: EvidenceStrength,
        source: str,
        source_id: str,
        raw_quote: str,
        locator: Locator,
        retrieved_at: str,
        source_url: str | None = None,
        provenance: Provenance | None = None,
        paper_id: str | None = None,
        doi: str | None = None,
    ) -> "Evidence":
        return cls(
            evidence_id=evidence_identity(
                source, source_id, evidence_type, locator, raw_quote),
            evidence_type=evidence_type,
            strength=strength,
            source=source,
            source_id=source_id,
            source_url=source_url,
            retrieved_at=retrieved_at,
            raw_quote=raw_quote,
            locator=locator,
            provenance=provenance,
            paper_id=paper_id,
            doi=doi,
        )

    @property
    def is_reconstructed(self) -> bool:
        """True when the quoted text was reassembled rather than written.

        Carried on the Evidence itself, not inferred from the source name, so it
        cannot be lost in a merge or a re-derivation.
        """
        return self.provenance is not None and bool(
            self.provenance.params.get("reconstructed")
        )


class Claim(BaseModel):
    """A statement, and the Evidence it rests on.

    There is no "AI confidence" field. No such quantity is defined anywhere in
    this project, and a field with no definition invites a number in it.
    """

    claim_id: str
    text: str
    claim_type: str
    #: At least one, enforced by the type. An unevidenced claim cannot be
    #: constructed, which is the point: "trust me" is not an option the model
    #: offers.
    evidence_refs: list[str] = Field(min_length=1)
    strength: EvidenceStrength | None = None
    provenance: Provenance | None = None

    @classmethod
    def build(cls, claim_id: str, text: str, claim_type: str,
              evidence_refs: list[str], strength: EvidenceStrength | None = None,
              provenance: Provenance | None = None) -> "Claim":
        return cls(claim_id=claim_id, text=text, claim_type=claim_type,
                   evidence_refs=evidence_refs, strength=strength,
                   provenance=provenance)

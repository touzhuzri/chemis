"""EvidenceService: Paper -> Evidence, and Claim -> verified.

Pure transformation and pure validation. It mutates nothing, fetches nothing,
downloads nothing and asks no model. A `Paper` that goes in comes out
byte-identical, which is what makes it safe to run over anything the
literature layer produced.

Two boundaries are deliberate:

* **A search is not a citation.** `LiteratureService` finds works; this service
  turns a work into citable facts. Folding the second into the first would make
  every search pay for a conversion nobody asked for, and would put Evidence
  production behind a network call.
* **Validation is not retrieval.** `EvidenceRegistry` is an in-memory collection
  the caller supplies. Nothing here queries a database, and a missing reference
  is reported rather than looked up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crea.core.ids import normalize_doi
from crea.core.models.evidence import (
    Claim,
    Evidence,
    EvidenceType,
    Locator,
    evidence_identity,
)
from crea.core.models.literature import AbstractRecord, EvidenceStrength, Paper
from crea.core.policy.evidence import (
    CITED_METADATA_FIELDS,
    strength_for,
)
from crea.core.provenance import Provenance, utc_now_iso

#: The producer identity every derived Evidence carries. Recorded so a reader can
#: tell an Evidence we derived from a Paper from one a provider stated directly.
EVIDENCE_PRODUCER = "crea.services.evidence"
EVIDENCE_PRODUCER_VERSION = "evidence-v1"


def _require_paper(paper: object) -> Paper:
    """Fail legibly on the wrong kind of object.

    `Paper` and `Molecule` are both pydantic models with a `source`-ish
    attribute, so passing the wrong one produced an AttributeError about an
    unrelated field. The boundary is worth stating.
    """
    if not isinstance(paper, Paper):
        raise TypeError(
            f"expected a core.models.literature.Paper, got "
            f"{type(paper).__module__}.{type(paper).__name__}"
        )
    return paper


def _paper_source(paper: Paper, source: str | None, source_id: str | None):
    """Which provider the cited field came from.

    A merged `Paper` no longer knows, per field, who said what -- that lives in
    the merge result's resolutions. So a caller with a merge result passes the
    source in; without one we attribute to the paper's first source and say so.
    Guessing per-field attribution for a merged record would be inventing
    provenance.
    """
    chosen = source or (paper.sources[0] if paper.sources else "unknown")
    identifier = source_id or paper.source_ids.get(chosen) or ""
    if not identifier:
        identifier = normalize_doi(paper.identifiers.doi) or paper.paper_id
    return chosen, identifier


def _provenance(source: str, source_id: str, raw_payload_hash: str | None,
                params: dict) -> Provenance:
    return Provenance(
        tool=EVIDENCE_PRODUCER,
        tool_version=EVIDENCE_PRODUCER_VERSION,
        params=params,
        source=source,
        source_id=source_id,
        raw_payload_hash=raw_payload_hash,
    )


def paper_to_metadata_evidence(
    paper: Paper,
    *,
    source: str | None = None,
    source_id: str | None = None,
) -> list[Evidence]:
    """Derive the citable metadata facts of a Paper.

    A minimal set -- title, DOI, year, journal -- and nothing else. A field
    that is not cited is not evidence, and every field added here is a field a
    Claim may later lean on.
    """
    _require_paper(paper)
    chosen_source, chosen_id = _paper_source(paper, source, source_id)
    payload_hash = paper.raw_hashes.get(chosen_source) or next(
        iter(paper.raw_hashes.values()), None)
    doi = normalize_doi(paper.identifiers.doi)

    values = {
        "title": paper.title,
        "doi": doi,
        "publication_year": str(paper.year) if paper.year is not None else None,
        "journal": paper.journal,
    }

    out: list[Evidence] = []
    for field_name in CITED_METADATA_FIELDS:
        value = values.get(field_name)
        if value in (None, ""):
            # A field nobody has is an absence, not a piece of evidence. It is
            # skipped rather than emitted with an empty quote, because an
            # Evidence with nothing in it is a citation that proves nothing.
            continue
        out.append(Evidence.build(
            evidence_type=EvidenceType.METADATA,
            strength=strength_for(EvidenceType.METADATA),
            source=chosen_source,
            source_id=chosen_id,
            raw_quote=str(value),
            locator=Locator(
                locator_type="metadata_field",
                field=field_name,
            ),
            retrieved_at=paper.retrieved_at or utc_now_iso(),
            source_url=paper.url,
            provenance=_provenance(chosen_source, chosen_id, payload_hash,
                                   {"field": field_name, "derived_from": "paper"}),
            paper_id=paper.paper_id,
            doi=doi,
        ))
    return out


def abstract_to_evidence(
    paper: Paper,
    record: AbstractRecord,
    *,
    source: str | None = None,
    source_id: str | None = None,
) -> Evidence | None:
    """Cite a paper's abstract, keeping the abstract's own provenance intact.

    A Crossref abstract and an OpenAlex reconstruction arrive here as different
    records and leave as different Evidence. The reconstruction keeps its
    `reconstructed` flag, so nothing downstream can read it as a publisher's
    own words.
    """
    _require_paper(paper)
    if not record.text.strip():
        return None

    chosen_source, chosen_id = _paper_source(paper, source, source_id)
    # The abstract came from whoever supplied it, which is not necessarily
    # whoever supplied the rest of the paper.
    abstract_source = record.source.value
    payload_hash = paper.raw_hashes.get(chosen_source) or next(
        iter(paper.raw_hashes.values()), None)
    doi = normalize_doi(paper.identifiers.doi)

    locator = Locator(
        locator_type="abstract",
        field="abstract",
        document_id=chosen_id or None,
    )
    return Evidence.build(
        evidence_type=EvidenceType.ABSTRACT,
        strength=strength_for(EvidenceType.ABSTRACT, abstract=record),
        source=abstract_source,
        source_id=chosen_id or doi or "",
        raw_quote=record.text,
        locator=locator,
        retrieved_at=record.retrieved_at or paper.retrieved_at or utc_now_iso(),
        source_url=paper.url,
        provenance=_provenance(abstract_source, chosen_id, payload_hash, {
            "abstract_source": abstract_source,
            "reconstructed": record.is_reconstructed,
            "derived_from": "paper.abstracts",
        }),
        paper_id=paper.paper_id,
        doi=doi,
    )


# ---------------------------------------------------------------------------
# registry and validation
# ---------------------------------------------------------------------------


@dataclass
class EvidenceRegistry:
    """An in-memory collection of evidence the caller already holds.

    Not a database and not a cache. It exists so that validating a Claim means
    resolving its references against something concrete, instead of trusting
    that they were written correctly.
    """

    items: dict[str, Evidence] = field(default_factory=dict)

    def add(self, evidence: Evidence) -> Evidence:
        existing = self.items.get(evidence.evidence_id)
        if existing is not None and existing != evidence:
            # Two different things claiming one identity is a bug in a producer,
            # and silently keeping the first would hide it.
            raise ValueError(
                f"evidence_id {evidence.evidence_id} already registered with "
                f"different content"
            )
        self.items[evidence.evidence_id] = evidence
        return evidence

    def extend(self, evidence: list[Evidence]) -> None:
        for item in evidence:
            self.add(item)

    def get(self, evidence_id: str) -> Evidence | None:
        return self.items.get(evidence_id)

    def __len__(self) -> int:
        return len(self.items)

    def __contains__(self, evidence_id: object) -> bool:
        return evidence_id in self.items


@dataclass
class ClaimValidation:
    ok: bool
    missing_refs: list[str] = field(default_factory=list)
    duplicate_refs: list[str] = field(default_factory=list)
    weak_refs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def raise_if_invalid(self, claim: Claim | None = None) -> None:
        if self.ok:
            return
        from crea.core.errors import EvidenceIntegrityError

        parts = []
        if self.missing_refs:
            parts.append(f"missing evidence: {', '.join(self.missing_refs)}")
        if self.duplicate_refs:
            parts.append(f"duplicate evidence: {', '.join(self.duplicate_refs)}")
        if self.weak_refs:
            parts.append(f"not primary evidence: {', '.join(self.weak_refs)}")
        prefix = f"claim {claim.claim_id}: " if claim else ""
        raise EvidenceIntegrityError(prefix + "; ".join(parts))


def validate_claim(claim: Claim, registry: EvidenceRegistry) -> ClaimValidation:
    """Check a Claim's references against evidence we actually hold.

    Referential integrity only. It makes no chemical judgement: whether the
    cited text really supports the claim is a question for a reader, and
    pretending otherwise here would be worse than saying nothing.
    """
    result = ClaimValidation(ok=True)

    seen: set[str] = set()
    for ref in claim.evidence_refs:
        if ref in seen:
            # Decided rather than left open: citing the same evidence twice is
            # either a mistake or an attempt to make one source look like two,
            # and neither is a reason to be lenient about it.
            result.duplicate_refs.append(ref)
        seen.add(ref)

        evidence = registry.get(ref)
        if evidence is None:
            result.missing_refs.append(ref)
            continue
        if evidence.evidence_id != ref:
            result.notes.append(f"registry id mismatch for {ref}")
        if claim.claim_type == "experimental_fact" \
                and evidence.strength is not EvidenceStrength.PRIMARY_DOCUMENT_EVIDENCE \
                and evidence.strength is not EvidenceStrength.USER_PROVIDED_EVIDENCE:
            # flagged, not rejected: the claim is not automatically wrong, but
            # nobody should be told a method came from an abstract
            result.weak_refs.append(ref)

    result.ok = not (result.missing_refs or result.duplicate_refs)
    return result


class EvidenceService:
    """Thin, stateless facade over the functions above."""

    @staticmethod
    def metadata_from_paper(paper: Paper, **kwargs) -> list[Evidence]:
        return paper_to_metadata_evidence(paper, **kwargs)

    @staticmethod
    def from_abstract(paper: Paper, record: AbstractRecord, **kwargs):
        return abstract_to_evidence(paper, record, **kwargs)

    @staticmethod
    def registry_from_paper(paper: Paper, **kwargs) -> EvidenceRegistry:
        registry = EvidenceRegistry()
        registry.extend(paper_to_metadata_evidence(paper, **kwargs))
        for record in paper.abstracts:
            evidence = abstract_to_evidence(paper, record, **kwargs)
            if evidence is not None:
                registry.add(evidence)
        return registry

    @staticmethod
    def validate(claim: Claim, registry: EvidenceRegistry) -> ClaimValidation:
        return validate_claim(claim, registry)

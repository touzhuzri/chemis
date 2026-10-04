"""Document -> Evidence, and finding passages inside a local document.

Kept separate from `services/evidence.py` on purpose: that module turns a
`Paper` into citable facts, this one turns a *file* into them. Different
sources, different trust, different strength. Fusing them would make it
possible to write `document_region_to_evidence(paper, ...)` and get something
that looks like a citation to a file nobody holds.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field

from crea.core.models.document import LocalDocument
from crea.core.models.evidence import Evidence, EvidenceType, Locator
from crea.core.models.literature import EvidenceStrength
from crea.core.provenance import Provenance

DOCUMENT_SOURCE = "user_provided"
"""The source name for anything that came off this machine.

Chosen so it can never be mistaken for a provider. Unpaywall tells us where an
open-access copy lives; this tells us the user handed us a file. Neither is a
licence, and `user_provided` is the word that cannot be misread as one.
"""

EVIDENCE_PRODUCER = "crea.services.document_evidence"
EVIDENCE_PRODUCER_VERSION = "document-evidence-v1"

#: Token matches closer together than this are one occurrence, not several.
TOKEN_COLLAPSE_CHARS = 24


class MatchType(str, Enum):
    """How a query was found. Not a score."""

    SUBSTRING = "substring"
    CASE_INSENSITIVE = "case_insensitive"
    TOKEN = "token"
    NOT_MATCHED = "not_matched"


class ExperimentalEvidencePolicy:
    """Which EvidenceType a document region earns, from where it sits.

    A passage under an experimental or methods heading is `EXPERIMENTAL`;
    anywhere else in a document it is `FULLTEXT`. That is a claim about *where
    the words are*, not about whether they are right -- a paper's discussion
    can still be wrong.
    """

    EXPERIMENTAL_SECTIONS = frozenset({"experimental_or_methods"})

    @classmethod
    def evidence_type_for(cls, section_kind: str | None) -> EvidenceType:
        if section_kind in cls.EXPERIMENTAL_SECTIONS:
            return EvidenceType.EXPERIMENTAL
        return EvidenceType.FULLTEXT


def document_region_to_evidence(
    document: LocalDocument,
    quote: str,
    *,
    page_start: int,
    page_end: int | None = None,
    section: str | None = None,
    section_kind: str | None = None,
    char_range: tuple[int, int] | None = None,
    strength: EvidenceStrength = EvidenceStrength.USER_PROVIDED_EVIDENCE,
) -> Evidence:
    """Cite a passage of a local document.

    `quote` must be text that came out of the file. There is deliberately no
    path here that can take a model's repr: a citation to `str(page)` proves
    nothing a reader could check.

    The default strength is `USER_PROVIDED_EVIDENCE`, **not**
    `PRIMARY_DOCUMENT_EVIDENCE`. A file being local says we were *given* it.
    It says nothing about whether it is the publisher's PDF, a scan, somebody's
    notes, or a preprint, and this project has no way to tell. Being handed a
    document is not the same as the document being the primary record.
    """
    page_end = page_end or page_start
    locator = Locator(
        locator_type="document_region",
        document_id=document.document_id,
        page_start=page_start,
        page_end=page_end,
        page=page_start,
        section=section,
        char_range=char_range,
    )
    return Evidence.build(
        evidence_type=ExperimentalEvidencePolicy.evidence_type_for(section_kind),
        strength=strength,
        source=DOCUMENT_SOURCE,
        source_id=document.document_id,
        raw_quote=quote,
        locator=locator,
        retrieved_at=document.ingested_at,
        source_url=document.source_path or None,
        provenance=Provenance(
            tool=EVIDENCE_PRODUCER,
            tool_version=EVIDENCE_PRODUCER_VERSION,
            params={
                "derived_from": "local_document",
                "document_sha256": document.sha256,
                "page_start": page_start,
                "page_end": page_end,
                "section": section,
                "section_kind": section_kind,
            },
            source=DOCUMENT_SOURCE,
            source_id=document.document_id,
            # the document's own content hash, so the Evidence can be traced to
            # the exact bytes it was read from
            raw_payload_hash=document.sha256,
        ),
    )


class EvidenceHit(BaseModel):
    """A found passage.

    Deliberately carries **no relevance score**. A number here would be the
    first thing a future ranking is built on, and a number nobody defined is
    worse than no number: it looks like a measurement.
    """

    evidence: Evidence
    match_type: MatchType
    matched_count: int = 0
    position: int | None = None      # offset within the page's extracted text
    page: int = 0
    section: str | None = None
    section_kind: str | None = None


def find_occurrences(haystack: str, needle: str) -> list[tuple[int, MatchType]]:
    """Locate a query deterministically: exact, then case-folded, then tokens.

    No ranking and no scoring -- hits come back in the order they appear.
    """
    if not needle or not needle.strip():
        return []
    exact = [m.start() for m in re.finditer(re.escape(needle), haystack)]
    if exact:
        return [(i, MatchType.SUBSTRING) for i in exact]

    folded = [(m.start(), MatchType.CASE_INSENSITIVE)
              for m in re.finditer(re.escape(needle), haystack, re.IGNORECASE)]
    if folded:
        return folded

    tokens = [t for t in re.split(r"\W+", needle) if t]
    if not tokens:
        return []
    pattern = r"\b(?:" + "|".join(re.escape(t) for t in tokens) + r")\b"
    raw = [m.start() for m in re.finditer(pattern, haystack, re.IGNORECASE)]
    if not raw:
        return []

    # One hit per *occurrence of the token set*, not one per token. Searching
    # "hydride sodium" against a sentence containing both would otherwise
    # return two hits for the same place, and a page of chemistry prose would
    # fill with near-duplicates of every query.
    collapsed: list[int] = []
    for position in raw:
        if collapsed and position - collapsed[-1] < TOKEN_COLLAPSE_CHARS:
            continue
        collapsed.append(position)
    return [(p, MatchType.TOKEN) for p in collapsed]


def _section_for(sections, position: int):
    for section in sections:
        if section.char_start <= position < section.char_end:
            return section
    return sections[0] if sections else None


def search_document(
    parsed,
    query: str,
    *,
    section: str | None = None,
    page: int | None = None,
    context_chars: int = 300,
    max_hits: int = 50,
) -> list[EvidenceHit]:
    """Find passages in one document that match a query.

    Substring, case-insensitive and token matching, filtered by section and
    page. It is not semantic search, it does not understand a question, and it
    does not rank. "Which catalyst did this reaction use" is a language
    question, and it belongs to a later phase.

    A whole PDF is never shredded into Evidence: passages are produced where a
    query actually lands.
    """
    from crea.services.document_sections import sections_on_page

    hits: list[EvidenceHit] = []
    document = parsed.document

    for candidate in parsed.pages:
        if page is not None and candidate.page_number != page:
            continue
        on_page = sections_on_page(parsed, candidate.page_number)
        if section is not None:
            on_page = [s for s in on_page if s.kind.value == section]
        elif on_page and not any(s.text for s in on_page):
            continue

        occurrences = find_occurrences(candidate.text, query)
        if not occurrences:
            continue

        for position, match_type in occurrences:
            # the section the *match* falls in, not the first on the page
            found = _section_for(on_page, position)
            start = max(0, position - context_chars // 2)
            end = min(len(candidate.text), position + context_chars)
            quote = candidate.text[start:end].strip()
            if not quote:
                continue
            heading = found.detected_heading if found else None
            kind = found.kind.value if found else None
            hits.append(EvidenceHit(
                evidence=document_region_to_evidence(
                    document,
                    quote,
                    page_start=candidate.page_number,
                    page_end=candidate.page_number,
                    section=heading,
                    section_kind=kind,
                    char_range=(start, end),
                ),
                match_type=match_type,
                matched_count=len(occurrences),
                position=position,
                page=candidate.page_number,
                section=heading,
                section_kind=kind,
            ))
            if len(hits) >= max_hits:
                return hits
    return hits


class DocumentEvidenceService:
    """Thin facade. Stateless; a `ParsedDocument` goes in, `Evidence` comes out."""

    def __init__(self, service=None) -> None:
        self._service = service

    def from_region(self, document, quote: str, **kwargs) -> Evidence:
        return document_region_to_evidence(document, quote, **kwargs)

    def search(self, parsed, query: str, **kwargs) -> list[EvidenceHit]:
        return search_document(parsed, query, **kwargs)

"""Deterministic section detection.

No model, no heuristics beyond matching, and it is deliberately *literal*:
a heading is recognised when the normalised line equals a known heading, not
when it merely contains one.

That matters most for the one case the phase brief called out: "Experimental
results" is not an experimental method section, and a detector that greps for
the word "Experimental" would put a results discussion under
`EXPERIMENTAL_OR_METHODS` — which is exactly the section whose contents would
then be promoted to `EXPERIMENTAL` evidence. A false positive here silently
reclassifies results as methods. So the matcher is an exact table.

What was actually found is kept on `detected_heading`, so a reader can see the
wording and disagree with us.
"""

from __future__ import annotations

import re
import unicodedata

from crea.core.models.document import (
    DocumentSection,
    DocumentSectionKind,
    ParsedDocument,
)

#: Normalised heading -> what it means. Exact matches only.
_HEADINGS: dict[str, DocumentSectionKind] = {
    "abstract": DocumentSectionKind.ABSTRACT,
    "summary": DocumentSectionKind.ABSTRACT,

    "introduction": DocumentSectionKind.INTRODUCTION,
    "background": DocumentSectionKind.INTRODUCTION,

    "results": DocumentSectionKind.RESULTS,
    "results and discussion": DocumentSectionKind.RESULTS,
    "results and discussions": DocumentSectionKind.RESULTS,

    "discussion": DocumentSectionKind.DISCUSSION,
    "discussions": DocumentSectionKind.DISCUSSION,
    "general discussion": DocumentSectionKind.DISCUSSION,

    "experimental": DocumentSectionKind.EXPERIMENTAL_OR_METHODS,
    "experiments": DocumentSectionKind.EXPERIMENTAL_OR_METHODS,
    "experimental section": DocumentSectionKind.EXPERIMENTAL_OR_METHODS,
    "experimental procedures": DocumentSectionKind.EXPERIMENTAL_OR_METHODS,
    "method": DocumentSectionKind.EXPERIMENTAL_OR_METHODS,
    "methods": DocumentSectionKind.EXPERIMENTAL_OR_METHODS,
    "material and methods": DocumentSectionKind.EXPERIMENTAL_OR_METHODS,
    "materials and methods": DocumentSectionKind.EXPERIMENTAL_OR_METHODS,
    "materials and method": DocumentSectionKind.EXPERIMENTAL_OR_METHODS,

    "conclusion": DocumentSectionKind.CONCLUSION,
    "conclusions": DocumentSectionKind.CONCLUSION,
    "concluding remarks": DocumentSectionKind.CONCLUSION,

    "references": DocumentSectionKind.REFERENCES,
    "reference": DocumentSectionKind.REFERENCES,
    "bibliography": DocumentSectionKind.REFERENCES,
    "literature cited": DocumentSectionKind.REFERENCES,

    "supporting information": DocumentSectionKind.SUPPORTING_INFORMATION,
    "supplementary information": DocumentSectionKind.SUPPORTING_INFORMATION,
    "supporting information and methods": DocumentSectionKind.SUPPORTING_INFORMATION,
}

_NUMBERING = re.compile(r"^\s*(?:[0-9]+|[ivxlcdm]+)[.)]?\s+", re.IGNORECASE)
_WS = re.compile(r"\s+")


def normalize_heading(line: str) -> str:
    """Fold a heading line to its comparable form.

    Unicode-folded, lower-cased, numbering stripped, punctuation trimmed,
    whitespace collapsed. `"3.  Experimental Section:"` and `"experimental
    section"` both become `experimental section`.
    """
    if not line:
        return ""
    text = unicodedata.normalize("NFKD", line)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.strip().lower()
    text = _NUMBERING.sub("", text)
    text = re.sub(r"[^a-z0-9+ ]+", " ", text)
    return _WS.sub(" ", text).strip()


def classify_heading(line: str) -> tuple[DocumentSectionKind, str] | None:
    """The kind a heading line denotes, or None if it is not one we know."""
    normalized = normalize_heading(line)
    if not normalized or len(normalized) > 60:
        # a long "heading" is a sentence someone capitalised
        return None
    kind = _HEADINGS.get(normalized)
    if kind is None:
        return None
    return kind, line.strip()


def detect_sections(parsed: ParsedDocument) -> list[DocumentSection]:
    """Find headings across pages and cut the document at them.

    Text before the first recognised heading is not a section: a title block is
    not an abstract, and calling one would be a guess.
    """
    hits: list[tuple[int, DocumentSectionKind, str]] = []
    for page in parsed.pages:
        for line in page.text.splitlines():
            found = classify_heading(line)
            if found is not None:
                kind, heading = found
                if hits and hits[-1][0] == page.page_number \
                        and hits[-1][1] is kind:
                    continue        # a running header repeated on the same page
                hits.append((page.page_number, kind, heading))

    if not hits:
        return []

    # A heading repeated at the top of consecutive pages is a running header, not
    # a new section. Merge before cutting, or "Experimental" on pages 2 and 3
    # becomes two sections where there is one.
    merged: list[tuple[int, DocumentSectionKind, str]] = []
    for page_number, kind, heading in hits:
        if (merged and merged[-1][1] is kind
                and page_number == merged[-1][0] + 1):
            continue          # same kind on the next page: one section
        merged.append((page_number, kind, heading))
    hits = merged

    def page_text(start: int, end: int) -> str:
        return "\n".join(
            page.text for page in parsed.pages if start <= page.page_number <= end
        )

    sections: list[DocumentSection] = []
    for index, (page_start, kind, heading) in enumerate(hits):
        if index + 1 < len(hits):
            page_end = hits[index + 1][0] - 1
        else:
            page_end = parsed.document.page_count or page_start
        if page_end < page_start:
            page_end = page_start

        text = page_text(page_start, page_end)
        # Where this section begins inside that text. Several sections can share
        # a page -- a short communication puts its abstract and its methods on
        # page 1 -- so a passage is attributed by where it falls, not by which
        # page it is on.
        char_start = text.find(heading)
        if char_start < 0:
            char_start = 0
        if index + 1 < len(hits):
            char_end = text.find(hits[index + 1][2], char_start + len(heading))
        else:
            char_end = len(text)
        if char_end <= char_start:
            char_end = len(text)

        sections.append(DocumentSection(
            kind=kind,
            detected_heading=heading,
            page_start=page_start,
            page_end=page_end,
            char_start=char_start,
            char_end=char_end,
            text=text[char_start:char_end],
            char_count=char_end - char_start,
        ))
    return sections


def sections_on_page(parsed: ParsedDocument, page_number: int) -> list[DocumentSection]:
    """Every section that touches a page. Usually one; on a short paper, several."""
    return [s for s in parsed.sections
            if s.page_start <= page_number <= s.page_end]


def section_at(parsed: ParsedDocument, page_number: int, position: int) -> DocumentSection | None:
    """The section a character offset falls in.

    Correct where a page holds more than one section, which is the norm for a
    short communication and the reason `sections_on_page` returns a list.
    """
    for section in sections_on_page(parsed, page_number):
        if section.char_start <= position < section.char_end:
            return section
    # fall back to the first section on the page, so an odd offset still lands
    # somewhere rather than nowhere
    candidates = sections_on_page(parsed, page_number)
    return candidates[0] if candidates else None

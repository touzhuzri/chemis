"""Local documents the user brought with them.

A different kind of thing from a `Paper`. A `Paper` is what an index says a work
is; a `LocalDocument` is a file on this machine. The distinction is not
cosmetic: a user-supplied PDF tells us we were *given* a file to process
locally, and it tells us nothing at all about who published it, under what
licence, or whether anyone else may fetch it. Those are separate questions with
separate answers, and conflating them is how a local file becomes an implied
retrieval permission.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from crea.core.ids import content_hash
from crea.core.provenance import Provenance

DOCUMENT_ID_VERSION = "document-id-v1"


class DocumentSectionKind(str, Enum):
    """What a detected heading says the section is.

    Deliberately coarse. `EXPERIMENTAL` and `METHODS` are the same thing to
    every consumer here, so they are one member; the exact wording that was
    found is kept on `DocumentSection.detected_heading` for a reader who cares.
    Enumerating more would be enumerating guesses.
    """

    ABSTRACT = "abstract"
    INTRODUCTION = "introduction"
    RESULTS = "results"
    DISCUSSION = "discussion"
    EXPERIMENTAL_OR_METHODS = "experimental_or_methods"
    CONCLUSION = "conclusion"
    REFERENCES = "references"
    SUPPORTING_INFORMATION = "supporting_information"
    UNKNOWN = "unknown"


class DocumentMediaType(str, Enum):
    PDF = "application/pdf"
    UNKNOWN = "application/octet-stream"


class DocumentPage(BaseModel):
    """One page. `page_number` is 1-based, as a reader would say it."""

    page_number: int
    text: str
    char_count: int = 0
    #: True when the parser normalised whitespace or line breaks. The bytes in a
    #: PDF are a layout, not a sentence, and pretending the extracted text is
    #: byte-identical to the file would be a small lie that becomes a big one
    #: the first time somebody quotes a hyphenated line break.
    parser_normalized: bool = True


class DocumentSection(BaseModel):
    """A detected section.

    Page bounds alone are not enough: a short communication puts its abstract
    and its experimental section on the *same* page, and attributing a passage
    to whichever section happened to be found first would put a method under
    `ABSTRACT`. So a section also carries the character range it occupies in
    the concatenation of its pages, and a passage is attributed by where it
    falls rather than by which page it is on.
    """

    kind: DocumentSectionKind = DocumentSectionKind.UNKNOWN
    detected_heading: str = ""
    page_start: int
    page_end: int
    char_start: int = 0
    char_end: int = 0
    text: str = ""
    char_count: int = 0


class LocalDocument(BaseModel):
    """A file the user supplied, identified by what it contains.

    The identity is `sha256 + media type`, and nothing else. An absolute path
    is *not* identity: the same paper at `C:\\papers\\a.pdf` and
    `D:\\archive\\a.pdf` is one document, and two files that happen to share a
    name are not.
    """

    document_id: str
    filename: str
    media_type: DocumentMediaType
    sha256: str
    size_bytes: int
    page_count: int = 0
    #: Where the file was when we read it. Provenance, not identity: moving the
    #: file does not make it a different document.
    source_path: str = ""
    ingested_at: str
    provenance: Provenance | None = None

    @staticmethod
    def identity(sha256: str, media_type: DocumentMediaType | str) -> str:
        return content_hash(
            f"{DOCUMENT_ID_VERSION}|{sha256}|{media_type}", 20)

    @classmethod
    def build(cls, **kwargs) -> "LocalDocument":
        sha256 = kwargs["sha256"]
        media_type = kwargs["media_type"]
        return cls(document_id=cls.identity(sha256, media_type), **kwargs)


class ParsedDocument(BaseModel):
    """The deterministic middle layer between a PDF and everything else.

    A third-party parser is an adapter, not a data model. This is what the rest
    of the system sees, so swapping `pypdf` for another parser changes nothing
    above this line.
    """

    document: LocalDocument
    pages: list[DocumentPage] = Field(default_factory=list)
    sections: list[DocumentSection] = Field(default_factory=list)
    #: Free text of the whole document, concatenation of page text. Convenient,
    #: and never what an Evidence should quote.
    full_text: str = ""
    parser: str = ""
    parser_version: str = ""
    warnings: list[str] = Field(default_factory=list)

    def page(self, page_number: int) -> DocumentPage | None:
        for page in self.pages:
            if page.page_number == page_number:
                return page
        return None


class DocumentIdentifierCandidate(BaseModel):
    """A DOI-shaped string found in a local file.

    A candidate, deliberately. Nothing here has been resolved against a
    registry, and a DOI printed in a PDF is a claim made by that file, not a
    fact we have checked.
    """

    value: str
    page_number: int | None = None
    context: str = ""
    extractor: str = "deterministic-regex-v1"


class LinkBasis(str, Enum):
    EXPLICIT_USER = "explicit_user"   # the caller said so
    DOI_MATCH = "doi_match"          # a DOI candidate in the text matched


class PaperDocumentLink(BaseModel):
    """An association between a work we know about and a file we hold.

    A DOI candidate is local evidence, so a link made on one is recorded as a
    `DOI_MATCH` basis and is not described as verified anywhere.
    """

    paper_id: str
    document_id: str
    link_basis: LinkBasis = LinkBasis.EXPLICIT_USER
    doi: str | None = None
    note: str = ""


class DocumentErrorKind(str, Enum):
    NOT_FOUND = "not_found"
    NOT_A_REGULAR_FILE = "not_a_regular_file"
    EMPTY = "empty"
    TOO_LARGE = "too_large"
    UNREADABLE = "unreadable"
    NOT_A_PDF = "not_a_pdf"
    PARSE_FAILED = "parse_failed"


@runtime_checkable
class DocumentParser(Protocol):
    """What CREA needs from a PDF library. Deliberately small.

    Implementations are adapters. Nothing above this protocol may import a
    third-party PDF package.
    """

    name: str
    version: str

    def parse(self, path, sha256: str, media_type: DocumentMediaType) -> ParsedDocument:
        ...

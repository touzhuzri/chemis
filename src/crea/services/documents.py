"""LocalDocumentService: a path the user gave us becomes a ParsedDocument.

Everything here is local. There is no download, no remote lookup, no
authentication of any kind, and nothing in this module or anything it reaches
can fetch a file that was not already on this machine when we were pointed at
it. `fulltext_url` from a provider is still not a permission, and a local file
is not a permission either -- it is an input we were handed.
"""

from __future__ import annotations

import hashlib
import pathlib
import re

from crea.core.errors import DocumentError
from crea.core.models.document import (
    DocumentErrorKind,
    DocumentIdentifierCandidate,
    DocumentMediaType,
    DocumentParser,
    LinkBasis,
    LocalDocument,
    PaperDocumentLink,
    ParsedDocument,
)
from crea.core.policy.document import (
    classify_size,
    looks_like_pdf,
    max_document_bytes,
    media_type_for,
)
from crea.core.provenance import utc_now_iso
from crea.services.document_sections import detect_sections

#: Deliberately conservative. A DOI is `10.` + a registrant code + `/` + a
#: suffix. Anything looser starts matching URLs that merely contain digits.
_DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>,;)\]]+", re.IGNORECASE)

EXTRACTOR_VERSION = "deterministic-regex-v1"


def sha256_of_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LocalDocumentService:
    """Ingest a file the user pointed at, and describe what it is.

    Deliberately not a `LiteratureProvider`. A literature provider answers
    "what works exist"; this answers "what is in this file". Forcing them into
    one interface would make a local PDF look like a search result.
    """

    def __init__(self, parser: DocumentParser) -> None:
        self._parser = parser

    # -- ingestion ---------------------------------------------------------

    def ingest(self, path) -> ParsedDocument:
        target = pathlib.Path(path)

        if not target.exists():
            raise DocumentError(f"no such file: {target}",
                                kind=DocumentErrorKind.NOT_FOUND, path=str(target))
        if not target.is_file():
            raise DocumentError(f"not a regular file: {target}",
                                kind=DocumentErrorKind.NOT_A_REGULAR_FILE,
                                path=str(target))

        size = target.stat().st_size
        if size == 0:
            raise DocumentError(f"file is empty: {target}",
                                kind=DocumentErrorKind.EMPTY, path=str(target))
        if size > max_document_bytes():
            raise DocumentError(
                f"file is {size} bytes, over the {max_document_bytes()} byte limit: "
                f"{target}. This is a resource-protection limit, not a PDF limit.",
                kind=DocumentErrorKind.TOO_LARGE, path=str(target))

        try:
            with open(target, "rb") as handle:
                head = handle.read(8)
        except OSError as exc:
            raise DocumentError(f"could not read {target}: {exc}",
                                kind=DocumentErrorKind.UNREADABLE,
                                path=str(target)) from exc

        # the extension is a claim by whoever named the file; the magic bytes
        # are the first thing we can actually check
        if not looks_like_pdf(head):
            raise DocumentError(
                f"not a PDF (missing %PDF- signature): {target}. A .pdf extension "
                f"is not evidence that the file is one.",
                kind=DocumentErrorKind.NOT_A_PDF, path=str(target))

        size_problem = classify_size(size)
        if size_problem is not None:
            raise DocumentError(
                f"file is {size} bytes, which cannot be a usable PDF: {target}",
                kind=size_problem, path=str(target))

        digest = sha256_of_file(target)
        parsed = self._parser.parse(
            target, digest, media_type_for(head),
            filename=target.name, size_bytes=size,
        )
        parsed = parsed.model_copy(update={
            "sections": detect_sections(parsed),
        })
        return parsed

    # -- identity ----------------------------------------------------------

    @staticmethod
    def identity_of(sha256: str, media_type=DocumentMediaType.PDF) -> str:
        return LocalDocument.identity(sha256, media_type)

    @staticmethod
    def same_document(a: LocalDocument, b: LocalDocument) -> bool:
        """Content identity: same bytes and same media type.

        Not the path, not the filename, not when we read it. A paper copied
        between folders is one document, and two files that share a name are
        not.
        """
        return a.document_id == b.document_id

    # -- DOI candidates ----------------------------------------------------

    def extract_identifier_candidates(
        self, parsed: ParsedDocument, limit: int = 20
    ) -> list[DocumentIdentifierCandidate]:
        """DOIs the file *claims* to be about. Candidates, not resolutions.

        No network, no registry, no disambiguation. Several DOIs in a paper --
        its own, its references' -- is normal, and picking the "right" one from
        the text is not something regex can do.
        """
        from crea.core.ids import normalize_doi

        out: list[DocumentIdentifierCandidate] = []
        seen: set[str] = set()
        for page in parsed.pages:
            for match in _DOI.finditer(page.text):
                candidate = normalize_doi(match.group(0).rstrip(".,;"))
                if not candidate or candidate in seen:
                    continue
                seen.add(candidate)
                start = max(0, match.start() - 60)
                out.append(DocumentIdentifierCandidate(
                    value=candidate,
                    page_number=page.page_number,
                    context=page.text[start:match.end() + 40].replace("\n", " "),
                    extractor=EXTRACTOR_VERSION,
                ))
                if len(out) >= limit:
                    return out
        return out

    # -- association -------------------------------------------------------

    @staticmethod
    def link(
        paper_id: str,
        document_id: str,
        link_basis: LinkBasis = LinkBasis.EXPLICIT_USER,
        doi: str | None = None,
        note: str = "",
    ) -> PaperDocumentLink:
        """Associate a known work with a local file.

        A DOI *candidate* found in the text is a `DOI_MATCH` basis, which is
        local evidence. It is not recorded as verified anywhere, because nothing
        has checked it against anything.
        """
        return PaperDocumentLink(
            paper_id=paper_id, document_id=document_id,
            link_basis=link_basis, doi=doi, note=note,
        )

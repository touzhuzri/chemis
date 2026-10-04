"""The only module in the project that imports pypdf.

An adapter, not a data model. Everything above it sees `ParsedDocument`,
`DocumentPage` and `DocumentSection`; swapping this file for another PDF
library must not change a single line above.

pypdf is BSD-3-Clause, verified against its upstream LICENSE. PyMuPDF remains
excluded: AGPL-3.0.
"""

from __future__ import annotations

import hashlib
import pathlib

from crea.core.errors import DocumentError
from crea.core.models.document import (
    DocumentErrorKind,
    DocumentMediaType,
    DocumentPage,
    LocalDocument,
    ParsedDocument,
)
from crea.core.policy.document import PDF_SIGNATURE, looks_like_pdf
from crea.core.provenance import Provenance, utc_now_iso


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PypdfDocumentParser:
    """Page-level text extraction via pypdf."""

    name = "pypdf"

    def __init__(self) -> None:
        import pypdf

        self._pypdf = pypdf
        self.version = pypdf.__version__

    def parse(
        self,
        path,
        sha256: str,
        media_type: DocumentMediaType,
        filename: str = "",
        size_bytes: int = 0,
    ) -> ParsedDocument:
        pypdf = self._pypdf
        path = pathlib.Path(path)
        document = LocalDocument.build(
            filename=filename or path.name,
            media_type=media_type,
            sha256=sha256,
            size_bytes=size_bytes,
            page_count=0,
            source_path=str(path),
            ingested_at=utc_now_iso(),
            provenance=Provenance(
                tool="crea.sources.pdf",
                tool_version=self.version,
                source="user_provided",
                raw_payload_hash=sha256,
            ),
        )
        warnings: list[str] = []
        try:
            reader = pypdf.PdfReader(str(path))
            if getattr(reader, "is_encrypted", False):
                try:
                    reader.decrypt("")
                except Exception as exc:  # noqa: BLE001
                    raise DocumentError(
                        f"PDF is encrypted and cannot be read: {path}",
                        kind=DocumentErrorKind.UNREADABLE, path=str(path),
                    ) from exc
            raw_pages = list(reader.pages)
        except DocumentError:
            raise
        except Exception as exc:  # noqa: BLE001 - any parser failure is one thing
            raise DocumentError(
                f"could not read PDF: {path} ({type(exc).__name__}: {exc})",
                kind=DocumentErrorKind.PARSE_FAILED, path=str(path),
            ) from exc

        pages: list[DocumentPage] = []
        for index, page in enumerate(raw_pages):
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001 - one bad page, not a bad file
                text = ""
                warnings.append(
                    f"page {index + 1}: text extraction failed ({type(exc).__name__}); "
                    f"the page is present but empty"
                )
            # pypdf's output is line-broken and whitespace-ragged. That is a
            # layout decision, not the document's characters, and it is recorded
            # on every page rather than quietly presented as the file's text.
            pages.append(DocumentPage(
                page_number=index + 1,          # 1-based, as a reader says it
                text=text,
                char_count=len(text),
                parser_normalized=True,
            ))

        document = document.model_copy(update={"page_count": len(pages)})
        if not pages:
            warnings.append("PDF contains no pages")

        return ParsedDocument(
            document=document,
            pages=pages,
            full_text="\n".join(p.text for p in pages),
            parser=self.name,
            parser_version=self.version,
            warnings=warnings,
        )


def read_head(path, size: int = 8) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(size)
    except OSError as exc:
        raise DocumentError(
            f"could not read the first bytes of {path}: {exc}",
            kind=DocumentErrorKind.UNREADABLE, path=str(path),
        ) from exc


__all__ = [
    "PDF_SIGNATURE",
    "PypdfDocumentParser",
    "looks_like_pdf",
    "read_head",
    "sha256_of",
]

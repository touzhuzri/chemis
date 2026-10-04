"""File-handling policy for local documents.

A resource-protection limit, not a PDF standard limit. The PDF specification
has no maximum file size; this number exists so that pointing the tool at the
wrong file -- a disk image, a video, a directory -- costs a clear error rather
than an afternoon of memory pressure.
"""

from __future__ import annotations

from crea.core.models.document import DocumentErrorKind, DocumentMediaType

#: 100 MiB. Comfortably above any single paper, comfortably below anything that
#: would hurt. Override with CREA_MAX_LOCAL_DOCUMENT_BYTES.
DEFAULT_MAX_DOCUMENT_BYTES = 100 * 1024 * 1024

#: The five bytes every PDF begins with. Checked before handing the file to a
#: parser: a `.pdf` extension is a claim by whoever named the file, and this is
#: the cheapest way to find out they were wrong.
PDF_SIGNATURE = b"%PDF-"

#: A PDF shorter than this is a stub or a truncated download. We do not try to
#: salvage it; we say so.
MIN_PDF_BYTES = 32


def max_document_bytes() -> int:
    from crea.core.config import settings

    configured = getattr(settings, "max_local_document_bytes", None)
    if configured:
        return int(configured)
    return DEFAULT_MAX_DOCUMENT_BYTES


def looks_like_pdf(head: bytes) -> bool:
    return head[:len(PDF_SIGNATURE)] == PDF_SIGNATURE


def classify_size(size: int) -> DocumentErrorKind | None:
    if size == 0:
        return DocumentErrorKind.EMPTY
    if size < MIN_PDF_BYTES:
        return DocumentErrorKind.EMPTY
    if size > max_document_bytes():
        return DocumentErrorKind.TOO_LARGE
    return None


def media_type_for(head: bytes) -> DocumentMediaType:
    return (DocumentMediaType.PDF if looks_like_pdf(head)
            else DocumentMediaType.UNKNOWN)

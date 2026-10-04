"""A minimal PDF writer, for the offline demo and for anyone who wants a
synthetic document without a PDF-authoring dependency.

Deliberately duplicated from the test suite's copy rather than imported from it:
`src/` must never reach into `tests/`. It is a writer, not a parser -- the
parsing adapter is `sources/pdf.py`.
"""

from __future__ import annotations

PAGE_WIDTH = 612
PAGE_HEIGHT = 792


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def make_demo_pdf(pages: list[list[str]]) -> bytes:
    """A valid, single-font, text-extractable PDF: one page per list."""
    if not pages:
        raise ValueError("a PDF needs at least one page")

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog_no = add(b"")
    pages_no = add(b"")
    font_no = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    page_nos: list[int] = []
    for lines in pages:
        parts = ["BT", "/F1 11 Tf", "14 TL", f"1 0 0 1 56 740 Tm"]
        for line in lines:
            parts.append(f"({_escape(line)}) Tj")
            parts.append("T*")
        parts.append("ET")
        stream = "\n".join(parts).encode("latin-1", "replace")
        content_no = add(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream + b"\nendstream"
        )
        page_nos.append(add(
            f"<< /Type /Page /Parent {pages_no} 0 R /MediaBox "
            f"[0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] /Resources "
            f"<< /Font << /F1 {font_no} 0 R >> >> /Contents {content_no} 0 R >>"
            .encode("latin-1")
        ))

    kids = " ".join(f"{n} 0 R" for n in page_nos)
    objects[catalog_no - 1] = f"<< /Type /Catalog /Pages {pages_no} 0 R >>".encode()
    objects[pages_no - 1] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_nos)} >>".encode()
    )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_no} 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)

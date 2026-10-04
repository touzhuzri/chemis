"""Synthetic PDF generation for tests.

Written by hand rather than with a PDF *writing* library, so the repository
gains no dependency for the sake of fixtures, and so it is obvious that no real
paper was copied into this project. Every document produced here is
`origin = SYNTHETIC`.

These PDFs are deliberately plain: Helvetica, one text column, a real content
stream. They exercise page numbering, text extraction and heading lines. They
do not and cannot exercise two-column layout, ligatures, embedded images or
anything else a real publisher's PDF does -- see `docs/PHASE2_PDF_POLICY.md`.
"""

from __future__ import annotations

PAGE_WIDTH = 612
PAGE_HEIGHT = 792


def _escape(text: str) -> str:
    return (text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)"))


def _content_stream(lines: list[str], start_y: int = 740, leading: int = 16) -> bytes:
    parts = ["BT", "/F1 11 Tf", f"14 TL", f"1 0 0 1 56 {start_y} Tm"]
    for line in lines:
        parts.append(f"({_escape(line)}) Tj")
        parts.append("T*")
    parts.append("ET")
    return "\n".join(parts).encode("latin-1", "replace")


def make_pdf(pages: list[list[str]]) -> bytes:
    """A valid, single-font, text-extractable PDF with one page per list."""
    if not pages:
        raise ValueError("a PDF needs at least one page")

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)      # 1-based object number

    catalog_no = add(b"")        # 1, patched below once pages are known
    pages_no = add(b"")          # 2
    font_no = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    page_nos: list[int] = []
    for lines in pages:
        stream = _content_stream(lines)
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


# --- the fixtures the phase brief asked for, all synthetic ------------------

SINGLE_PAGE = [[
    "Synthesis of a Test Compound",
    "Abstract",
    "We report a procedure for a model substrate.",
    "Experimental",
    "A solution of the substrate was treated with reagent A.",
    "The reaction was stirred at 80 C for 12 hours.",
    "Yield 82%.",
    "doi: 10.0000/synthetic.test.0001",
]]

MULTI_PAGE = [
    ["Introduction", "This work examines a model reaction."],
    ["Experimental", "General procedure for the synthesis."],
    ["Experimental", "Specific procedure for compound 1.",
     "The mixture was heated at 110 C for 6 h."],
    ["References", "[1] A. Author, J. Chem. 2020, 1, 1."],
]

#: Deliberately awkward: ragged spacing, a numbered heading, a repeated
#: header, and a heading-like phrase that must NOT be read as methods.
AWKWARD = [[
    "3.    Experimental",
    "     General    procedure.",
    "",
    "Experimental results are discussed below.",
    "3 Experimental",
]]

NO_EXPERIMENTAL = [
    ["Title of a Paper Without Methods",
     "Introduction", "Some background."],
    ["Results", "We obtained the product in good yield."],
]


def write_pdf(directory, name: str, pages: list[list[str]]) -> str:
    path = directory / name
    path.write_bytes(make_pdf(pages))
    return str(path)


#: The worked example from the phase brief: a complete procedure with a
#: catalyst, a base, a solvent, a temperature, a time and an isolated yield --
#: and, deliberately, no pressure, no atmosphere and no workup.
EXPERIMENTAL = [[
    "Experimental",
    "Phenylacetylene (1.0 mmol),",
    "CuI (10 mol%),",
    "DBU (2.0 equiv)",
    "were dissolved in DMF (5 mL).",
    "The mixture was stirred at 80 C",
    "for 12 h.",
    "The product was isolated in 82% yield.",
]]

#: Two procedures side by side, for the comparison case. No ranking, only
#: "these are the conditions and they differ here".
TWO_PROCEDURES = [[
    "Experimental",
    "Procedure A",
    "Phenylacetylene (1.0 mmol) and CuI (10 mol%) in DMF (5 mL).",
    "The mixture was stirred at 80 C for 12 h.",
    "The product was isolated in 82% yield.",
    "",
    "Procedure B",
    "Phenylacetylene (1.0 mmol) and Pd(PPh3)4 (5 mol%) in toluene (5 mL).",
    "The mixture was stirred at 60 C for 24 h.",
    "The product was isolated in 76% yield.",
]]

#: A bare sentence: temperature and time, nothing else. Used to prove the
#: extractor reports what is absent instead of supplying a usual value.
TEMPERATURE_ONLY = [[
    "Experimental",
    "The reaction was stirred at 80 C for 12 h.",
]]

#: Two passages that disagree about the temperature.
CONFLICTING = [
    ["Experimental", "The reaction was stirred at 80 C for 12 h."],
    ["Experimental", "The reaction was stirred at 90 C for 12 h."],
]

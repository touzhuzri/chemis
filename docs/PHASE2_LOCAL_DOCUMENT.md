# Local documents

A user hands the tool a file that is already on this machine. We read it, and we
cite it.

```
path
 ↓  validate: exists, regular, non-empty, within size limit, %PDF- signature
 ↓  sha256
 ↓  PDF parser  (sources/pdf.py — the only module that imports pypdf)
 ↓  ParsedDocument  (pages, 1-based)
 ↓  deterministic section detection
 ↓  a query lands somewhere
 ↓  Evidence  (page, section, exact quoted text)
 ↓  Claim
```

## Responsibilities

| Layer | Module | Does |
|---|---|---|
| model | `core/models/document.py` | `LocalDocument`, `DocumentPage`, `ParsedDocument`, `DocumentSection`, `DocumentIdentifierCandidate`, `PaperDocumentLink`, `DocumentParser` protocol |
| policy | `core/policy/document.py` | size limit, PDF signature, media type |
| adapter | `sources/pdf.py` | pypdf → our own models |
| service | `services/documents.py` | validation, hashing, ingestion, DOI candidates, linking |
| service | `services/document_sections.py` | heading → section, no model |
| service | `services/document_evidence.py` | region → Evidence, and retrieval |

**`LocalDocumentService` is not a `LiteratureProvider`.** A literature provider
answers "what works exist". This answers "what is in this file". Forcing them
into one interface would make a local PDF look like a search result.

## Identity

```
document_id = content_hash("document-id-v1" | sha256 | media_type)
```

Content, and nothing else. Not the filename, not the absolute path, not the
time we read it. `paper-a.pdf` at `C:\papers\` and `downloaded-again.pdf` at
`D:\archive\` are one document; two files that happen to share a name are not.
`source_path` is kept as *provenance* — where the file was when we read it — and
is deliberately outside the identity.

## Parsing is behind a seam

`pypdf` is an adapter. The rest of the system sees `ParsedDocument`,
`DocumentPage` and `DocumentSection` and never a `pypdf.PageObject`. Six
architecture tests enforce it: only `sources/pdf.py` may import a PDF library;
neither the evidence service nor the models may; the document layer may not
reach the network; and `chem/` may not know the word "pdf".

Swapping parsers must change one file and nothing above it.

## Page numbers are 1-based

A reader says "page 7". Python says `pages[6]`. The model stores 1.

## Sections

Deterministic, literal, table-driven. A heading is recognised when its
normalised form *equals* a known heading — after unicode folding, lower-casing,
numbering stripping (`3 Experimental`, `IV. RESULTS`) and punctuation
trimming.

The conservative choice is deliberate. **"Experimental results" is not an
experimental section.** A detector that greps for the word would put a results
discussion under `EXPERIMENTAL`, and that section's contents are exactly what
becomes `EXPERIMENTAL` evidence. A false positive here silently reclassifies
results as methods, so the matcher is an exact table and everything else is
`UNKNOWN`.

Two things the detector gets right that a page-only model would not:

- **A running header is not a section.** "Experimental" at the top of pages 2
  and 3 is one section spanning both, not two.
- **Several sections can share a page.** A short communication puts its
  abstract and its methods on page 1. Page bounds alone would attribute the
  method to whichever section was found first, so sections also carry a
  character range and a passage is attributed by **where it falls**.

`EXPERIMENTAL` and `METHODS` are one enum member; the exact wording found is
kept on `detected_heading` for a reader who wants to disagree with us.

## Evidence from a document

A passage becomes Evidence when a **query lands on it** — never by shredding
the file. A 30-page paper does not become 30,000 citations.

```python
document_region_to_evidence(document, quote, page_start=7, section="Experimental",
                            section_kind="experimental_or_methods")
```

`raw_quote` must be text that came out of the file. There is no path that can
take a model's repr: a citation to `str(page)` proves nothing a reader could
check.

### Strength: `USER_PROVIDED_EVIDENCE`, not primary

A local file is not automatically a primary document. Being handed a document
says we were *given* it; it says nothing about whether it is the publisher's
PDF, a scan, somebody's notes, or a preprint, and this project cannot tell.
Defaulting to `PRIMARY_DOCUMENT_EVIDENCE` would forge a provenance claim.

### Type follows position

`EXPERIMENTAL` when the passage is under an experimental or methods heading,
`FULLTEXT` anywhere else. That is a claim about *where the words are*, not
about whether they are right.

### Provenance answers "where did this sentence come from"

```
source        = user_provided
source_id     = document_id
locator       = document_id, page 7, section "Experimental", char range
raw_quote     = the extracted text, unmodified
provenance    = document sha256, so the Evidence traces to exact bytes
```

`char_range` is an offset into the **extracted page text**, not into the PDF
bytes — a PDF stores layout, not characters. Every page carries
`parser_normalized = True` for the same reason.

## Retrieval

Substring, case-insensitive, then token matching; filtered by section and page.

```python
search_document(parsed, "sodium hydride", section="experimental_or_methods")
```

**No relevance score.** A number there would be the first thing a future ranking
is built on, and a number nobody defined is worse than no number: it looks like
a measurement. `EvidenceHit` carries `match_type`, `matched_count`, `position`
and `page` instead.

It locates text and does not read it. "Which catalyst did this reaction use"
may match the word "reaction" and return the surrounding passage. It does not
return a catalyst, and nothing in this stack can produce a claim the text does
not support.

## DOI candidates

`extract_identifier_candidates` returns what the file *claims* about itself.
Conservative regex, the existing `normalize_doi`, no network, no registry, no
disambiguation. A paper's own DOI and its references' DOIs are all returned —
picking "the right one" from text is not something a regex can do, and guessing
would be worse than not answering.

`PaperDocumentLink.link_basis` distinguishes `EXPLICIT_USER` from `DOI_MATCH`.
A `DOI_MATCH` is local evidence from an unverified string, and is recorded as
such. It is never described as verified, because nothing has checked it.

## What this is not

```
Local PDF  ≠  Publisher authorization
Local PDF  ≠  OpenAlex permission
Local PDF  ≠  Crossref TDM permission
Local PDF  ≠  an open-access copy
```

Being handed a file means the system has a local input to process. It carries
no further implication about the file's copyright status, and none is inferred.

Nothing in this phase downloads a file, resolves a DOI to a publisher, opens a
browser, or authenticates anywhere.

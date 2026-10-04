# Phase 2 — Chemical Literature Retrieval: design (revision 2)

## Status

Steps 1–6 are implemented and tested. Real provider HTTP clients are
intentionally blocked until official API documentation has been verified.

| # | Step | State |
|---|---|---|
| 1 | Literature data models | [x] `core/models/literature.py`, `core/models/dedup.py` |
| 2 | Access policy | [x] `core/policy/access.py` |
| 3 | Dedup state machine | [x] `services/dedup.py` |
| 4 | Provider transport seam | [x] `sources/base.py` — interface only, no endpoint |
| 5 | Offline integration | [x] `tests/integration_offline/`, `tests/fakes/` |
| 6 | Network guard | [x] `tests/architecture/test_no_network.py` + session socket guard |
| 7 | Official API verification | [x] field structure for all three; **prose policy still UNVERIFIED** — see `PHASE2_PROVIDER_CONTRACT.md` |
| 8 | Real provider implementation | [x] OpenAlex (2.1A), Crossref (2.2A), Unpaywall (2.3A) -- all metadata-only behind the Transport seam. Unpaywall's *policy* stays UNVERIFIED and no code assumes it |
| 9 | LiteratureService | [x] deterministic orchestration -- [`PHASE2_LITERATURE_SERVICE.md`](PHASE2_LITERATURE_SERVICE.md). No ranking, no LLM, no agent |
| 12 | Local documents | [x] ingestion, page-level parsing, deterministic section detection, document evidence + retrieval -- [`PHASE2_LOCAL_DOCUMENT.md`](PHASE2_LOCAL_DOCUMENT.md), [`PHASE2_PDF_POLICY.md`](PHASE2_PDF_POLICY.md). User-supplied files only; nothing is fetched |
| 11 | Evidence chain | [x] Evidence / Claim / Locator models, deterministic IDs, Paper->Evidence, Abstract->Evidence, referential validation -- [`PHASE2_EVIDENCE_AUDIT.md`](PHASE2_EVIDENCE_AUDIT.md), [`PHASE2_EVIDENCE_POLICY.md`](PHASE2_EVIDENCE_POLICY.md). No PDF, no LLM |
| 10 | Live integration tests | [x] three opt-in smoke suites behind `CREA_LIVE_TESTS=1`; all **skip** by default and all three live paths are NOT YET VERIFIED (`httpx` not installed) |

**Hardening (structured OA status, nullable title, dated-policy naming),
cross-provider merge, access reconciliation and the LiteratureService are all
done.**

**Cross-provider merge and access reconciliation are done** -- see
[`PHASE2_INTEGRATION.md`](PHASE2_INTEGRATION.md). No `LiteratureService` and no
full-text retrieval exist.

**Step 7 is only partly complete.** Provider *field structure* was verified from
the providers' own live responses on 2026-09-30. The prose documentation of
OpenAlex and Unpaywall could not be retrieved — both sites are client-rendered
applications that return an identical shell for every path — so their rate
limits, polite-pool rules, licences and definitions remain **UNVERIFIED**.
Details, and the three blockers that were found, are in
[`PHASE2_PROVIDER_CONTRACT.md`](PHASE2_PROVIDER_CONTRACT.md).

No provider, downloader, PDF parser or LLM extraction is implemented. Phase 1's
Chemical Core is untouched.

Revision 2 applies seven corrections: the abstract-source distinction, a
non-automatic fuzzy dedup state machine, hard offline-by-default testing,
official-documentation verification as a precondition, Unpaywall's semantics,
a split of OA status from access status, and a reaffirmation that HTTP clients
wait for the design to be approved.

---

## 0. Precondition: no HTTP client until the docs are checked

**Partly discharged on 2026-09-30.** Field structure for all three providers was
verified against their own live responses and is recorded, cell by cell, in
[`PHASE2_PROVIDER_CONTRACT.md`](PHASE2_PROVIDER_CONTRACT.md). What remains
UNVERIFIED there is prose-level policy: rate limits, polite-pool rules, data
licences, and definitions.

The rule for this phase is unchanged:

> No `openalex.py` / `crossref.py` / `unpaywall.py` HTTP client is written until
> every line of the contract sheet for that provider reads VERIFIED, and the
> confirmation is recorded in the fixture's `.meta.json`.

Three blockers were found during verification and must be fixed before any
client exists — most importantly that `OAStatus` does not contain `diamond`,
which OpenAlex returns in the wild, and that the OpenAlex branch of the
synthetic fixture parser reads Unpaywall's field names. See the contract sheet.

---

## 1. Literature data model (revised)

### 1.1 Abstracts are typed, and a reconstruction is never a publisher original

```python
class AbstractSource(str, Enum):
    CROSSREF = "crossref"                            # JATS <abstract> via Crossref
    OPENALEX_RECONSTRUCTED = "openalex_reconstructed"  # from abstract_inverted_index
    PUBLISHER = "publisher"                          # publisher's own metadata
    USER_PROVIDED = "user_provided"                  # pasted in by the user
    UNKNOWN = "unknown"


class AbstractRecord(BaseModel):
    text: str
    source: AbstractSource
    retrieved_at: str
    raw_excerpt: str | None = None      # the leading ~300 chars, for human checking

    @property
    def is_reconstructed(self) -> bool:
        return self.source is AbstractSource.OPENALEX_RECONSTRUCTED
```

A reconstructed OpenAlex abstract is assembled from a positional inverted index
and is therefore **a reconstruction, not a quote**. It is still perfectly good
for search, ranking and screening, and the pipeline may use it for all three.
What it may not do is be presented downstream as publisher text.

Both abstracts are kept when both exist. Preference order is
`PUBLISHER > CROSSREF > OPENALEX_RECONSTRUCTED > USER_PROVIDED > UNKNOWN` — the
non-reconstructed sources win, which is a change from revision 1 where OpenAlex
won. If the two texts differ materially, the disagreement goes into
`Paper.merge_conflicts` rather than being silently resolved.

The Evidence layer reads `is_reconstructed` and must know. A rule, not a
convention:

```python
def abstract_admissible_as_evidence(rec: AbstractRecord) -> bool:
    """Reconstructed abstracts may be screened on; they are not citable.

    Every extractable `Evidence` must ultimately carry a `raw_quote` that is
    findable verbatim in a *document*. An abstract is not a document, and a
    reconstruction of one is further removed still.
    """
    return not rec.is_reconstructed
```

### 1.2 Paper

```python
class LicenseInfo(BaseModel):
    spdx_id: str | None = None
    name: str = "UNKNOWN"
    url: str | None = None
    content_version: str = "UNKNOWN"
    applies_to: Literal["published_article", "oa_copy", "unknown"] = "unknown"
    is_known: bool = False
    stated_by: list[str] = Field(default_factory=list)
```

A Crossref licence and an Unpaywall licence are licences of *different things*
and are both retained, distinguished by `applies_to`. They are never merged
into one field.

```python
class Paper(BaseModel):
    paper_id: str
    title: str
    authors: list[Author] = Field(default_factory=list)
    journal: str | None = None
    year: int | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None

    abstracts: list[AbstractRecord] = Field(default_factory=list)
    identifiers: Identifiers = Field(default_factory=Identifiers)
    access: AccessAssessment = Field(default_factory=AccessAssessment)
    url: str | None = None

    sources: list[str] = Field(default_factory=list)
    source_ids: dict[str, str] = Field(default_factory=dict)
    retrieved_at: str
    raw_hashes: dict[str, str] = Field(default_factory=dict)
    merge_conflicts: list[str] = Field(default_factory=list)

    @property
    def abstract(self) -> AbstractRecord | None:   # by preference order
        ...
```

`title`, `authors` and `journal` are populated by a merge policy in the service,
never by a provider directly seeing another provider's output.

## 2. AccessAssessment — OA status, access status and permission are three things

Revision 1 let one eleven-value enum carry all of the meaning. That conflated
"the publisher says this is open access" with "we are permitted to fetch it",
which is precisely the conflation that produces a system that downloads
paywalled articles.

### 2.1 Three dimensions

```python
class OAStatus(str, Enum):
    """What the OA ecosystem says about the work. A property of the work."""
    UNKNOWN = "unknown"
    GOLD = "gold"          # published in a fully OA journal
    GREEN = "green"        # repository / preprint copy
    HYBRID = "hybrid"      # OA under an open licence, journal otherwise subscription
    BRONZE = "bronze"      # free to read on the publisher site, no open licence
    CLOSED = "closed"      # no OA copy known


class AccessStatus(str, Enum):
    """What is reachable from here, now, by us. A property of the situation."""
    NOT_CHECKED = "not_checked"
    AVAILABLE = "available"        # a legal full text exists and retrieval is permitted
    SUBSCRIPTION = "subscription"  # behind a licence a human may hold. We do nothing.
    UNAVAILABLE = "unavailable"    # checked; no legal route from here
    UNKNOWN = "unknown"            # check failed or the field was missing. Fail closed.


class FulltextPermission(str, Enum):
    """Whether we may fetch, keep or redistribute. Never inferred from a URL."""
    UNKNOWN = "unknown"
    MAY_RETRIEVE = "may_retrieve"          # licence permits retrieval
    OA_NO_DOWNLOAD = "oa_no_download"      # free to read; licence forbids automated retrieval
    SUBSCRIPTION_ONLY = "subscription_only"
    NOT_PERMITTED = "not_permitted"
```

### 2.2 The assessment record

```python
class AccessAssessment(BaseModel):
    oa_status: OAStatus = OAStatus.UNKNOWN
    access_status: AccessStatus = AccessStatus.NOT_CHECKED
    permission: FulltextPermission = FulltextPermission.UNKNOWN

    source: str = ""                       # "unpaywall" | "crossref" | "local_document"
    source_url: str | None = None          # the API endpoint that produced this
    license: LicenseInfo = Field(default_factory=LicenseInfo)
    fulltext_url: str | None = None
    url_type: Literal["landing_page", "pdf", "unknown"] = "unknown"
    oa_version: str = "UNKNOWN"

    checked_at: str | None = None
    notes: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)   # which fields came from where
```

### 2.3 The rule that matters

> A non-null `fulltext_url` **never** implies `permission == MAY_RETRIEVE`.

Unpaywall returns the *location* of an OA copy. A location is not an
authorisation. Bronze OA in particular is free-to-read with no open licence and
is the case where a system that conflated the two would quietly start
downloading. The gate consults `permission`, never the URL.

Default mapping, applied by policy in the service and never by a provider:

| oa_status | license known & permissive | permission |
|---|---|---|
| GOLD / GREEN / HYBRID | yes | `MAY_RETRIEVE` |
| GOLD / GREEN / HYBRID | no | `UNKNOWN` |
| BRONZE | any | `OA_NO_DOWNLOAD` |
| CLOSED | any | `NOT_PERMITTED` |
| UNKNOWN | any | `UNKNOWN` |

`UNSUPPORTED` is never the default. Unknown licence means unknown permission,
and unknown permission means we do not fetch.

### 2.4 Unpaywall's role, stated precisely

`Unpaywall = OA availability / OA location metadata provider`.

It is not an access authority and not a licence authority for the published
article. On its own it never authorises downloading, keeping or redistributing
full text. The service reports what Unpaywall said; deciding what may be done
with that is policy, and policy lives in `evidence.policy` (a later phase).

### 2.5 `FULLTEXT_UNAVAILABLE`

Dropped as a status value. It was a *retrieval outcome*, not a property of the
work, and mixing it into the status enum is what made revision 1's eleven-value
version unusable. Retrieval outcomes are now recorded on the retrieval attempt:

```python
class RetrievalOutcome(BaseModel):
    paper_id: str
    attempted_at: str
    outcome: Literal["success", "no_url", "permission_denied",
                     "not_found", "network_error", "not_attempted"]
    http_status: int | None = None
    note: str = ""
```

`permission_denied` is the value that the gate is expected to produce, and it
is produced *without* a network call.

## 3. DOI deduplication as an explicit state machine

Revision 1 merged on `0.92 + 1 year` and called the rest "audit". Revision 2
makes the decision a state transition with named evidence and a recorded
outcome. Default on insufficient evidence is `KEEP_SEPARATE`.

```
                    ┌──────────────┐
     record ────────►  NORMALISED  │  doi / title / year / authors / journal
                    └──────┬───────┘
                           │
                 doi present?
                   ┌───────┴────────┐
                  yes              no
                   │                 │
            ┌──────▼──────┐   ┌──────▼───────────────┐
            │ DOI_EXACT   │   │ TITLE_YEAR_EXACT     │  pass 2
            │  → MERGED   │   │  → MERGED / SEPARATE │
            └─────────────┘   └──────┬───────────────┘
                                     │ leftovers only
                              ┌──────▼────────────┐
                              │ TITLE_FUZZY_SCREEN │  pass 3
                              └──────┬────────────┘
                       sim >= 0.92 AND |Δyear| <= 1 ?
                          ┌────────┴────────┐
                         no                 yes
                          │                  │
                 ┌────────▼───────┐  ┌───────▼──────────────┐
                 │ KEEP_SEPARATE  │  │ DUPLICATE_CANDIDATE  │
                 └────────────────┘  └───────┬──────────────┘
                                            │ gather evidence
                              title_similarity, year_difference,
                              journal_match, authors_overlap
                                            │
                              all gates hold?  ──no──► KEEP_SEPARATE
                                            │ yes
                                       ┌────▼─────┐
                                       │  MERGE   │
                                       └──────────┘
```

### 3.1 Normalisation (unchanged, applied at ingest)

DOI: lowercase; strip whitespace; strip `doi:`, `https://doi.org/`,
`http://dx.doi.org/`, `doi.org/` prefixes; strip trailing `.,;)`; drop
anything after `?` or `#`; collapse internal whitespace.

Title: lowercase; strip diacritics; punctuation to single spaces; drop leading
articles; drop trailing `[...]` / `(...)` markers; collapse whitespace.

### 3.2 Pass 3 merge gates

All four must hold. Failing any one is `KEEP_SEPARATE`.

| # | Gate | Note |
|---|---|---|
| 1 | `normalized_title_similarity >= 0.92` | necessary, never sufficient |
| 2 | `year_difference <= 1` | necessary |
| 3 | `journal_match` is true, **or** at least one journal is unknown | additional independent evidence — a shared title across two different journals at the same year is usually two papers |
| 4 | `authors_overlap > 0.0` | **auxiliary corroboration only**; a record with no author list is not blocked by this gate, and a shared author is never on its own sufficient |

Authors are explicitly not a merge condition. An author list shared across a
paper series says nothing about identity, so it is recorded as supporting
evidence and used only to corroborate gates 1–3.

### 3.3 Audit record

Every pass-3 screening, whether merged or not, produces one of these.

```python
class DedupDecision(BaseModel):
    decision_id: str
    record_a: str
    record_b: str
    pass_name: Literal["doi_exact", "title_year_exact", "title_fuzzy"]
    normalized_title_similarity: float | None = None
    year_difference: int | None = None
    journal_match: bool | None = None
    authors_overlap: float | None = None        # auxiliary only
    decision: Literal["MERGE", "KEEP_SEPARATE"]
    reason: str = ""
    threshold_version: str = "dedup-v1"          # thresholds are versioned so
                                                # past decisions stay auditable
                                                # after a threshold change
```

Pass 1 and 2 decisions are recorded too, with `normalized_title_similarity` left
`None` where it was not computed.

## 4. Provider interface

```python
class LiteratureProvider(Protocol):
    name: str
    capabilities: frozenset[Capability]
    requires_key: bool
    rate_limit: RateLimit

    def status(self) -> ProviderStatus: ...
    def search(self, q: LiteratureQuery) -> ProviderResult: ...
    def get_metadata(self, ref: PaperRef) -> PaperPatch | None: ...
    def get_access_assessment(self, doi: str) -> AccessAssessment | None: ...
    def get_fulltext(self, paper: Paper) -> RetrievalOutcome:
        """NOT IMPLEMENTED IN THIS PHASE.

        Reserved so the shape is fixed. When implemented it must consult
        `AccessAssessment.permission` first and return
        `outcome="permission_denied"` without any network call when retrieval
        is not permitted.
        """
```

### 4.1 The separation that matters

A provider **reports**; it does not **decide**.

- A provider maps one upstream payload into our models and stamps provenance.
- A provider never sees another provider's output.
- Merge precedence, dedup, permission inference and ranking live in the service
  and in `core` policy functions.

This is what makes providers swappable, fixture-testable, and impossible to use
as a place where policy hides.

### 4.2 Rules encoded in the base class

1. **Missing credential → `Availability.MISSING_KEY`**, not an exception.
   Unpaywall requires an email; OpenAlex and Crossref do not. The service must
   degrade to discovery-plus-metadata and say plainly that access was not
   determined.
2. **No unattributed record.** There is no code path through a provider that
   yields a `Paper` without `sources`, `source_ids` and `retrieved_at`.
3. **`get_access_assessment` reports; it does not infer `permission`.** A
   provider may set `oa_status` and `fulltext_url`. It may not set
   `permission` to anything but `UNKNOWN`; that field belongs to policy.
4. **`get_fulltext` is never implemented in this phase.**

## 5. Offline fixture architecture

### 5.1 Default suite is offline, structurally

```
tests/
├── fixtures/
│   └── providers/
│       ├── openalex/
│       │   ├── search_diyne.json          # upstream payload, unmodified shape
│       │   ├── search_diyne.meta.json     # endpoint, params, recorded_at, api_version
│       │   └── work_by_doi.json
│       ├── crossref/
│       │   └── doi_10_1002_xxx.json
│       └── unpaywall/
│           ├── is_oa_green.json
│           ├── is_oa_closed.json
│           └── is_oa_missing_fields.json
├── fakes/
│   ├── transport.py        # FakeTransport: request(method, url, params) -> Response
│   └── providers.py        # FixtureProvider: serves the above, counts calls
└── integration_offline/    # service-level tests, no network possible
```

### 5.2 Fixtures are upstream payloads, not our models

A fixture is the provider's JSON **as returned**, not a pre-mapped `Paper`.
Parsing raw → model is part of what is under test, so an upstream schema change
surfaces as a parse failure rather than as a silently different value. Each
fixture carries a `.meta.json` recording the endpoint, parameters, the date it
was recorded and the API version it came from — this is also where the §7
documentation confirmation gets recorded.

Fixtures contain **metadata only**. No full text is recorded, and credentials
are redacted.

### 5.3 A transport seam rather than a mocking library

`FakeTransport` implements the same method signature as the httpx client:

```python
class Transport(Protocol):
    async def request(self, method: str, url: str, *, params=None, json=None) -> Response: ...
```

Tests inject a `FakeTransport`; production injects `HttpxTransport`. No mocking
library, and no way for a test to accidentally reach the network.

### 5.4 A test that enforces the offline property

Mirroring the existing layering test, one architecture test asserts that no
module under `services/` or `sources/` constructs a real `HttpxTransport` unless
an explicit live-test flag is set. The offline guarantee is a test, not a
convention.

## 6. Live integration test architecture

Opt-in, never in the default gate.

```bash
CREA_LIVE_TESTS=1 CREA_UNPAYWALL_EMAIL=you@example.org \
  pytest -m live
```

- Registered marker `live` in `pyproject.toml`.
- **Skipped, loudly, without the env var.** Never silently passed.
- **Not in the default CI gate.** A separate scheduled job, non-blocking, with
  results reported but not gating merges.
- **Weak assertions on purpose**: shape and enum membership, never exact counts
  or exact ranking, because upstream results drift and a live suite that fails
  on a drifted count trains everyone to ignore it.
- Rate limited, cached, one small query per test.
- A live test may assert that a returned `fulltext_url` is a well-formed
  `http(s)` URL. **It must never download it.**

Live coverage: OpenAlex search returns ≥1 work with a title and a year;
Crossref resolves a known DOI to matching title; Unpaywall returns a valid
`OAStatus` for a known DOI and for a known-closed DOI returns
`oa_status=closed`; the service returns papers with `access.access_status`
set and never raises on a missing credential.

## 7. API details requiring official-documentation confirmation

> **Superseded.** These tables were the pre-verification belief list. They have
> been replaced by [`PHASE2_PROVIDER_CONTRACT.md`](PHASE2_PROVIDER_CONTRACT.md),
> which records what was actually verified on 2026-09-30 and from which source.
>
> Where the two disagree, the contract sheet wins. The beliefs recorded here
> that turned out to be **wrong** include: OpenAlex's rate limit (it is now a
> credit/paid model, not a requests-per-day pool) and OpenAlex's
> `best_oa_location` field names (it is `landing_page_url` / `pdf_url`, not
> `url` / `url_for_pdf`, which are Unpaywall's). The historical tables are kept
> below for audit.

**None of the below was verified at the time this section was written.**
WebFetch was unavailable, and nothing here may be implemented from.

### 7.1 OpenAlex

| Item | Current belief | Confirmed |
|---|---|---|
| endpoint | `https://api.openalex.org/works` | ☐ |
| method | GET | ☐ |
| query params | `search`, `filter`, `per-page`, `cursor`, `mailto`, `select` | ☐ |
| `per-page` maximum | ~~200~~ **SUPERSEDED — docs say 100, live accepted 200; contradiction unresolved, see contract §7.1** | ✗ |
| pagination model | cursor-based | ☐ |
| polite pool | `mailto` parameter | ☐ |
| rate limit | unknown | ☐ |
| title / `publication_year` fields | as named | ☐ |
| authors + affiliations shape | `authorships[]` with nested `author` | ☐ |
| abstract | `abstract_inverted_index` requiring reassembly | ☐ |
| identifier fields | `ids` containing openalex/pmid/pmcid/mag | ☐ |
| OA fields | `open_access.{is_oa,oa_status,oa_url,license}`, `best_oa_location` | ☐ |
| licence semantics | mapping of `oa_status` to gold/green/hybrid/bronze/closed | ☐ |
| email requirement | none, but `mailto` recommended | ☐ |
| terms / rate policy | unknown | ☐ |

### 7.2 Crossref

| Item | Current belief | Confirmed |
|---|---|---|
| endpoint | `https://api.crossref.org/works` | ☐ |
| method | GET | ☐ |
| query params | `query`, `query.bibliographic`, `rows`, `mailto`, `filter` | ☐ |
| `rows` maximum | 1000 | ☐ |
| rate limit | unknown; polite pool via `mailto` | ☐ |
| DOI field | `DOI` | ☐ |
| title / container | `title[]`, `container-title[]` (arrays) | ☐ |
| authors | `author[]` with `given`/`family` | ☐ |
| year | `issued.date-parts[][]` | ☐ |
| abstract | JATS XML string, often absent | ☐ |
| licence | `license[]` with `URL` and `content-version` | ☐ |
| full-text links | `link[]`, often TDM and often authenticated | ☐ |
| email requirement | none; `mailto` recommended | ☐ |

### 7.3 Unpaywall

| Item | Current belief | Confirmed |
|---|---|---|
| endpoint | `https://api.unpaywall.org/v2/{doi}` | ☐ |
| method | GET | ☐ |
| query params | `email` (required) | ☐ |
| rate limit | unknown | ☐ |
| OA status field | `is_oa` and/or `oa_status`; which is authoritative in v2 | ☐ |
| OA status values | gold / hybrid / bronze / green / closed — **UNVERIFIED for Unpaywall; the official definition list is on a page that will not render here** | ✗ |
| best location | ~~`best_oa_location.{url,url_for_pdf,...}`~~ **SUPERSEDED — Unpaywall's field names; OpenAlex uses `landing_page_url`/`pdf_url` and has no `url`/`url_for_pdf`** | ✗ |
| all locations | `oa_locations[]` | ☐ |
| licence fields | SPDX string, or URL? | ☐ |
| 404 semantics | not found vs unknown | ☐ |
| email requirement | **required** | ☐ |
| terms | unknown | ☐ |

### 7.4 Open questions that change the design, not just the code

1. Does Unpaywall distinguish bronze OA from an open licence? If bronze carries
   no licence field, our mapping to `OA_NO_DOWNLOAD` is an inference and must be
   labelled as one.
2. Do Crossref `link[]` entries require authentication? If they do, they are
   not a full-text location for us and must never populate `fulltext_url`.
3. Is the OpenAlex `per-page` maximum still 200, and is the polite pool still
   `mailto`? If the pool has changed name, the rate-limit code changes with it.

## 8. What is deliberately not built in this phase

- OpenAlex / Crossref / Unpaywall HTTP clients
- any full-text downloader
- any PDF parser
- any LLM extraction
- `LocalDocumentProvider` (interface reserved; user-supplied PDFs remain a
  separate entry point from remote providers)
- the `Literature Agent`

## 9. Build order once the design is approved

1. `core/models/literature.py` — models only, no I/O
2. `tests/fakes/transport.py` + `providers.py` — the offline seam
3. `core/policy/access.py` — OA→permission mapping, in core, testable alone
4. `services/dedup.py` — normalisation + state machine + audit records
5. `sources/base.py` — `HttpProvider` scaffolding against the fake transport
6. offline integration tests for 1–5
7. **stop, and re-verify §7 against the live documentation**
8. only then: real clients, one provider at a time

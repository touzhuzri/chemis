# Phase 2.1A — OpenAlex provider

> **This provider is metadata-only. Full-text retrieval is intentionally
> outside Phase 2.1A.** OpenAlex content retrieval has a separate cost model
> ($10 per download against a $1/day free budget) and a separate rights model
> (OpenAlex states the PDFs retain their original copyright and that it grants
> no additional rights to them). Nothing in this provider downloads.

---

## 1. Scope

Implemented:

```
request → raw response → parse → Paper / PaperRef → provenance
```

Deliberately **not** the provider's job, and not present in it:

- deduplication (`services/dedup.py`)
- cross-provider merge (`services/dedup.merge_papers`)
- policy decisions (`core/policy/access.py`)
- licence inference *for the article* — the provider reports the licence string
  it was given; policy decides what it means
- ranking, and any decision about whether a paper's full text may be downloaded

The provider **reports**; policy and the service **decide**. A provider that
started deciding would be impossible to swap and impossible to test offline.

## 2. Official contract

`docs/PHASE2_PROVIDER_CONTRACT.md` §2, VERIFIED against
`help.openalex.org` markdown on 2026-09-30.

## 3. Endpoint

`https://api.openalex.org/works`

Declared in exactly one module, `sources/openalex.py`. The architecture test
fails if an endpoint appears anywhere else in `src/`.

| Operation | Request |
|---|---|
| search | `GET /works?search=…&per-page=…` |
| work lookup | `GET /works/{openalex_id}` |
| DOI lookup | `GET /works?filter=doi:{normalized}` |
| list | `GET /works?filter=…&per-page=…` |
| paginate | `GET /works?cursor=…&filter=…&per-page=…` |

DOI input is normalised before it is sent. `https://doi.org/10.x/abc`,
`doi:10.x/abc` and `10.x/abc` all become `10.x/abc`, so a resolver form never
reaches a dedup key. An unparseable DOI produces **no request at all**.

## 4. Pagination

Cursor, per the documented mechanism: `cursor=*` opens the sequence,
`meta.next_cursor` continues it, and an absent cursor ends it. An empty page
also ends it, without a further request.

`per-page` is capped at the documented **100**. A live probe was served 200
and rejected 500 with "must be between 1 and 200", which contradicts the
documentation; the documented value is what ships.

`max_pages` is a local brake on top of the documented 10,000-result basic
paging limit, because walking a large corpus is a budget decision the caller
should have to make explicitly.

## 5. Rate and budget

OpenAlex bills usage: **$1/day free with a key, $0.10/day keyless**, resetting
at midnight UTC. A **list/filter call is $0.10 and a search is $1.00** — one
search consumes the entire free daily allowance.

The provider therefore:

- **never sleeps** and **never hardcodes a rate**. A 429 is raised as
  `RateLimited` with the budget headers attached, because only the caller knows
  whether it can afford to wait.
- records `X-RateLimit-Limit`, `-Remaining`, `-Reset`, `-Cost-USD`,
  `-Limit-USD`, `-Remaining-USD` **verbatim** into `RateSnapshot`, every field
  optional, absence recorded as absence.
- computes no budget, tracks no spend, and enforces no quota. That subsystem is
  deliberately deferred to a future full-text phase, where a `CostPolicy` can
  be designed against real usage.

## 6. API key and contact address

Both optional, both from configuration, neither ever written onto a `Paper`, a
result, an error message or a log line.

- **No key** is a normal state. OpenAlex serves keyless requests; a free key
  only raises the budget. `status().usable` is therefore `True` without one.
- `mailto` is passed when the operator supplied a contact address. It is
  **not** a polite pool: no such mechanism is documented anywhere, and its
  presence changes no rate limit and no priority.

Any URL quoted in an error message goes through `redact_url()`, which
replaces the values of `api_key`, `email`, `token` and `key`.

## 7. Metadata fields handled

`id`, `doi`, `display_name` / `title`, `publication_year`, `publication_date`,
`type`, `ids`, `authorships[].author`, `primary_location.source.display_name`,
`abstract_inverted_index`, `open_access`, `best_oa_location`, `locations`,
`has_fulltext`, `has_content`, `content_urls`, `is_retracted`, `apc_list`.

## 8. OA status

Official values are exactly `diamond`, `gold`, `green`, `hybrid`, `bronze`,
`closed` — six, matching `OAStatus` plus its `UNKNOWN`. An unrecognised value
degrades to `UNKNOWN` and is **never coerced** into an existing status, because
treating an unknown value as `gold` would let a record inherit a permission
path it never qualified for.

## 9. Abstract reconstruction

`abstract_inverted_index` is reassembled from its position lists. The result is
tagged `AbstractSource.OPENALEX_RECONSTRUCTED` with `is_reconstructed=True` and
is **never** promoted to `PUBLISHER` and is **never** admissible as primary
evidence. A null index yields no abstract at all — `None` and `""` stay
distinguishable.

## 10. Fulltext boundary

`best_oa_location` uses **`landing_page_url` and `pdf_url`**. OpenAlex has no
`url` and no `url_for_pdf`; falling back to those would be borrowing
Unpaywall's schema.

A `pdf_url` still does **not** produce `MAY_RETRIEVE`. Permission is inferred
by `core/policy/access.py` from the licence alone.

## 11. `content_urls` boundary

`content_urls.pdf` and `content_urls.grobid_xml` are **recognised and recorded
in `AccessAssessment.notes`**, and never promoted to `fulltext_url`. The note
says why: OpenAlex grants no additional rights to the content, so permission is
per-work and comes from the location's licence.

The provider issues **no** request to a content URL, has **no** download method,
and a test asserts both. `content.openalex.org` is a $10 operation with its own
rights question; it is not this phase's business.

## 12. Error mapping

| Condition | Error |
|---|---|
| HTTP 400 | `InvalidRequest` |
| HTTP 401 / 403 | `AuthenticationOrAuthorizationError` |
| HTTP 429 | `RateLimited` — never an ordinary failure |
| HTTP 5xx | `UpstreamError` |
| non-JSON body | `InvalidUpstreamResponse` |
| JSON that contradicts the contract | `UpstreamSchemaError` |

No error message contains an API key.

## 13. Licensing — two different licences

**OpenAlex's data** is CC0 (`CC0-1.0`, confirmed against the SPDX licence
list), with no personal-use carve-out. That is a fact about the metadata feed
and it is **never written onto a `Paper`**. Saying the data is CC0 is not a
statement that any article or PDF is CC0.

**An article's own licence** comes from `best_oa_location.license` and is
recorded with `applies_to="oa_copy"`. OpenAlex's vocabulary maps to SPDX:

| OpenAlex | SPDX | Note |
|---|---|---|
| `public-domain` | `CC0-1.0` | the only slug OpenAlex explicitly equates to a named SPDX licence |
| `cc-by`, `cc-by-sa`, `cc-by-nd`, `cc-by-nc`, `cc-by-nc-sa`, `cc-by-nc-nd` | the same family, upper-cased | **no version** — OpenAlex does not state one and neither do we |

`public-domain` is OpenAlex's slug, not an SPDX identifier, and is never written
into the SPDX field. The raw provider value is preserved in `LicenseInfo.name`.

## 14. Test policy

**Offline (default, always runs).** `tests/integration_offline/test_openalex_provider.py`
runs against `FakeTransport` and asserts on the *requests we intend to make*
and the *records we produce*: page size, DOI normalisation, cursor sequence,
provenance, error mapping, key redaction, and the fulltext boundary. The
session-wide socket guard would fail any test that tried to leave the machine.

**Live (opt-in, never in the gate).** `tests/live/test_openalex_live.py`,
behind `CREA_ENABLE_LIVE=1` and `-m live`. It asks for `per-page=1`, never
follows a content URL, and asserts only on shape — never on what the database
currently contains, because a test that fails when OpenAlex acquires a paper
teaches everyone to ignore it. `httpx` is an optional `[live]` extra; the
default suite installs no network dependency at all.

## 15. Known limitations

1. **`per-page` docs/live contradiction is unresolved.** The documented 100 is
   used; if OpenAlex lowers it, a live test will show it.
2. **No budget subsystem.** A caller that loops can spend a day of allowance
   without noticing, because nothing here counts.
3. **No `mailto` semantics.** Sent when configured; its effect is undocumented.
4. **Licence versions are absent.** `CC-BY` carries no version, so
   `content_version` stays `UNKNOWN`. A client that needs the version must read
   it from elsewhere or ask.
5. **Crossref and Unpaywall remain unimplemented**, with their policy still
   UNVERIFIED. OpenAlex working is not a reason to move on to them.
6. **No service layer.** `OpenAlexProvider → Paper` and nothing above it. No
   LiteratureService, no agent, no ranking.

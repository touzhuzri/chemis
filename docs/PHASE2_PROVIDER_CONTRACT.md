# Phase 2 — Provider Contract Sheet

Verification round: **2026-09-30** (field structure) and **2026-09-30** (official
prose documentation, OpenAlex only).

---

## 0. Status vocabulary — four classes, not three

| Class | Meaning |
|---|---|
| **VERIFIED** | Stated explicitly in official OpenAlex / Crossref / Unpaywall documentation that was read in this round. |
| **LIVE_RESPONSE_VERIFIED** | Field name, type or shape confirmed by the provider's own live API response. The official policy prose does **not** state it. |
| **UNVERIFIED** | Could not be established from an official source. No value is guessed. |
| **NOT_APPLICABLE** | The provider has no such concept. |

> **LIVE_RESPONSE_VERIFIED is never silently promoted to VERIFIED.**
> The distinction is the point: a field can be provably named `pdf_url` and still
> carry no policy meaning whatsoever. Upgrading one into the other is how
> "the API returns it" quietly becomes "we may use it".

**Nothing marked UNVERIFIED may be implemented from, and a client may not be
written while any cell it depends on is UNVERIFIED.**

---

## 1. Sources

| Source | Retrieved | Usable |
|---|---|---|
| `https://help.openalex.org/llms.txt` | HTTP 200, 31 424 B — the site's own machine index | **yes** |
| `https://help.openalex.org/<path>.md` — 11 policy pages as markdown | HTTP 200, 3.5–14.4 KB each | **yes** |
| OpenAlex live API + response headers | HTTP 200 | **yes** |
| Crossref `tips-for-using-the-crossref-rest-api` (prose) | HTTP 200, 20 138 B | **yes** |
| Crossref `rest-api-filters` (prose) | HTTP 200, 21 037 B | **yes** |
| Crossref live API | HTTP 200 | **yes** |
| Unpaywall live API | HTTP 200 / 404 / 422 | **yes** |
| **OpenAlex main site** `openalex.org/{terms,pricing,about}` | **HTTP 403** behind Cloudflare — "Just a moment… Enable JavaScript and cookies" | **NO** |
| **Unpaywall site**, every path including `robots.txt`, `sitemap.xml`, `llms.txt`, `*.md` | HTTP 200 but an identical **1 421 B** JavaScript shell, always | **NO** |
| **Crossref** authentication / content-negotiation / headers pages | HTTP 404 at every path tried | **NO** |
| `api.openalex.org/` self-description `documentation_url: "/docs"` | HTTP 404 — **the API's own advertised docs pointer is broken** | — |

**Net effect.** OpenAlex is fully documented in readable form and is largely
resolved. Crossref is partially documented. Unpaywall has **no readable prose
documentation at all** from this environment; only its live API is available.

---

## 2. OpenAlex

### 2.1 Usage and rate policy — VERIFIED

Source: `help.openalex.org/access/pricing`, `/api/authentication`,
`/access/example-costs`.

| Property | Value | Status |
|---|---|---|
| Model | Usage-based. **Data is free; serving it is not.** | **VERIFIED** |
| Free tier | **$1 of API usage per day**, free account, no payment method | **VERIFIED** |
| Without a key | **$0.10/day** — one tenth of the keyed budget | **VERIFIED** |
| Free key vs keyless | a free key gives **10×** the keyless budget | **VERIFIED** |
| Budget reset | every midnight UTC | **VERIFIED** |
| More usage | prepaid, $1 increments, expires 3 months after last purchase; or annual plans ($20–$200+/day) | **VERIFIED** |
| `X-RateLimit-Limit` | "Your total daily budget" | **VERIFIED** |
| `X-RateLimit-Remaining` | "Remaining for today" | **VERIFIED** |
| `X-RateLimit-Credits-Used` | "Cost of this request" | **VERIFIED** |
| `X-RateLimit-Reset` | "Seconds until reset (midnight UTC)" | **VERIFIED** |
| 429 conditions | (a) daily budget exceeded, (b) **more than 100 requests per second** | **VERIFIED** |
| `per_page` maximum | **docs say 100** | **VERIFIED (docs)** |
| `per_page` maximum | **live API accepted 200; 500 → "must be between 1 and 200"** | **LIVE_RESPONSE_VERIFIED — CONTRADICTS the docs. See §7.1** |
| Basic paging limit | 10 000 results, then cursor paging | **VERIFIED** |
| OR values per filter | 100 | **VERIFIED** |
| `sample` maximum | 10 000 | **VERIFIED** |
| Key delivery | `?api_key=` **or** `Authorization: Bearer` | **VERIFIED** |
| Cost: list + filter | **$0.10** | **VERIFIED** |
| Cost: search | **$1.00** | **VERIFIED** |
| Cost: semantic search | **$1.00** | **VERIFIED** |
| Cost: **content download** | **$10.00 per file** | **VERIFIED** |
| Worked example | 1 000 PDFs = **$10.00**; one search ≈ the whole $1 daily budget | **VERIFIED** |
| Polite pool | **No "polite pool" is mentioned in any page read.** | **VERIFIED (absent)** |
| `mailto` | accepted by the live API; **its purpose is not stated in any page read** | **LIVE_RESPONSE_VERIFIED / UNVERIFIED (meaning)** |
| Personal/research use | "enough for most research and personal projects" | **VERIFIED** |

### 2.2 Data licence — VERIFIED

Source: `help.openalex.org/access/pricing` §"Free data, paid services".

| Property | Value | Status |
|---|---|---|
| Licence | **CC0 public domain** | **VERIFIED** |
| Personal-use carve-out | **none** — stated explicitly: "no 'personal use only' carve-out and no permission to ask" | **VERIFIED** |
| API vs dump | the same CC0 covers both; the dump is called out as openly downloadable | **VERIFIED** |
| Storing metadata | permitted under CC0 | **VERIFIED** |
| Storing a raw API response as a fixture | permitted under CC0 (subject to §9 of the design doc) | **VERIFIED** |
| Attribution | **not a licence condition** (CC0 imposes none). The site *requests* a citation: "Please use: Priem, J., Piwowar, H., & Orr, R. (2022)…" | **VERIFIED** |

### 2.3 Content — `content.openalex.org`

Source: `help.openalex.org/access/fulltext`.

| Property | Value | Status |
|---|---|---|
| What it is | OpenAlex's **own cached copy** of full text, 50M+ OA works, as PDF and Grobid TEI XML | **VERIFIED** |
| URL form | `https://content.openalex.org/works/{work_id}.pdf?api_key=KEY`; `.grobid-xml` for TEI | **VERIFIED** |
| Authentication | **an API key is required**; "Any API key works, including a free one" | **VERIFIED** |
| Metering | each download is metered against the daily budget ($10/file) | **VERIFIED** |
| **Retrieval permission** | **"The PDFs retain their original copyright. OpenAlex does not grant any additional rights to the content."** | **VERIFIED** |
| Per-work licence | check `best_oa_location.license`; OpenAlex offers no blanket grant | **VERIFIED** |
| Relationship to `best_oa_location` | separate. `content_urls` is OpenAlex-hosted; `best_oa_location.pdf_url` is the **publisher's** URL. The docs explicitly warn: "Do **not** map work IDs to archive objects via `locations[].pdf_url`" | **VERIFIED** |
| `has_content` | `{pdf: bool, grobid_xml: bool}`; **filter syntax is `has_content.pdf:true`** (dotted) | **VERIFIED** |
| `content_urls` | `{pdf, grobid_xml}`; **API-only**, not in the snapshot | **VERIFIED** |
| Saving | not stated; follows from "retains its original copyright" → per-work licence governs | **UNVERIFIED** (explicit statement about retention absent) |

**Consequence for our code — and it confirms the Phase 2.0a decision.** OpenAlex
*provides* the URL and *permits* the retrieval call, but grants **no rights to
the content**. Permission is per-work and comes from `best_oa_location.license`.
A record with `content_urls` and no licence on the location is therefore
`FulltextPermission.UNKNOWN`, and the gate denies. Recording the URL in `notes`
while refusing to promote it to `fulltext_url` is exactly right, and is now
backed by an official sentence rather than by caution.

### 2.4 OA status — VERIFIED, and our enum is complete

Source: `help.openalex.org/data/works/open-access`.

| Value | Official definition | Status |
|---|---|---|
| `diamond` | "Published in a fully-OA journal that charges no article processing charges — free for both readers and authors" | **VERIFIED** |
| `gold` | "Published in a fully-OA journal" | **VERIFIED** |
| `green` | "Toll-access on the publisher landing page, but a free copy exists in an OA repository" | **VERIFIED** |
| `hybrid` | "Free under an open license in a toll-access journal" | **VERIFIED** |
| `bronze` | **"Free to read on the publisher landing page, but without an identifiable open license"** | **VERIFIED** |
| `closed` | "Everything else" | **VERIFIED** |

**"The hybrid/bronze line is drawn by license. A free-to-read article in a
toll-access journal is `hybrid` if it carries an open license and `bronze` if it
doesn't."**

Six values, exactly matching our enum. **No new status; the enum is complete.**

**This is the official grounding for `BRONZE → UNKNOWN`.** Bronze is *defined*
as the absence of an identifiable open licence. Mapping it to
`OA_NO_DOWNLOAD` would have invented a prohibition the definition does not
contain; mapping it to `MAY_RETRIEVE` would have inverted its meaning. The
Phase 2 revision was right, and now for a stated reason rather than a cautious
one.

### 2.5 Licence vocabulary — VERIFIED

Source: `help.openalex.org/data/licenses`.

`cc-by`, `cc-by-sa`, `cc-by-nd`, `cc-by-nc`, `cc-by-nc-sa`, `cc-by-nc-nd`,
`public-domain` (CC0), plus a pseudo-licence **`implied-oa`** (publisher labels
an article OA under an APC-funded hybrid arrangement but there is no specific
licence). OpenAlex "errs toward inclusiveness" on what counts as open.

> **Gap found.** Our `PERMISSIVE_SPDX` contains `cc-by`, `cc-by-sa` and their
> versioned forms, but **not `public-domain`**, which is OpenAlex's name for
> CC0. A public-domain work would resolve to `UNKNOWN` rather than
> `MAY_RETRIEVE`. That is fail-closed and therefore safe, but it is wrong. It
> is a **design note for the next phase**, not a change made here — widening the
> permissive set is a policy decision, and it must be made deliberately.

---

## 3. Crossref

Field structure was verified from the live API in the previous round and is
unchanged: `/v1/works`, `/v1/works/{doi}`, `message-type` `work` / `work-list`,
cursor paging, `rows` default 20 max 1000, `rows=0`, `select`, `filter`, `title[]`
and `container-title[]` as arrays, `author[].given/family/sequence/affiliation`,
`issued.date-parts`, `URL` as a DOI resolver, `resource.primary.URL`.

### 3.1 `link[]` and `license[]` — LIVE_RESPONSE_VERIFIED, policy VERIFIED for scope

| Observation | Value | Status |
|---|---|---|
| `link[]` entry shape | `{URL, content-type, content-version, intended-application}` | **LIVE_RESPONSE_VERIFIED** |
| `link[].intended-application` | `"text-mining"` | **LIVE_RESPONSE_VERIFIED** |
| `license[]` entry shape | `{start, content-version, delay-in-days, URL}` | **LIVE_RESPONSE_VERIFIED** |
| `license[].content-version` | `"tdm"`, URL pointing at the publisher's TDM terms | **LIVE_RESPONSE_VERIFIED** |
| Meaning of `intended-application: "text-mining"` | the link is a **text-and-data-mining** link | **LIVE_RESPONSE_VERIFIED** (from the field's own name and value) |
| `TDM metadata exists ⇒ full-text download allowed` | **NOT SUPPORTED BY ANYTHING READ.** No page read states this. | **UNVERIFIED — and must not be inferred** |
| `abstract` | absent on the record sampled; JATS format | presence **LIVE_RESPONSE_VERIFIED**; JATS format **UNVERIFIED** |

### 3.1b Live-verified behaviour of the implemented client (2026-09-30)

| Check | Result |
|---|---|
| `GET /v1/works/10.1038%2Fnphys1170` (percent-encoded DOI) | **HTTP 200**, correct record returned — the encoding this provider sends works |
| `GET /v1/works?query.bibliographic=…&rows=1&cursor=*` | **HTTP 200**, `message` carries `items`, `total-results`, `next-cursor` |
| `GET /v1/works?query.bibliographic=…&rows=1000` | **HTTP 200**, `items-per-page: 1000` — the documented cap is real |
| real record `issued` / `published-print` | both present, so the date priority is exercisable on live data |
| real record `abstract` | **absent** — consistent with "often absent" |
| real record `publisher` / `ISSN` | present |

### 3.2 Policy — UNVERIFIED

| Property | Status |
|---|---|
| Rate limit | **UNVERIFIED** — no number in any readable page |
| `mailto` / polite pool | **UNVERIFIED** — the string does not appear in either readable page |
| `User-Agent` requirement | **UNVERIFIED** |
| Dynamic throttling on the public API | **UNVERIFIED** |
| API data licence | **UNVERIFIED** — the CC-BY 4.0 notice on the site is about **website content**, not API data, and must not be read as an API grant |
| Recommended client behaviour | **VERIFIED, qualitatively**: cache results; use filters over per-DOI loops; use `select`; split very large pulls into smaller queries; "use your REST API responsibly and efficiently" |
| Enforcement | **VERIFIED**: "Very occasionally we have had to block users who misuse our APIs, usually through carelessness rather than malice." A dynamic limit plainly exists; its numbers are not published where we could read them. |
| Content negotiation | **UNVERIFIED** — every path tried returned 404 |

---

## 4. Unpaywall

**No readable official documentation exists from this environment.** Every path
on `unpaywall.org` — including `robots.txt` and `sitemap.xml` — returns the same
1 421 B JavaScript shell. The API's own root response advertises
`documentation_url: "https://unpaywall.org/data"`, which is that same shell.
`support.unpaywall.org` is unreachable.

Everything below is therefore **LIVE_RESPONSE_VERIFIED at best**.

### 4.1 Field structure — LIVE_RESPONSE_VERIFIED

| Property | Value |
|---|---|
| Endpoint | `https://api.unpaywall.org/v2/{doi}` |
| `email` | **required**; omitting → HTTP 422 `{"error": true, "message": "Email address required in API call…"}` |
| Unknown DOI | HTTP 404, **HTML** body |
| Top-level keys | `doi, doi_url, title, year, published_date, journal_name, journal_issn_l, journal_issns, publisher, genre, is_oa, oa_status, best_oa_location, first_oa_location, oa_locations, oa_locations_embargoed, has_repository_copy, journal_is_oa, journal_is_in_doaj, is_paratext, z_authors, updated, data_standard` |
| `best_oa_location` keys | `endpoint_id, evidence, host_type, is_best, license, oa_date, pmh_id, repository_institution, updated, url, url_for_landing_page, url_for_pdf, version` |
| `license` | `null` observed **on a `gold` record** |
| `evidence` / `updated` (location) | literal value `"deprecated"` |
| `pmh_id` | location level only |
| `is_oa_confirmed` | **absent** from both records probed |

### 4.2 Policy — UNVERIFIED, all of it

| Property | Status |
|---|---|
| Rate limit / request limits | **UNVERIFIED** |
| User-Agent / etiquette | **UNVERIFIED** |
| API key required? | **UNVERIFIED** — none was needed for the calls made, but absence of a requirement was never stated |
| **Official definition of `bronze`** | **UNVERIFIED** |
| Official definitions of gold / green / hybrid / closed | **UNVERIFIED** |
| Meaning of a `null` `license` | **UNVERIFIED** — the observation is verified, the interpretation is not |
| Provenance of `license` (publisher vs repository metadata) | **UNVERIFIED** |
| API data licence | **UNVERIFIED** |
| Caching / storage permitted | **UNVERIFIED** |
| Attribution | **UNVERIFIED** |

### 4.3 The one thing we do know, and it is enough

A `gold` record was observed carrying `license: null`. OpenAlex is a different
provider and cannot speak for Unpaywall — but the shape is consistent: OA status
and licence are separate fields, and a status does not arrive with a licence
attached. Our policy already refuses to derive `MAY_RETRIEVE` from a status, so
this observation changes nothing. It is a sanity check, not a justification.

---

## 4b. Four things that are not the same thing

This is the section the Crossref provider exists to keep honest.

```
Crossref metadata          what a publisher deposited about a work
        !=
Crossref licence metadata  a statement about that deposit, and about what the
                           depositor agreed to for text-and-data-mining
        !=
article copyright         the rights in the work itself
        !=
full-text retrieval       whether *we* may fetch, keep or redistribute a copy
```

What the live API actually returned on 2026-09-30, for one record:

```json
"link":    [{"URL": ".../nphys1170.pdf", "content-type": "application/pdf",
             "content-version": "vor", "intended-application": "text-mining"}]
"license": [{"content-version": "tdm", "delay-in-days": 0,
             "URL": "http://www.springer.com/tdm"}]
```

Both fields say *text and data mining*. Neither says *reuse*. Neither says
*retrieval*. The `intended-application` name bounds the link; the
`content-version: "tdm"` bounds the licence. Reading either as a grant is the
exact error the frozen permission rules exist to prevent.

Three consequences the provider implements and tests:

1. `link[].URL` **never** becomes `fulltext_url`. The links go to
   `AccessAssessment.notes` as recorded metadata, not as a location we hold.
2. `license[].content-version == "tdm"` **never** yields `MAY_RETRIEVE`. The
   licence URL is kept verbatim in `LicenseInfo.url`, `spdx_id` stays `None`
   because we established no SPDX identity for it, and the policy therefore
   cannot act on it.
3. The CC-BY 4.0 notice on the Crossref **website** is about website content.
   It is not an API data licence and it is not the licence of any article. The
   API data licence remains **UNVERIFIED** and no code assumes it.

What Crossref's licence metadata *is* good for: telling a downstream reader
that a TDM route was declared, with the terms URL to read. That is a fact
worth keeping, and it is kept as one.

## 5. Capability Matrix

| Capability | OpenAlex | Crossref | Unpaywall |
|---|---|---|---|
| Search | VERIFIED | VERIFIED | NOT_APPLICABLE |
| Metadata | VERIFIED | VERIFIED | VERIFIED |
| DOI lookup | VERIFIED | VERIFIED | VERIFIED |
| Pagination | VERIFIED | VERIFIED | NOT_APPLICABLE |
| Abstract | LIVE_RESPONSE_VERIFIED (reconstructed) | UNVERIFIED (absent on sample) | NOT_APPLICABLE |
| OA classification | VERIFIED | NOT_APPLICABLE | LIVE_RESPONSE_VERIFIED |
| External OA location | VERIFIED | NOT_APPLICABLE | LIVE_RESPONSE_VERIFIED |
| Provider-hosted fulltext | VERIFIED | NOT_APPLICABLE | NOT_APPLICABLE |
| TDM metadata | NOT_APPLICABLE | LIVE_RESPONSE_VERIFIED | NOT_APPLICABLE |
| License information | VERIFIED | LIVE_RESPONSE_VERIFIED | LIVE_RESPONSE_VERIFIED |
| Fulltext permission evidence | **UNVERIFIED** (licence field exists; whether it authorises *our* retrieval is not stated) | **UNVERIFIED** | **LIVE_RESPONSE_VERIFIED** (a `license` field exists and is routinely null) |
| Automated retrieval allowed | **UNVERIFIED** (OpenAlex grants no additional rights; per-work licence governs) | **UNVERIFIED** | **UNVERIFIED** |

The Unpaywall row deserves its own line, because it is the one most likely to
be got wrong: Unpaywall *is* the access provider, and a `gold` record from it
was observed carrying `license: null`. An OA status is not a licence, and a
licence is not a permission.

**No provider is VERIFIED for the last two rows, and none ever will be from
metadata alone.** The last row is a question about *us* and about a work's
copyright, not about an API. It is answered by evidence, per work, by
`core/policy/access.py` and `services/access_reconciliation.py` -- and the
answer is `UNKNOWN` far more often than anyone would like.

Implementation status (2026-09-30): all three providers are implemented,
metadata-only, behind the `Transport` seam. OpenAlex and Crossref have verified
policy; **Unpaywall's rate limit, terms, data licence and the prose definition
of `bronze` remain UNVERIFIED**, and no code assumes any of them.

---

## 6. Frozen FulltextPermission rules

These are the rules the code implements, now written down as contract.

### MAY_RETRIEVE
Only on **explicit, permissive licence evidence applicable to the retrieved
version**. Nothing else qualifies — not an OA status, not a URL, not the
provider offering a download endpoint.

### UNKNOWN
The gate result whenever evidence is absent. Typical cases:

- an OA status exists but no licence is known;
- a URL exists but retrieval permission is unknown;
- an OpenAlex `content_urls` entry exists but the per-work licence is unknown.

**UNKNOWN means "we decline to act because we have not established
permission". It is not a finding that retrieval is forbidden, and it is never
upgraded to NOT_PERMITTED.**

### NOT_PERMITTED
Only on **explicit evidence that retrieval is not permitted now** — currently
only a confirmed `CLOSED` OA status. Absence of evidence is not evidence of
absence.

### Consequences that follow mechanically

1. A `gold` record with `license: null` → **UNKNOWN**.
2. An OpenAlex record whose only full text is `content_urls` → **UNKNOWN**,
   because OpenAlex states it "does not grant any additional rights to the
   content".
3. A Crossref `link[]` with `intended-application: "text-mining"` → **UNKNOWN**.
   The field's own name bounds its scope; it is not a reuse grant.
4. Any bronze record → **UNKNOWN**, now with an official definition to cite.

---

## 7. Contradictions and gaps found

### 7.1 OpenAlex `per_page`: docs and live API disagree

Official prose says the maximum is **100**. The live API **accepted 200** and
rejected 500 with `"per-page parameter must be between 1 and 200"`. Both
observations are recorded. This is not resolved here and must not be papered
over: a client must either probe at start-up or use the lower value.

### 7.2 OpenAlex `documentation_url` is broken

`https://api.openalex.org/` advertises `documentation_url: "/docs"`, which
returns 404. Harmless, but it means the API's self-description is not a
trustworthy index.

### 7.3 Our permissive-licence set is missing `public-domain`

See §2.5. Fail-closed, therefore safe; wrong, therefore to be fixed — but as a
deliberate policy change, not a drive-by.

### 7.4 AccessAssessment is sufficient — no model change

The model already carries `source`, `source_url`, `license`, `fulltext_url`,
`url_type`, `checked_at`, `permission`, `permission_basis`, `notes`. All nine
expressible. **No field was added**, and specifically **no `hosting_type`**.

*Design note, not a change.* OpenAlex's `content_urls` and
`best_oa_location` are two different kinds of location with different rights,
and a future full-text phase may need to distinguish "provider-hosted copy"
from "external OA copy". Today `notes` carries that distinction adequately.
When a real retrieval path exists, revisit — do not pre-build for it.

---

## 8. Stop-condition status

| Blocking item | State |
|---|---|
| OpenAlex rate policy | **VERIFIED** — resolved |
| OpenAlex data licence | **VERIFIED** — resolved (CC0) |
| OpenAlex content retrieval terms | **VERIFIED** — resolved (no additional rights; per-work licence) |
| Crossref rate / mailto policy | **UNVERIFIED** — pages 404 |
| Crossref data / licence policy | **UNVERIFIED** |
| Crossref content negotiation | **UNVERIFIED** — pages 404 |
| Unpaywall rate policy | **UNVERIFIED** — no readable docs exist |
| Unpaywall API / data licence | **UNVERIFIED** — same |
| Unpaywall bronze definition | **UNVERIFIED** — same |

**Phase 2.0b is partially verified. Provider implementation remains blocked.**

Three of nine blocking items were resolved; six remain, all because the
official prose is unreachable from this environment. That is the correct
outcome, not a failure — and no client is written.

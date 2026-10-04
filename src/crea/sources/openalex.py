"""OpenAlex provider. Metadata only.

> **This provider is intentionally separated from full-text retrieval because
> OpenAlex content retrieval has a separate cost and rights model.** The
> documented price of a content download is $10 against a $1/day free budget,
> and OpenAlex states plainly that the PDFs retain their original copyright
> and that it "does not grant any additional rights to the content". Nothing in
> this module downloads, and nothing here decides whether downloading is
> permitted.

Responsibilities, and nothing beyond them: request, receive, parse, attribute.
Dedup, cross-provider merge, policy, licence inference for the article, and
ranking all live elsewhere. A provider that started making those decisions
would become impossible to swap and impossible to test offline.

Contract source: `docs/PHASE2_PROVIDER_CONTRACT.md` §2 (VERIFIED against
`help.openalex.org` markdown, 2026-09-30). Where the docs and a live probe
disagreed, the official value is used and the discrepancy is recorded in
`OFFICIAL_PER_PAGE_MAX` below.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from crea.core.errors import (
    AuthenticationOrAuthorizationError,
    InvalidRequest,
    InvalidUpstreamResponse,
    ProviderError,
    RateLimited,
    UpstreamError,
    UpstreamSchemaError,
)
from crea.core.ids import content_hash, normalize_doi
from crea.core.models.literature import (
    AbstractRecord,
    AbstractSource,
    AccessAssessment,
    Author,
    Identifiers,
    LicenseInfo,
    OAStatus,
    Paper,
)
from crea.providers.base import Capability, LiteratureQuery, PaperRef
from crea.sources.base import (
    BaseProvider,
    ProviderResult,
    RateLimit,
    RateSnapshot,
    Transport,
    redact_url,
)

WORKS_ENDPOINT = "https://api.openalex.org/works"

#: The official maximum. A live probe on 2026-09-30 was served 200 results and
#: rejected 500 with "per-page parameter must be between 1 and 200", which
#: contradicts the documentation. The documented 100 is used regardless:
#: LIVE_RESPONSE_VERIFIED is not a contract.
OFFICIAL_PER_PAGE_MAX = 100

#: "Basic paging limit 10,000 results" per the authentication documentation.
BASIC_PAGING_LIMIT = 10_000

#: OpenAlex's licence vocabulary, and the SPDX identifier each maps to.
#:
#: `public-domain` is the only entry OpenAlex explicitly equates to a named
#: SPDX licence ("public domain (CC0)"), and `CC0-1.0` is confirmed as the SPDX
#: short identifier at spdx.org/licenses/CC0-1.0.html. The mapping is documented
#: in both places; it is not a locally invented identifier.
#:
#: The CC-* entries are licence *families*. OpenAlex does not state a version
#: and neither do we: mapping `cc-by` to `CC-BY-4.0` would assert a version
#: nobody told us. The unversioned family name is carried instead, and
#: `LicenseInfo.content_version` stays UNKNOWN to record the absence.
OPENALEX_LICENSE_TO_SPDX = {
    "public-domain": "CC0-1.0",
    "cc-by": "CC-BY",
    "cc-by-sa": "CC-BY-SA",
    "cc-by-nd": "CC-BY-ND",
    "cc-by-nc": "CC-BY-NC",
    "cc-by-nc-sa": "CC-BY-NC-SA",
    "cc-by-nc-nd": "CC-BY-NC-ND",
}

#: OpenAlex's own data licence. CC0, confirmed on the pricing page
#: ("released under a CC0 public-domain license with no 'personal use only'
#: carve-out and no permission to ask"). This is a fact about the *data*, and
#: it is deliberately never written onto a `Paper`: saying "OpenAlex data is
#: CC0" is not a statement that any article or PDF is CC0.
OPENALEX_DATA_LICENSE = "CC0-1.0"


def reconstruct_abstract(inverted_index: dict | None) -> str | None:
    """Rebuild running text from OpenAlex's positional inverted index.

    The output is a RECONSTRUCTION. Word order comes from the position lists,
    which is a best effort, not a quotation, and it is tagged as one wherever it
    lands. A null index yields None rather than an empty string, so "no
    abstract" and "empty abstract" stay distinguishable.
    """
    if not inverted_index:
        return None
    positions: list[tuple[int, str]] = []
    for word, spots in inverted_index.items():
        if not isinstance(spots, (list, tuple)):
            return None
        try:
            positions.extend((int(p), word) for p in spots)
        except (TypeError, ValueError):
            return None
    if not positions:
        return None
    positions.sort(key=lambda pair: (pair[0], pair[1]))
    return " ".join(word for _pos, word in positions)


def _oa_status(value: object) -> OAStatus:
    """Map an upstream string onto OAStatus, defaulting to UNKNOWN.

    An unrecognised status is never coerced into an existing one. Treating an
    unknown value as GOLD would let a record inherit a permission path it never
    qualified for.
    """
    try:
        return OAStatus(str(value).lower())
    except ValueError:
        return OAStatus.UNKNOWN


class OpenAlexProvider(BaseProvider):
    """Search and metadata retrieval against the OpenAlex Works API."""

    name = "openalex"
    requires_key = False          # keyless requests are served; the key only raises budget
    capabilities = frozenset({
        Capability.LITERATURE_SEARCH,
        Capability.METADATA,
        Capability.OPEN_ACCESS,
        Capability.COMPOUND_LOOKUP,
    })
    # Documented as 100 requests/second. We do not sit at the ceiling, and the
    # provider never sleeps on its own -- a 429 is surfaced to the caller with
    # the budget headers attached, because only the caller knows whether it can
    # afford to wait.
    rate_limit = RateLimit(requests_per_second=10.0, burst=10, max_retries=3)

    def __init__(
        self,
        transport: Transport,
        api_key: str | None = None,
        mailto: str | None = None,
        endpoint: str = WORKS_ENDPOINT,
        **kwargs,
    ) -> None:
        super().__init__(name=self.name, transport=transport, **kwargs)
        # The key is held in memory for the request and never copied onto a
        # Paper, a result, an error message or a log line.
        self._api_key = api_key or None
        self._mailto = mailto or None
        self.endpoint = endpoint

    def status(self):
        return super().status()

    # ------------------------------------------------------------------
    # request plumbing
    # ------------------------------------------------------------------

    def _auth_params(self) -> dict:
        params: dict[str, str] = {}
        if self._api_key:
            params["api_key"] = self._api_key
        if self._mailto:
            # Optional and unlabelled in the documentation. It is passed
            # because the operator supplied a contact address; it is NOT a
            # polite pool, no such mechanism is documented, and its presence
            # changes no rate limit and no priority.
            params["mailto"] = self._mailto
        return params

    def _check(self, response, url: str):
        code = response.status_code
        if 200 <= code < 300:
            return response.payload
        safe = redact_url(url)
        if code == 400:
            raise InvalidRequest(
                f"OpenAlex rejected the request (400): {safe}",
                provider=self.name, http_status=code)
        if code in (401, 403):
            raise AuthenticationOrAuthorizationError(
                f"OpenAlex refused the credential (HTTP {code}): {safe}",
                provider=self.name, http_status=code)
        if code == 429:
            raise RateLimited(
                f"OpenAlex returned 429 -- daily budget exhausted or request "
                f"rate above the documented 100/second ceiling: {safe}",
                provider=self.name, http_status=code)
        if 500 <= code < 600:
            raise UpstreamError(
                f"OpenAlex is failing upstream (HTTP {code}): {safe}",
                provider=self.name, http_status=code)
        raise ProviderError(
            f"unexpected OpenAlex status {code}: {safe}",
            provider=self.name, http_status=code)

    async def _get(self, params: dict):
        url = f"{self.endpoint}"
        full = {**params, **self._auth_params()}
        response = await self.transport.request("GET", url, params=full)
        payload = self._check(response, url)
        if not isinstance(payload, dict):
            raise InvalidUpstreamResponse(
                f"OpenAlex returned {type(payload).__name__}, expected an object",
                provider=self.name, http_status=response.status_code)
        return payload, RateSnapshot.from_headers(response.headers)

    # ------------------------------------------------------------------
    # public operations
    # ------------------------------------------------------------------

    async def search(self, query: LiteratureQuery) -> ProviderResult:
        """Full-text keyword search.

        Documented at $1.00 per call against a $1/day free budget, so this is
        the most expensive metadata call we make and is called sparingly.
        """
        params = {"search": query.text, "per-page": min(query.limit, OFFICIAL_PER_PAGE_MAX)}
        if query.year_from is not None or query.year_to is not None:
            lo = query.year_from or 0
            hi = query.year_to or 9999
            params["filter"] = f"from_publication_date:{lo}-01-01," \
                               f"to_publication_date:{hi}-12-31"
        payload, rate = await self._get(params)
        works = self._works(payload)
        papers = [self._ingest(w, query.text) for w in works]
        return ProviderResult(
            provider=self.name,
            papers=papers,
            raw_hashes={self.name: _hash(payload)},
            retrieved_at=self._now(),
            complete=True,
            rate=rate,
        )

    async def get_work(self, openalex_id: str) -> Paper | None:
        """Fetch one work by OpenAlex ID, e.g. `W1234567890`."""
        work_id = str(openalex_id).strip().rstrip("/").split("/")[-1]
        url = f"{self.endpoint}/{work_id}"
        response = await self.transport.request("GET", url, params=self._auth_params())
        payload = self._check(response, url)
        if not isinstance(payload, dict):
            raise UpstreamSchemaError(
                "OpenAlex work lookup did not return an object",
                provider=self.name, http_status=response.status_code)
        return self._ingest(payload)

    async def get_metadata(self, ref: PaperRef) -> Paper | None:
        """Resolve one work from a `PaperRef`.

        The shared contract's lookup entry point. A `PaperRef` carrying an
        OpenAlex ID is a direct fetch; one carrying a DOI goes through the
        `doi` filter, which is the only way OpenAlex addresses a work by
        identifier.
        """
        if ref.source_id and not ref.doi:
            return await self.get_work(ref.source_id)
        if ref.doi:
            return await self.get_by_doi(ref.doi)
        return None

    async def get_by_doi(self, doi: str) -> Paper | None:
        """Fetch one work by DOI. The DOI is normalised before it is sent."""
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        result = await self.list_works({"filter": f"doi:{normalized}"}, limit=1)
        return result.papers[0] if result.papers else None

    async def list_works(self, filters: dict, limit: int = OFFICIAL_PER_PAGE_MAX) -> ProviderResult:
        params = {"per-page": min(limit, OFFICIAL_PER_PAGE_MAX), **filters}
        payload, rate = await self._get(params)
        papers = [self._ingest(w) for w in self._works(payload)]
        return ProviderResult(
            provider=self.name,
            papers=papers,
            raw_hashes={self.name: _hash(payload)},
            retrieved_at=self._now(),
            complete=True,
            rate=rate,
        )

    async def paginate(
        self,
        filters: dict,
        per_page: int = OFFICIAL_PER_PAGE_MAX,
        max_pages: int = 100,
    ) -> AsyncIterator[Paper]:
        """Cursor pagination, per the documented mechanism.

        `cursor=*` opens the sequence and `meta.next_cursor` continues it. The
        documented basic paging limit is 10,000 results; `max_pages` is a
        second, local brake on top of that, because walking 100 pages of a large
        corpus is a budget decision the caller should have to make explicitly.
        """
        per_page = min(per_page, OFFICIAL_PER_PAGE_MAX)
        cursor: str | None = "*"
        seen = 0
        for _page in range(max_pages):
            params = {**filters, "per-page": per_page}
            if cursor is not None:
                params["cursor"] = cursor
            payload, _rate = await self._get(params)
            works = self._works(payload)
            if not works:
                return
            for work in works:
                seen += 1
                yield self._ingest(work)
            if seen >= BASIC_PAGING_LIMIT:
                return
            meta = payload.get("meta")
            cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
            if not cursor:
                return

    # ------------------------------------------------------------------
    # parsing
    # ------------------------------------------------------------------

    def _works(self, payload: dict) -> list[dict]:
        results = payload.get("results")
        if results is None:
            raise UpstreamSchemaError(
                "OpenAlex response has no 'results' key", provider=self.name)
        if not isinstance(results, list):
            raise UpstreamSchemaError(
                f"OpenAlex 'results' is {type(results).__name__}, expected a list",
                provider=self.name)
        for item in results:
            if not isinstance(item, dict):
                raise UpstreamSchemaError(
                    "OpenAlex returned a non-object inside 'results'",
                    provider=self.name)
        return results

    def _now(self) -> str:
        return _now()

    def _ingest(self, work: dict, query_text: str = "") -> Paper:
        """Parse, attribute, then let policy look at the access record.

        The order matters and is the provider/policy split: the provider
        reports what OpenAlex said, and `report_access` is where the permission
        is inferred. A provider that inferred permission itself would be
        impossible to swap and impossible to test.
        """
        paper = self.stamp(self.parse_work(work, query_text))
        paper.access = self.report_access(paper.access)
        return paper

    def parse_work(self, work: dict, query_text: str = "") -> Paper:
        """Map one OpenAlex work onto our `Paper` and attribute it."""
        paper = parse_work(work, provider_name=self.name, retrieved_at=self._now())
        if query_text:
            paper.raw_hashes[f"{self.name}:query"] = content_hash(query_text, 12)
        return self.stamp(paper)


def parse_work(
    work: dict,
    provider_name: str = "openalex",
    retrieved_at: str | None = None,
) -> Paper:
    """Map one OpenAlex work payload onto our `Paper`.

    Free-standing so the mapping can be exercised without a provider, a
    transport or a network.

    Field names come from the verified schema in the contract sheet.
    OpenAlex's location object uses `landing_page_url` / `pdf_url`; it has no
    `url` and no `url_for_pdf`, and falling back to those would be borrowing
    Unpaywall's schema.
    """
    if not isinstance(work, dict):
        raise UpstreamSchemaError("work is not an object", provider=provider_name)

    raw_doi = work.get("doi")
    doi = normalize_doi(raw_doi)
    work_id = str(work.get("id") or "").rstrip("/").split("/")[-1] or None
    ids = work.get("ids") or {}
    now = retrieved_at or _now()

    paper = Paper(
        paper_id=doi or work_id or "",
        # None, not "": "untitled" must look the same whichever provider
        # said it, or downstream code has to test both.
        title=(work.get("display_name") or work.get("title") or None),
        authors=_authors(work.get("authorships")),
        journal=_journal(work.get("primary_location")),
        year=work.get("publication_year"),
        url=work.get("id"),
        identifiers=Identifiers(
            doi=doi,
            openalex_id=work_id or _tail(ids.get("openalex")),
            pmid=_tail(ids.get("pmid")),
            pmcid=_tail(ids.get("pmcid")),
        ),
        retrieved_at=now,
    )

    text = reconstruct_abstract(work.get("abstract_inverted_index"))
    if text:
        paper.abstracts.append(AbstractRecord(
            text=text,
            # A positional reconstruction, always and forever. It is not the
            # publisher's text and is never admissible as primary evidence.
            source=AbstractSource.OPENALEX_RECONSTRUCTED,
            retrieved_at=now,
            raw_excerpt=text[:300],
        ))

    paper.access = _access(work, provider_name=provider_name, checked_at=now)
    # A parsed record says where it came from even before a provider stamps it,
    # so nothing downstream can hold an unattributed `Paper`. `stamp` is
    # idempotent and will not double up.
    if provider_name and provider_name not in paper.sources:
        paper.sources = [provider_name]
    if work_id:
        paper.source_ids[provider_name] = work_id
    # The raw DOI is never discarded. `identifiers.doi` is the normalised key;
    # a note is kept that the resolver form was what we were given.
    if raw_doi and normalize_doi(raw_doi) != raw_doi:
        paper.raw_hashes[f"{provider_name}:raw_doi"] = content_hash(str(raw_doi), 12)
    return paper


def _authors(authorships: object) -> list[Author]:
    if not isinstance(authorships, list):
        return []
    out = []
    for entry in authorships:
        if not isinstance(entry, dict):
            continue
        author = entry.get("author") or {}
        out.append(Author(
            name=str(author.get("display_name") or ""),
            orcid=author.get("orcid"),
        ))
    return out


def _journal(primary_location: object) -> str | None:
    if not isinstance(primary_location, dict):
        return None
    source = primary_location.get("source")
    if not isinstance(source, dict):
        return None
    name = source.get("display_name")
    return str(name) if name else None


def _access(
    work: dict,
    provider_name: str = "openalex",
    checked_at: str | None = None,
) -> AccessAssessment:
    oa = work.get("open_access") or {}
    location = work.get("best_oa_location") or {}
    content_urls = work.get("content_urls") or {}

    pdf_url = location.get("pdf_url") if isinstance(location, dict) else None
    landing_url = location.get("landing_page_url") if isinstance(location, dict) else None
    raw_license = location.get("license") if isinstance(location, dict) else None
    spdx = (
        OPENALEX_LICENSE_TO_SPDX.get(str(raw_license).lower())
        if raw_license else None
    )

    notes: list[str] = []
    if content_urls:
        # Recorded, never used. OpenAlex hosts the file and will serve the
        # download, but it states that the PDFs retain their original copyright
        # and that it grants no additional rights to them, so permission is
        # per-work and comes from best_oa_location.licence. Nothing in this
        # module downloads anything, in this phase or any other.
        offered = ", ".join(sorted(content_urls))
        notes.append(
            f"OpenAlex-hosted content offered ({offered}); OpenAlex grants no "
            f"additional rights to it, so it is not a full-text location and "
            f"retrieval is outside Phase 2.1A"
        )

    evidence = ["openalex:open_access"]
    if isinstance(location, dict) and location:
        evidence.append("openalex:best_oa_location")
    if content_urls:
        evidence.append("openalex:content_urls")
    if oa.get("any_repository_has_fulltext"):
        evidence.append("openalex:open_access.any_repository_has_fulltext")

    return AccessAssessment(
        oa_status=_oa_status(oa.get("oa_status")),
        oa_status_raw=str(oa["oa_status"]) if oa.get("oa_status") is not None else None,
        fulltext_url=pdf_url or landing_url,
        url_type="pdf" if pdf_url else ("landing_page" if landing_url else "unknown"),
        oa_version=(location.get("version") if isinstance(location, dict) else None) or "UNKNOWN",
        license=LicenseInfo(
            spdx_id=spdx,
            name=str(raw_license) if raw_license else "UNKNOWN",
            url=None,
            applies_to="oa_copy",
            is_known=bool(spdx),
            stated_by=[provider_name] if raw_license else [],
        ),
        notes=notes,
        evidence=evidence,
        checked_at=checked_at or _now(),
    )


    def _now(self) -> str:
        return _now()

    def _ingest(self, work: dict, query_text: str = "") -> Paper:
        """Parse, attribute, then let policy look at the access record.

        The order matters and is the provider/policy split: the provider
        reports what OpenAlex said, and `report_access` is where the permission
        is inferred. A provider that inferred permission itself would be
        impossible to swap and impossible to test.
        """
        paper = self.stamp(self.parse_work(work, query_text))
        paper.access = self.report_access(paper.access)
        return paper


def _now() -> str:
    from crea.core.provenance import utc_now_iso

    return utc_now_iso()


def _tail(value: object) -> str | None:
    if not value:
        return None
    tail = str(value).rstrip("/").split("/")[-1]
    return tail or None


def _hash(payload: Any) -> str:
    import json

    return content_hash(json.dumps(payload, sort_keys=True, ensure_ascii=False), 16)

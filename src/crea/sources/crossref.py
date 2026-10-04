"""Crossref provider. Metadata only.

The second provider, built to the same contract as OpenAlex and to the same
separation of duties: this module requests, parses, normalises, attributes and
maps errors. It does not deduplicate, does not merge, does not rank, and does
not decide permission.

> **A Crossref `link[]` is not a full-text permission.**
> A Crossref `license[]` is not a permission either. They are *statements about
> a deposit* -- what the publisher lodged, and for what purpose. Crossref
> license metadata, the article's copyright, and our permission to retrieve the
> full text are three different things, and nothing this module receives
> collapses the last one into the first two.

Contract source: `docs/PHASE2_PROVIDER_CONTRACT.md` §3, LIVE_RESPONSE_VERIFIED
from the API plus VERIFIED prose for pagination, rows, select and filter.
Crossref's rate limit, mailto etiquette and API data licence are still
UNVERIFIED, and nothing here assumes them.
"""

from __future__ import annotations

import json
from typing import AsyncIterator
from urllib.parse import quote

from crea.core.errors import (
    AuthenticationOrAuthorizationError,
    InvalidRequest,
    InvalidUpstreamResponse,
    ProviderError,
    RateLimited,
    ResourceNotFound,
    UpstreamError,
    UpstreamSchemaError,
)
from crea.core.ids import content_hash, normalize_doi
from crea.core.models.literature import (
    AbstractRecord,
    AbstractSource,
    AccessAssessment,
    AccessStatus,
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

WORKS_ENDPOINT = "https://api.crossref.org/v1/works"

#: Documented: "The default number of rows returned is 20 and you can increase
#: it up to 1000." Clamped, never passed through. A caller asking for 2000 gets
#: 1000 and is not silently obeyed.
ROWS_MAX = 1000
ROWS_MIN = 1
ROWS_DEFAULT = 20

#: "Basic paging limit" on our side only. This is NOT a Crossref limit and is
#: never described as one: the documented mechanism is `cursor`, and this is a
#: caller-side brake on how many pages one call is willing to walk.
DEFAULT_MAX_PAGES = 100

#: Crossref publishes several date fields on a work. "The publication date" is
#: **not** one of them -- it is a choice among several, and the choice below is
#: **CREA policy, not a Crossref rule.** Crossref does not rank its own date
#: fields and we have not found a document that says it does.
#:
#: The policy is versioned and the version is written onto every record, so a
#: change in the future is distinguishable from this one, and every raw date is
#: retained under `UpstreamFacts.publication_dates_raw` alongside the selection.
#:
#: `created` is the date the *record* was deposited. It is never a publication
#: date, whatever else changes.
DATE_PRIORITY = ("published-print", "published-online", "issued")
DATE_POLICY_VERSION = "crea-crossref-dates-v1"
DEPOSIT_DATE_FIELD = "created"


def effective_rows(requested: int | None) -> int:
    """Clamp a requested page size into the documented range."""
    if requested is None:
        return ROWS_DEFAULT
    return max(ROWS_MIN, min(int(requested), ROWS_MAX))


def _date_string(date_field: object) -> tuple[str | None, int | None]:
    """Render `{"date-parts": [[2021, 4, 17]]}` as far as the parts allow."""
    if not isinstance(date_field, dict):
        return None, None
    parts = date_field.get("date-parts")
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], list):
        return None, None
    values = parts[0]
    if not values or values[0] is None:
        return None, None
    year = int(values[0])
    if len(values) > 1 and values[1]:
        if len(values) > 2 and values[2]:
            return f"{year:04d}-{int(values[1]):02d}-{int(values[2]):02d}", year
        return f"{year:04d}-{int(values[1]):02d}", year
    return f"{year:04d}", year


def _first_text(value: object) -> str | None:
    """First non-empty entry of a Crossref string array.

    Crossref returns `title` and `container-title` as arrays even when there is
    one entry. We take the first non-empty one and never concatenate: a second
    title is usually a translation or an alternative, and joining them would
    produce a string that is in neither the deposit nor reality.
    """
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _authors(value: object) -> list[Author]:
    if not isinstance(value, list):
        return []
    out: list[Author] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        given = str(entry.get("given") or "").strip()
        family = str(entry.get("family") or "").strip()
        affiliation = None
        affs = entry.get("affiliation")
        if isinstance(affs, list) and affs and isinstance(affs[0], dict):
            name = affs[0].get("name")
            affiliation = str(name) if name else None
        out.append(Author(
            given=given,
            family=family,
            name=" ".join(p for p in (given, family) if p),
            orcid=entry.get("ORCID") or None,
            affiliation=affiliation,
            # Crossref states the ordering hint; we record it rather than
            # re-deriving it, and a missing affiliation costs nothing.
            sequence=entry.get("sequence"),
        ))
    return out


def parse_work(
    message: dict,
    provider_name: str = "crossref",
    retrieved_at: str | None = None,
) -> Paper:
    """Map one Crossref work `message` onto our `Paper`.

    Free-standing: no provider, no transport, no network.
    """
    if not isinstance(message, dict):
        raise UpstreamSchemaError("Crossref message is not an object", provider=provider_name)

    now = retrieved_at or _now()
    raw_doi = message.get("DOI")
    doi = normalize_doi(raw_doi)

    dates: dict[str, tuple[str | None, int | None]] = {}
    for field in (*DATE_PRIORITY, DEPOSIT_DATE_FIELD):
        dates[field] = _date_string(message.get(field))

    chosen_field = next((f for f in DATE_PRIORITY if dates[f][0]), None)
    date_text, year = (dates[chosen_field] if chosen_field else (None, None))

    notes: list[str] = []
    stated = {f: dates[f][0] for f in DATE_PRIORITY if dates[f][0]}
    if len(set(stated.values())) > 1:
        # Conflicting dates are a fact about the deposit, not something to
        # average into a fictional "true" date.
        notes.append(
            f"crossref: conflicting publication dates "
            + "; ".join(f"{f}={v}" for f, v in stated.items())
            + f" -- selected {chosen_field} by CREA policy "
            + f"{DATE_POLICY_VERSION}, not by any Crossref ranking"
        )
    if dates[DEPOSIT_DATE_FIELD][0] and chosen_field != DEPOSIT_DATE_FIELD:
        notes.append(
            f"crossref: {DEPOSIT_DATE_FIELD} (deposit date) "
            f"{dates[DEPOSIT_DATE_FIELD][0]} is NOT used as the publication date"
        )

    paper = Paper(
        paper_id=doi or "",
        title=_first_text(message.get("title")),
        authors=_authors(message.get("author")),
        journal=_first_text(message.get("container-title")),
        year=year,
        volume=message.get("volume"),
        issue=message.get("issue"),
        pages=message.get("page"),
        url=message.get("URL"),
        identifiers=Identifiers(doi=doi),
        retrieved_at=now,
    )

    paper.upstream.publisher = message.get("publisher") or None
    paper.upstream.type = message.get("type") or None
    paper.upstream.publication_date = date_text
    paper.upstream.publication_date_type = chosen_field
    paper.upstream.publication_dates_raw = dict(stated)
    paper.upstream.date_policy_version = DATE_POLICY_VERSION
    paper.upstream.notes = notes
    issn = message.get("ISSN")
    if isinstance(issn, list):
        paper.upstream.issn = [str(x) for x in issn if x]
    count = message.get("is-referenced-by-count")
    if isinstance(count, int):
        paper.upstream.is_referenced_by_count = count

    abstract = message.get("abstract")
    if isinstance(abstract, str) and abstract.strip():
        # Kept verbatim. Crossref abstracts arrive as JATS/XML and we are not
        # going to strip tags inside a parser: a cleaned string and the deposit
        # would no longer be the same string, and the deposit is the part that
        # can be checked against.
        paper.abstracts.append(AbstractRecord(
            text=abstract,
            source=AbstractSource.CROSSREF,
            retrieved_at=now,
            raw_excerpt=abstract[:300],
        ))

    paper.access = _access(message, provider_name=provider_name, checked_at=now)

    # Attributed at parse time, so no code path can hold an unattributed Paper.
    if provider_name and provider_name not in paper.sources:
        paper.sources = [provider_name]
    if doi:
        paper.source_ids[provider_name] = doi
    if raw_doi and normalize_doi(raw_doi) != raw_doi:
        # The resolver form we were given is never lost.
        paper.raw_hashes[f"{provider_name}:raw_doi"] = content_hash(str(raw_doi), 12)
    return paper


def _access(
    message: dict,
    provider_name: str = "crossref",
    checked_at: str | None = None,
) -> AccessAssessment:
    """License and link metadata, recorded and deliberately not acted upon."""
    notes: list[str] = []
    evidence: list[str] = []

    licences = message.get("license")
    licence_info = LicenseInfo(applies_to="published_article", stated_by=[provider_name])
    if isinstance(licences, list) and licences and isinstance(licences[0], dict):
        first = licences[0]
        url = first.get("URL")
        licence_info.url = url
        licence_info.name = str(url) if url else "UNKNOWN"
        licence_info.content_version = first.get("content-version") or "UNKNOWN"
        # A licence WAS stated. That is not the same as having established that
        # it is a permissive SPDX licence for the version we would retrieve, and
        # `spdx_id` stays None so the policy cannot act on it.
        licence_info.is_known = bool(url)
        evidence.append("crossref:license")
        notes.append(
            f"crossref: license[0] content-version="
            f"{licence_info.content_version} url={url} -- a statement about the "
            f"deposit, not a retrieval permission"
        )
    else:
        licence_info = LicenseInfo(applies_to="published_article", stated_by=[])

    links = message.get("link")
    if isinstance(links, list):
        for index, link in enumerate(links):
            if not isinstance(link, dict):
                continue
            evidence.append(f"crossref:link[{index}]")
            notes.append(
                f"crossref: link[{index}] url={link.get('URL')} "
                f"content-type={link.get('content-type')} "
                f"content-version={link.get('content-version')} "
                f"intended-application={link.get('intended-application')}"
            )
        tdm = [
            l for l in links
            if isinstance(l, dict) and l.get("intended-application") == "text-mining"
        ]
        if tdm:
            notes.append(
                "crossref: link[] entries are scoped to text and data mining; "
                "the field bounds the link, it does not grant reuse or "
                "retrieval permission"
            )

    return AccessAssessment(
        # Crossref does not classify open access. Saying so is better than
        # leaving the field at a value we inferred from something else.
        oa_status=OAStatus.UNKNOWN,
        access_status=AccessStatus.NOT_CHECKED,
        # Never populated from link[]. A Crossref link is a TDM artefact, not
        # a location we are permitted to fetch. The links live in notes.
        fulltext_url=None,
        url_type="unknown",
        license=licence_info,
        notes=notes,
        evidence=evidence,
        checked_at=checked_at or _now(),
    )


class CrossrefProvider(BaseProvider):
    """Metadata retrieval against the Crossref REST API."""

    name = "crossref"
    # Crossref needs no credential. There is deliberately no API-key field:
    # inventing one would imply an authentication scheme the documentation does
    # not describe.
    requires_key = False
    capabilities = frozenset({
        Capability.LITERATURE_SEARCH,
        Capability.METADATA,
    })
    # No sleep, no retry, no assumed requests-per-second. Crossref's precise
    # rate limits are not a stable published contract, so this records nothing
    # it cannot justify. `RateSnapshot` carries whatever the response says.
    rate_limit = RateLimit(requests_per_second=1.0, burst=1, max_retries=0)

    def __init__(self, transport: Transport, mailto: str | None = None,
                 endpoint: str = WORKS_ENDPOINT, **kwargs) -> None:
        super().__init__(name=self.name, transport=transport, **kwargs)
        self._mailto = mailto or None
        self.endpoint = endpoint

    def _params(self) -> dict:
        return {"mailto": self._mailto} if self._mailto else {}

    # ------------------------------------------------------------------
    # request plumbing
    # ------------------------------------------------------------------

    def _check(self, response, url: str):
        code = response.status_code
        if 200 <= code < 300:
            return response.payload
        safe = redact_url(url)
        if code == 400:
            raise InvalidRequest(
                f"Crossref rejected the request (400): {safe}",
                provider=self.name, http_status=code)
        if code == 404:
            raise ResourceNotFound(
                f"Crossref has no such resource (404): {safe}",
                provider=self.name, http_status=code)
        if code in (401, 403):
            raise AuthenticationOrAuthorizationError(
                f"Crossref refused the request (HTTP {code}): {safe}",
                provider=self.name, http_status=code)
        if code == 429:
            raise RateLimited(
                f"Crossref returned 429: {safe}", provider=self.name, http_status=code)
        if 500 <= code < 600:
            raise UpstreamError(
                f"Crossref is failing upstream (HTTP {code}): {safe}",
                provider=self.name, http_status=code)
        raise ProviderError(
            f"unexpected Crossref status {code}: {safe}",
            provider=self.name, http_status=code)

    async def _get(self, url: str, params: dict | None = None):
        merged = {**(params or {}), **self._params()}
        response = await self.transport.request("GET", url, params=merged)
        payload = self._check(response, url)
        if not isinstance(payload, dict):
            raise InvalidUpstreamResponse(
                f"Crossref returned {type(payload).__name__}, expected an object",
                provider=self.name, http_status=response.status_code)
        return payload, RateSnapshot.from_headers(response.headers)

    # ------------------------------------------------------------------
    # operations
    # ------------------------------------------------------------------

    def _ingest(self, message: dict, query_text: str = "") -> Paper:
        paper = self.stamp(parse_work(message, provider_name=self.name,
                                      retrieved_at=self._now()))
        paper.access = self.report_access(paper.access)
        if query_text:
            paper.raw_hashes[f"{self.name}:query"] = content_hash(query_text, 12)
        return paper

    async def search(self, query: LiteratureQuery) -> ProviderResult:
        """Bibliographic search over `/works`.

        Field order is whatever Crossref returns. We do not re-rank: relevance
        scoring is a service-layer decision and this provider does not make it.
        """
        params = {
            "query.bibliographic": query.text,
            "rows": effective_rows(query.limit),
        }
        if query.doi:
            normalised = normalize_doi(query.doi)
            if normalised:
                # An exact identifier is a filter, not a search term.
                params.pop("query.bibliographic")
                params["filter"] = f"doi:{normalised}"
        payload, rate = await self._get(self.endpoint, params)
        message, items = self._items(payload)
        papers = [self._ingest(item, query.text) for item in items]
        return ProviderResult(
            provider=self.name,
            papers=papers,
            raw_hashes={self.name: _hash(payload)},
            warnings=[] if isinstance(message.get("total-results"), int) else
                     ["crossref: no total-results in the response"],
            retrieved_at=self._now(),
            complete=True,
            rate=rate,
        )

    async def get_metadata(self, ref: PaperRef) -> Paper | None:
        """Fetch one work by DOI, or a list by the query a PaperRef carries.

        A DOI that cannot be normalised returns None without a request, rather
        than being sent to `/works` as a search term -- a lookup and a search
        are different questions and pretending otherwise is how a typo becomes
        a confident wrong answer.
        """
        if ref.doi:
            normalised = normalize_doi(ref.doi)
            if not normalised:
                return None
            url = f"{self.endpoint}/{quote(normalised, safe='')}"
            payload, _rate = await self._get(url)
            message = payload.get("message")
            if not isinstance(message, dict):
                raise UpstreamSchemaError(
                    "Crossref work lookup returned no message object",
                    provider=self.name)
            return self._ingest(message)
        if ref.source_id:
            payload, _rate = await self._get(
                f"{self.endpoint}/{quote(str(ref.source_id), safe='')}")
            message = payload.get("message")
            if not isinstance(message, dict):
                raise UpstreamSchemaError(
                    "Crossref work lookup returned no message object",
                    provider=self.name)
            return self._ingest(message)
        return None

    async def get_by_doi(self, doi: str) -> Paper | None:
        """Convenience wrapper; `None` when the DOI normalises to nothing."""
        return await self.get_metadata(PaperRef(doi=doi))

    async def paginate(
        self,
        query: LiteratureQuery,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> AsyncIterator[Paper]:
        """Cursor pagination: `cursor=*`, then `message['next-cursor']`.

        Stops on a missing cursor, an empty cursor, an empty item list, a
        repeated cursor, or `max_pages`. That last brake is ours, not
        Crossref's, and is not described as an upstream limit.
        """
        rows = effective_rows(query.limit)
        cursor: str | None = "*"
        seen_cursors: set[str] = set()

        for _page in range(max(1, max_pages)):
            params = {
                "query.bibliographic": query.text,
                "rows": rows,
                "cursor": cursor,
            }
            payload, _rate = await self._get(self.endpoint, params)
            message, items = self._items(payload)
            for item in items:
                yield self._ingest(item, query.text)
            if not items:
                return
            nxt = message.get("next-cursor")
            if not nxt or not isinstance(nxt, str):
                return
            if nxt == cursor or nxt in seen_cursors:
                # A cursor that does not advance is an upstream fault. Stop
                # rather than loop on it for ever.
                return
            seen_cursors.add(nxt)
            cursor = nxt

    # ------------------------------------------------------------------
    # intentionally not implemented
    # ------------------------------------------------------------------

    async def get_access_assessment(self, doi: str) -> None:
        """NOT SUPPORTED. Crossref does not answer this question.

        Crossref states what a publisher deposited -- a licence record, a
        text-mining link. It does not classify open access, and it does not say
        whether any of it is ours to fetch. Answering that question is the job
        of an access provider, and the honest thing to return here is nothing.
        """
        return None

    # ------------------------------------------------------------------
    # parsing helpers
    # ------------------------------------------------------------------

    def _items(self, payload: dict) -> tuple[dict, list[dict]]:
        message = payload.get("message")
        if not isinstance(message, dict):
            raise UpstreamSchemaError(
                "Crossref response has no 'message' object", provider=self.name)
        items = message.get("items")
        if items is None:
            # A single-work response has no items; that is a shape difference,
            # not a fault, and the caller decides which it expected.
            return message, []
        if not isinstance(items, list):
            raise UpstreamSchemaError(
                f"Crossref 'items' is {type(items).__name__}, expected a list",
                provider=self.name)
        for item in items:
            if not isinstance(item, dict):
                raise UpstreamSchemaError(
                    "Crossref returned a non-object inside 'items'",
                    provider=self.name)
        return message, items

    def _now(self) -> str:
        return _now()


def _now() -> str:
    from crea.core.provenance import utc_now_iso

    return utc_now_iso()


def _hash(payload: object) -> str:
    return content_hash(json.dumps(payload, sort_keys=True, ensure_ascii=False), 16)

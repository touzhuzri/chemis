"""Unpaywall provider. OA availability and location metadata.

The third provider, and the one that actually answers the access question --
which is why the other two cannot.

> **An OA location is not a permission to fetch it.**
> Unpaywall reports where an open-access copy lives. Whether that copy carries a
> licence that permits *us* to retrieve and keep it is a separate question, and
> the answer is frequently "no information": a `gold` record was observed on the
> live API carrying `license: null`. This module records the location as a fact
> and lets `core.policy.access` decide the permission, fail-closed.

**Nothing here fetches a location.** No `url_for_pdf`, no `url`, no redirect
following, no repository crawling. Recording where something is and retrieving
it are two operations and this module does the first.

Contract: `docs/PHASE2_PROVIDER_CONTRACT.md` §4. Field structure is
LIVE_RESPONSE_VERIFIED; **rate limits, terms, data licence and the prose
definition of `bronze` remain UNVERIFIED** -- Unpaywall's documentation site is
a client-rendered application whose every path returns an identical JavaScript
shell, and nothing in this module assumes any of it.
"""

from __future__ import annotations

import json
from typing import Any

from crea.core.errors import (
    AuthenticationOrAuthorizationError,
    InvalidRequest,
    InvalidUpstreamResponse,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
    ResourceNotFound,
    UpstreamError,
    UpstreamSchemaError,
)
from crea.core.ids import content_hash, normalize_doi
from crea.core.models.literature import (
    AccessAssessment,
    AccessStatus,
    Author,
    Identifiers,
    LicenseInfo,
    OAStatus,
    Paper,
)
from crea.providers.base import (
    Availability,
    Capability,
    LiteratureQuery,
    PaperRef,
    ProviderStatus,
)
from crea.sources.base import (
    BaseProvider,
    ProviderResult,
    RateLimit,
    RateSnapshot,
    Transport,
    redact_url,
)

#: A template, not a base: a DOI is interpolated into the path.
LOOKUP_ENDPOINT = "https://api.unpaywall.org/v2/{doi}"

#: A missing credential is a normal state that degrades the answer. It is not an
#: error, and it is not a reason to fail the whole retrieval.
MISSING_CREDENTIAL_REASON = (
    "Unpaywall requires an email address; set CREA_UNPAYWALL_EMAIL"
)

#: Location keys, in the order the live response presented them. Recorded so a
#: location can be described without guessing which key produced which URL.
LOCATION_KEYS = (
    "url", "url_for_landing_page", "url_for_pdf",
    "license", "host_type", "version", "oa_date",
    "pmh_id", "endpoint_id", "repository_institution", "is_best",
    "evidence", "updated",
)

_TOP_LEVEL = (
    "doi", "title", "year", "published_date", "journal_name", "journal_issn_l",
    "journal_issns", "publisher", "genre", "is_oa", "oa_status",
    "best_oa_location", "first_oa_location", "oa_locations",
    "oa_locations_embargoed", "has_repository_copy", "journal_is_oa",
    "journal_is_in_doaj", "is_paratext", "z_authors", "updated",
    "data_standard", "doi_url",
)


def _oa_status(value: object) -> OAStatus:
    """Map an upstream string onto `OAStatus`, defaulting to UNKNOWN.

    An unrecognised value is never coerced into a status that has a policy
    attached to it. `diamond` exists in the enum because OpenAlex returns it;
    inventing a policy group for a status we have not read the definition of
    would be exactly the wrong move.
    """
    try:
        return OAStatus(str(value).lower())
    except ValueError:
        return OAStatus.UNKNOWN


def parse_location(location: object) -> tuple[str | None, str, dict]:
    """Return (pdf-or-landing url, url_type, described keys) for one location.

    A location is recorded, never retrieved. Which of `url`,
    `url_for_landing_page` or `url_for_pdf` held the value is kept, because
    "a link exists" and "a PDF link exists" are different facts and the report
    should not flatten them.
    """
    if not isinstance(location, dict):
        return None, "unknown", {}
    described = {k: location[k] for k in LOCATION_KEYS if location.get(k) is not None}
    if location.get("url_for_pdf"):
        return location["url_for_pdf"], "pdf", described
    if location.get("url_for_landing_page"):
        return location["url_for_landing_page"], "landing_page", described
    if location.get("url"):
        return location["url"], "landing_page", described
    return None, "unknown", described


def parse_response(
    payload: dict,
    provider_name: str = "unpaywall",
    retrieved_at: str | None = None,
) -> tuple[Paper, AccessAssessment]:
    """Map one Unpaywall response onto a Paper and its access assessment.

    Unpaywall is thin on bibliographic fields and rich on access fields, so it
    is a poor metadata source and a good access source. The Paper it yields is
    explicitly that: enough to identify the work, not enough to describe it.
    """
    if not isinstance(payload, dict):
        raise UpstreamSchemaError("Unpaywall payload is not an object",
                                  provider=provider_name)

    now = retrieved_at or _now()
    doi = normalize_doi(payload.get("doi"))
    raw_status = payload.get("oa_status")

    paper = Paper(
        paper_id=doi or "",
        title=(str(payload["title"]) if payload.get("title") else None),
        authors=[Author(name=(a or {}).get("raw_author_name", ""))
                 for a in (payload.get("z_authors") or [])
                 if isinstance(a, dict)],
        journal=payload.get("journal_name") or None,
        year=payload.get("year") if isinstance(payload.get("year"), int) else None,
        url=payload.get("doi_url") or None,
        identifiers=Identifiers(doi=doi),
        retrieved_at=now,
    )
    if provider_name and provider_name not in paper.sources:
        paper.sources = [provider_name]
    if doi:
        paper.source_ids[provider_name] = doi
        paper.raw_hashes[f"{provider_name}:raw_doi"] = content_hash(
            str(payload.get("doi")), 12)

    issn = payload.get("journal_issn_l")
    paper.upstream.issn = [issn] if isinstance(issn, str) and issn else []
    paper.upstream.type = payload.get("genre") or None
    paper.upstream.publisher = payload.get("publisher") or None
    paper.upstream.publication_date = payload.get("published_date") or None

    fulltext_url, url_type, described = parse_location(payload.get("best_oa_location"))
    licence = (payload.get("best_oa_location") or {}).get("license")

    notes: list[str] = []
    evidence = ["unpaywall:oa_status"]

    if raw_status is not None and _oa_status(raw_status) is OAStatus.UNKNOWN \
            and str(raw_status).lower() != "unknown":
        # The raw string is kept. An unmodelled status is a fact about the
        # upstream data, and dropping it would make the record look as though
        # Unpaywall had said nothing.
        # `key=value` so a downstream layer can read the raw string back
        # without parsing prose. The prose around it is for a human.
        notes.append(f"unpaywall: oa_status_raw={raw_status}")
        notes.append(
            f"unpaywall: oa_status {raw_status!r} is not a modelled status; "
            f"recorded verbatim and treated as UNKNOWN"
        )
    if payload.get("is_oa") is False and _oa_status(raw_status) is not OAStatus.CLOSED:
        notes.append("unpaywall: is_oa=false but oa_status is not 'closed'")
    if (payload.get("best_oa_location") or {}) and not licence:
        # The one fact this provider most often withholds, called out so that
        # UNKNOWN is not mistaken for "no licence exists".
        notes.append("unpaywall: an OA location was reported with license=null; "
                     "absence of licence metadata is not a prohibition and is "
                     "not a permission")
    for key, value in described.items():
        if key in ("url", "url_for_landing_page", "url_for_pdf"):
            continue
        notes.append(f"unpaywall: best_oa_location.{key}={value}")
    if payload.get("oa_locations"):
        notes.append(f"unpaywall: {len(payload['oa_locations'])} further OA location(s) reported")

    assessment = AccessAssessment(
        oa_status=_oa_status(raw_status),
        oa_status_raw=str(raw_status) if raw_status is not None else None,
        access_status=AccessStatus.NOT_CHECKED,
        # A URL is recorded because it is a fact about where an OA copy lives.
        # Whether we may fetch it is decided by policy, and with license=null
        # that decision is UNKNOWN.
        fulltext_url=fulltext_url,
        url_type=url_type,
        oa_version=str(described.get("version") or "UNKNOWN"),
        license=LicenseInfo(
            spdx_id=licence,
            name=licence or "UNKNOWN",
            url=licence,
            content_version=str(described.get("version") or "UNKNOWN"),
            applies_to="oa_copy",
            is_known=bool(licence),
            stated_by=[provider_name] if licence else [],
        ),
        notes=notes,
        evidence=evidence,
        checked_at=now,
    )
    # The record is self-contained like every other parser's; the tuple return
    # just spares a caller that only wanted the assessment.
    paper.access = assessment
    return paper, assessment


class UnpaywallProvider(BaseProvider):
    """OA availability and location lookup by DOI.

    No search: Unpaywall resolves an identifier it is given and offers no way to
    discover one. Saying so is part of implementing the contract honestly.
    """

    name = "unpaywall"
    # An email address is required by the API. Absent, this provider is
    # `missing_key` -- a normal state that degrades the answer, never a crash.
    requires_key = True
    capabilities = frozenset({Capability.OPEN_ACCESS, Capability.METADATA})
    # No sleep, no retry, no assumed rate. Unpaywall's rate limit is UNVERIFIED
    # and is therefore neither invented nor relied upon.
    rate_limit = RateLimit(requests_per_second=1.0, burst=1, max_retries=0)

    def __init__(self, transport: Transport, email: str | None = None,
                 endpoint: str = LOOKUP_ENDPOINT, **kwargs) -> None:
        super().__init__(name=self.name, transport=transport, **kwargs)
        self._email = email or None
        self.endpoint = endpoint

    def status(self) -> ProviderStatus:
        if not self._email:
            return ProviderStatus(self.name, Availability.MISSING_KEY,
                                  MISSING_CREDENTIAL_REASON)
        return ProviderStatus(self.name, Availability.AVAILABLE)

    # ------------------------------------------------------------------

    def _check(self, response, url: str):
        code = response.status_code
        if 200 <= code < 300:
            return response.payload
        safe = redact_url(url)
        if code == 400:
            raise InvalidRequest(f"Unpaywall rejected the request (400): {safe}",
                                provider=self.name, http_status=code)
        if code == 404:
            # The body is HTML, not JSON. The answer is "no such DOI", not an
            # outage, and must not be reported as one.
            raise ResourceNotFound(
                f"Unpaywall has no record for this DOI (404): {safe}",
                provider=self.name, http_status=code)
        if code in (401, 403):
            raise AuthenticationOrAuthorizationError(
                f"Unpaywall refused the request (HTTP {code}): {safe}",
                provider=self.name, http_status=code)
        if code == 422:
            # Observed: {"error": true, "message": "Email address required..."}
            raise InvalidRequest(
                f"Unpaywall requires an email address: {safe}",
                provider=self.name, http_status=code)
        if code == 429:
            raise RateLimited(f"Unpaywall returned 429: {safe}",
                             provider=self.name, http_status=code)
        if 500 <= code < 600:
            raise UpstreamError(
                f"Unpaywall is failing upstream (HTTP {code}): {safe}",
                provider=self.name, http_status=code)
        raise ProviderError(f"unexpected Unpaywall status {code}: {safe}",
                            provider=self.name, http_status=code)

    async def _get(self, doi: str):
        normalised = normalize_doi(doi)
        if not normalised:
            return None, None
        url = self.endpoint.format(doi=normalised)
        response = await self.transport.request(
            "GET", url, params={"email": self._email} if self._email else {})
        payload = self._check(response, url)
        if not isinstance(payload, dict):
            raise InvalidUpstreamResponse(
                f"Unpaywall returned {type(payload).__name__}, expected an object",
                provider=self.name, http_status=response.status_code)
        return payload, RateSnapshot.from_headers(response.headers)

    # ------------------------------------------------------------------

    def _now(self) -> str:
        return _now()

    async def get_access_assessment(self, doi: str) -> AccessAssessment | None:
        """The question this provider exists to answer."""
        if not self.status().usable:
            raise ProviderUnavailable(self.status().reason)
        payload, rate = await self._get(doi)
        if payload is None:
            return None
        _paper, assessment = parse_response(payload, provider_name=self.name,
                                             retrieved_at=self._now())
        return self.report_access(assessment)

    async def get_metadata(self, ref: PaperRef) -> Paper | None:
        """Unpaywall has no search and no ID lookup: a DOI is the whole address."""
        if not ref.doi:
            return None
        if not self.status().usable:
            raise ProviderUnavailable(self.status().reason)
        payload, _rate = await self._get(ref.doi)
        if payload is None:
            return None
        paper, assessment = parse_response(payload, provider_name=self.name,
                                          retrieved_at=self._now())
        paper.access = self.report_access(assessment)
        return self.stamp(paper)

    async def get_by_doi(self, doi: str) -> Paper | None:
        return await self.get_metadata(PaperRef(doi=doi))

    async def search(self, query: LiteratureQuery) -> ProviderResult:
        """NOT SUPPORTED. Unpaywall resolves identifiers; it does not find them.

        Discovery is OpenAlex's and Crossref's job. Returning an empty result
        here would be indistinguishable from "your query found nothing", which
        is a different and wrong answer.
        """
        raise ProviderUnavailable(
            "Unpaywall has no search endpoint; it resolves a DOI it is given. "
            "Use OpenAlex or Crossref to find candidates."
        )


def _now() -> str:
    from crea.core.provenance import utc_now_iso

    return utc_now_iso()


def _hash(payload: Any) -> str:
    return content_hash(json.dumps(payload, sort_keys=True, ensure_ascii=False), 16)

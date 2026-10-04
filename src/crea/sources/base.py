"""Provider adapters. Transport seam and shared machinery. No endpoint.

No HTTP endpoint is implemented in this phase, and the reason is recorded in
`docs/PHASE2_DESIGN.md` §0: the API details are unverified beliefs, and writing
a client against a guessed endpoint produces something that looks like an
integration and is not one. The `_search_endpoint` / `_endpoint` declarations
below are placeholders to be filled in from the official documentation.

What *is* real here is the discipline the boundary enforces:

* A provider reports; it does not decide. Merge precedence, dedup, permission
  inference and ranking live in `core.policy` and `services`. An adapter is not
  a place for policy to hide.
* A provider may set `oa_status` and `fulltext_url`. It may not set
  `permission`; that field is policy's to fill.
* `get_fulltext` consults the retrieval gate and cannot reach a transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from crea.core.models.literature import (
    AccessAssessment,
    Paper,
    RetrievalAttempt,
)
from crea.core.policy.access import gate_retrieval, infer_permission
from crea.providers.base import (
    Availability,
    Capability,
    LiteratureQuery,
    PaperRef,
    ProviderStatus,
)


# ---------------------------------------------------------------------------
# transport seam
# ---------------------------------------------------------------------------


class Response:
    """What a transport returns. Deliberately not an httpx response.

    Keeping the return type our own is what lets the offline suite be
    structurally incapable of reaching the network: there is no httpx object
    anywhere in the signature.
    """

    def __init__(
        self,
        status_code: int,
        payload: object,
        url: str = "",
        headers: object | None = None,
    ) -> None:
        self.status_code = status_code
        self.payload = payload
        self.url = url
        self.headers = headers

    def header(self, name: str) -> str | None:
        if self.headers is None:
            return None
        try:
            return self.headers.get(name)
        except AttributeError:
            return None


class Transport:
    """Interface every transport implements. One method; no real one exists yet.

    Three body/argument carriers, because the endpoints that use this seam
    disagree about which they want: `params` for a query string, `json` for a
    JSON body, and `data` for a form body. PUG REST takes a form body for
    SMILES and InChI lookups and has no JSON form for them, so without `data`
    the only alternative is putting a SMILES into a URL path -- which mangles
    every branched structure and is exactly the sort of silent wrong-answer
    this seam exists to prevent.
    """

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: object | None = None,
        data: dict | None = None,
    ) -> Response:
        raise NotImplementedError(
            "no real transport in this phase; see docs/PHASE2_DESIGN.md §0"
        )


@dataclass(frozen=True)
class RateLimit:
    requests_per_second: float = 1.0
    burst: int = 1
    max_retries: int = 3
    backoff_seconds: float = 1.0


class RateSnapshot(BaseModel):
    """Whatever the provider told us about cost and budget on this response.

    Every field is optional and every one of them is recorded verbatim. We do
    not compute a budget, do not sleep, and do not assume any of these headers
    exist on any given call -- the provider documents them for its API, and a
    field that is absent is recorded as absent rather than defaulted to a number
    we made up.

    The monetary fields are stored as strings because they are provider-
    formatted decimal values, not numbers we have agreed a precision for.
    """

    limit_usd: str | None = None
    remaining_usd: str | None = None
    cost_usd: str | None = None
    limit: str | None = None
    remaining: str | None = None
    reset_seconds: str | None = None
    headers_seen: list[str] = Field(default_factory=list)

    @classmethod
    def from_headers(cls, headers) -> "RateSnapshot":
        get = (lambda k: headers.get(k)) if headers is not None else (lambda k: None)
        known = [
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
            "X-RateLimit-Limit-USD",
            "X-RateLimit-Remaining-USD",
            "X-RateLimit-Cost-USD",
        ]
        return cls(
            limit_usd=get("X-RateLimit-Limit-USD"),
            remaining_usd=get("X-RateLimit-Remaining-USD"),
            cost_usd=get("X-RateLimit-Cost-USD"),
            limit=get("X-RateLimit-Limit"),
            remaining=get("X-RateLimit-Remaining"),
            reset_seconds=get("X-RateLimit-Reset"),
            headers_seen=[h for h in known if get(h) is not None],
        )


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


class ProviderResult(BaseModel):
    """A provider's answer, including the fact that it was partial.

    `warnings` and `complete` exist so a truncated or degraded response is
    representable rather than indistinguishable from a complete one.
    """

    provider: str
    papers: list[Paper] = Field(default_factory=list)
    raw_hashes: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    retrieved_at: str = ""
    complete: bool = True
    rate: RateSnapshot | None = None


#: Query parameters whose value must never reach a log line, an error message or
#: a stored record. OpenAlex accepts its key as `?api_key=`, so a URL is a
#: credential carrier and a URL is exactly what error messages love to quote.
#:
#: `mailto` is here even though it is not a credential. It is a person's
#: contact address, supplied by the operator, and it has no business in a log.
SECRET_QUERY_PARAMS = frozenset({"api_key", "email", "mailto", "token", "key"})


def redact_url(url: str) -> str:
    """Strip credential-bearing query values from a URL for display."""
    if not url or "?" not in url:
        return url
    base, _, query = url.partition("?")
    parts = []
    for chunk in query.split("&"):
        name, sep, _value = chunk.partition("=")
        parts.append(f"{name}=<redacted>" if sep and name.lower() in SECRET_QUERY_PARAMS else chunk)
    return f"{base}?{'&'.join(parts)}"


# ---------------------------------------------------------------------------
# shared base
# ---------------------------------------------------------------------------


@dataclass
class BaseProvider:
    """Machinery every provider shares. Owns no endpoint and no policy.

    A subclass implements `_parse_search` and `_parse_metadata`. Those are the
    only places a URL appears in the codebase, which makes the pending
    documentation check a two-method task per provider.
    """

    name: str
    transport: Transport
    requires_key: bool = False
    capabilities: frozenset = frozenset()
    rate_limit: RateLimit = field(default_factory=RateLimit)
    missing_key_reason: str = ""

    # -- endpoint declarations: PLACEHOLDERS, not specifications -----------
    _search_endpoint: str = ""
    _metadata_endpoint: str = ""

    def status(self) -> ProviderStatus:
        if not self.requires_key:
            return ProviderStatus(self.name, Availability.AVAILABLE)
        return ProviderStatus(
            self.name, Availability.MISSING_KEY, self.missing_key_reason
        )

    # -- payload -> model ---------------------------------------------------

    def _parse_search(self, payload: object) -> ProviderResult:
        raise NotImplementedError("endpoint and payload shape unverified; see design §7")

    def _parse_metadata(self, payload: object) -> Paper:
        raise NotImplementedError("endpoint and payload shape unverified; see design §7")

    # -- reporting ----------------------------------------------------------

    def stamp(self, paper: Paper) -> Paper:
        """Attribute a record. There is no path that yields an unattributed Paper."""
        if self.name not in paper.sources:
            paper.sources = [*paper.sources, self.name]
        return paper

    def report_access(self, assessment: AccessAssessment) -> AccessAssessment:
        """Record what the provider saw, then let policy decide permission.

        The provider is not permitted to set `permission` or `access_status`; it
        states `oa_status`, the licence and the location, and policy infers the
        rest. That separation is what stops an adapter from deciding.
        """
        assessment.source = self.name
        return infer_permission(assessment)

    # -- retrieval: reserved, and gated -------------------------------------

    async def get_fulltext(self, paper: Paper, at: str = "") -> RetrievalAttempt:
        """RESERVED. Consults the gate and cannot reach the transport.

        Returning a permitted `NOT_ATTEMPTED` rather than fetching is the
        intended behaviour for this phase. The branch that would need a real
        transport does not exist, so there is nothing to accidentally trigger.
        """
        return gate_retrieval(paper.access, paper.paper_id, at or paper.retrieved_at)

    def can_retrieve(self, paper: Paper) -> bool:
        return paper.access.permission.value == "may_retrieve"


__all__ = [
    "Availability",
    "BaseProvider",
    "Capability",
    "LiteratureQuery",
    "PaperRef",
    "ProviderResult",
    "ProviderStatus",
    "RateLimit",
    "Response",
    "Transport",
]

"""The one module in the codebase allowed to touch the network.

`tests/architecture/test_no_network.py` names this file explicitly. Everything
else -- chemistry, policy, models, services -- reaches it through the `Transport`
seam, which is what keeps the default test suite structurally offline and keeps
a credential from leaking into a core module by accident.

Nothing here knows what OpenAlex or Crossref look like. It moves bytes and
hands back a `Response`.
"""

from __future__ import annotations

from crea.core.errors import (
    InvalidUpstreamResponse,
    ProviderError,
    ProviderUnavailable,
)
from crea.sources.base import Response, Transport

#: Sent on every request. Provider etiquette asks clients to identify
#: themselves; the contact address is only included when the operator supplied
#: one, and never as a query parameter.
USER_AGENT = "crea-chemical-research-agent/0.1"


class HttpxTransport(Transport):
    """Real HTTP. Only a provider may construct one, and only outside tests."""

    def __init__(
        self,
        timeout_seconds: float = 30.0,
        user_agent: str = USER_AGENT,
        client: object | None = None,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ProviderUnavailable(
                "httpx is required for live provider access; the offline suite "
                "never needs it"
            ) from exc
        self._httpx = httpx
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )
        self.user_agent = user_agent

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: object | None = None,
        data: dict | None = None,
    ) -> Response:
        try:
            raw = await self._client.request(
                method, url, params=params, json=json, data=data
            )
        except Exception as exc:  # noqa: BLE001 - httpx raises a family of these
            raise ProviderError(f"transport failure for {method} {url}: {exc}") from exc
        return Response(
            status_code=raw.status_code,
            payload=_decode(raw),
            url=str(raw.url),
            headers=raw.headers,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _decode(raw) -> object:
    """Parse a JSON body, or hand back the text so the caller can say so."""
    try:
        return raw.json()
    except Exception:  # noqa: BLE001 - a non-JSON body is a real possibility
        return getattr(raw, "text", "")


def require_json(response: Response, provider: str) -> object:
    """Turn a non-JSON body into a typed error instead of a confusing one."""
    if not isinstance(response.payload, (dict, list)):
        raise InvalidUpstreamResponse(
            f"{provider} returned a non-JSON body "
            f"(status {response.status_code}); first 120 chars: "
            f"{str(response.payload)[:120]!r}",
            provider=provider,
            http_status=response.status_code,
        )
    return response.payload

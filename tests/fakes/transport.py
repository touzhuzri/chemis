"""Offline transport. The seam that makes the default suite unable to reach
the network.

`FakeTransport` implements the same single method as `crea.sources.base.Transport`
and records every call. A test that expects a request to *not* happen asserts
on `calls` -- which is how the retrieval gate is tested without a single byte
leaving the machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crea.sources.base import Response


@dataclass
class FakeTransport:
    """Routes (method, url-substring) to a canned response. Records all calls."""

    routes: dict[tuple[str, str], object] = field(default_factory=dict)
    sequences: dict[tuple[str, str], list] = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)

    def route(self, method: str, url: str, status_code: int, payload: object,
              headers: dict | None = None) -> "FakeTransport":
        self.routes[(method.upper(), url)] = (status_code, payload, headers or {})
        return self

    def route_sequence(self, method: str, url: str, responses: list) -> "FakeTransport":
        """Serve a different response on each successive call to the same URL.

        Cursor pagination moves in `params`, not in the URL, so a single static
        route cannot express "page two looks different". A sequence can.
        """
        self.sequences[(method.upper(), url)] = list(responses)
        return self

    def route_error(self, method: str, url: str, status_code: int) -> "FakeTransport":
        return self.route(method, url, status_code, {"error": f"http {status_code}"})

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: object | None = None,
        data: dict | None = None,
    ) -> Response:
        self.calls.append({
            "method": method.upper(), "url": url,
            "params": params or {}, "json": json, "data": data or {},
        })
        key = (method.upper(), url)
        route = self.routes.get(key)
        if route is None:
            # Longest registered URL wins, so a list route cannot swallow a more
            # specific one (`/works` must not answer `/works/{doi}`).
            best = None
            for (m, fragment), value in self.routes.items():
                if m == method.upper() and fragment in url:
                    if best is None or len(fragment) > len(best[0]):
                        best = (fragment, value)
            if best is not None:
                route = best[1]
        seq = self.sequences.get(key)
        if seq is not None:
            entry = seq.pop(0) if len(seq) > 1 else seq[0]
            status_code, payload, headers = entry
            return Response(status_code, payload, url=url, headers=headers)
        if route is not None:
            status_code, payload, headers = route
            return Response(status_code, payload, url=url, headers=headers)
        raise AssertionError(
            f"FakeTransport has no route for {method} {url}. "
            "A real network call was attempted in an offline test."
        )

    # -- assertions --------------------------------------------------------

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def assert_no_calls(self) -> None:
        assert self.calls == [], (
            f"expected no network calls, got {len(self.calls)}: "
            f"{[c['url'] for c in self.calls]}"
        )


class ExplodingTransport:
    """Fails loudly if anything tries to use it.

    Used to prove a code path is unreachable rather than merely unused.
    """

    def __init__(self) -> None:
        self.touched = False

    async def request(self, method: str, url: str, **_: object) -> Response:
        self.touched = True
        raise AssertionError(f"ExplodingTransport was called: {method} {url}")

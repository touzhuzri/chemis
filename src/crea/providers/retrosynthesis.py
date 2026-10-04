"""Retrosynthesis: the provider boundary.

A retrosynthesis engine proposes routes. It does not know whether they work, it
does not know whether the literature reports them, and it has never seen this
project's Evidence. So the boundary here is narrow on purpose:

    RetrosynthesisRequest -> RetrosynthesisResult

A provider reports a status even when it produced nothing:

    AVAILABLE        ran and returned routes
    UNAVAILABLE      not installed here
    NOT_CONFIGURED   installed, but no model / stock / config
    FAILED           ran and errored

None of those is a reason to invent a route. A missing engine produces an empty
`RetrosynthesisResult` with a reason, and the agent sees that.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from crea.core.models.route import (
    CandidateRoute,
    ProviderProvenance,
    ProviderReason,
    ProviderStatus,
    RetrosynthesisRequest,
    RetrosynthesisResult,
)


@runtime_checkable
class RetrosynthesisProvider(Protocol):
    name: str
    version: str | None

    def availability(self) -> ProviderStatus:
        ...

    def retrosynthesize(
        self, request: RetrosynthesisRequest
    ) -> RetrosynthesisResult:
        ...


class BaseRetrosynthesisProvider:
    """Shared bookkeeping: identity, status, provenance."""

    name = "base"
    version: str | None = None

    def availability(self) -> ProviderStatus:
        return ProviderStatus.UNAVAILABLE

    def provenance(self, **overrides) -> ProviderProvenance:
        data = {
            "provider": self.name,
            "provider_version": self.version,
            "generated_at": _now(),
        }
        data.update(overrides)
        return ProviderProvenance(**data)

    def empty_result(
        self,
        request: RetrosynthesisRequest,
        status: ProviderStatus,
        reason: str,
        reason_code: ProviderReason = ProviderReason.UNKNOWN,
        **overrides,
    ) -> RetrosynthesisResult:
        return RetrosynthesisResult(
            request=request,
            status=status,
            routes=[],
            reason=reason,
            reason_code=reason_code,
            provenance=self.provenance(**overrides),
            warnings=[reason] if reason else [],
        )

    def retrosynthesize(
        self, request: RetrosynthesisRequest
    ) -> RetrosynthesisResult:  # pragma: no cover - overridden
        raise NotImplementedError


def _now() -> str:
    from crea.core.provenance import utc_now_iso

    return utc_now_iso()


__all__ = [
    "BaseRetrosynthesisProvider",
    "CandidateRoute",
    "ProviderProvenance",
    "ProviderStatus",
    "RetrosynthesisProvider",
    "RetrosynthesisRequest",
    "RetrosynthesisResult",
]
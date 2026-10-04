"""Bounded retry for provider calls (CREA Stage 4.2).

Providers deliberately do not retry: a 429 is surfaced with its budget
headers, because only the caller knows whether it can afford to wait. This is
the caller's retry policy -- bounded, backing off exponentially, and honouring
the server's own `Retry-After` when it sent one. It retries only transient
failures (rate limits and upstream errors), never a genuine answer like
`NOT_FOUND` or `INVALID_REQUEST`.

Default is *no retry* (`max_retries=0`), which preserves the existing
provider contract exactly; a composition root opts in.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from crea.core.errors import ProviderError, RateLimited, UpstreamError

#: The transient failures a retry may help with. A 404 or a 400 never appears
#: here -- retrying an answer just asks the same question again.
_TRANSIENT = (RateLimited, UpstreamError)


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to retry, and how long to wait between attempts."""

    max_retries: int = 0
    base_backoff_seconds: float = 1.0
    #: A ceiling, so a server screaming `Retry-After: 3600` cannot park a run.
    max_backoff_seconds: float = 30.0

    @property
    def enabled(self) -> bool:
        return self.max_retries > 0


def _wait_seconds(exc: ProviderError, policy: RetryPolicy, attempt: int) -> float:
    """The server's Retry-After if it sent one, else exponential backoff."""
    if getattr(exc, "retry_after", None) is not None:
        wait = float(exc.retry_after)
    else:
        wait = policy.base_backoff_seconds * (2 ** attempt)
    return max(0.0, min(wait, policy.max_backoff_seconds))


def call_with_retry(fn, policy: RetryPolicy, *, sleep=time.sleep):
    """Run `fn`, retrying transient provider failures per `policy`.

    With `policy.max_retries == 0` this calls `fn` exactly once and lets any
    error propagate -- the unchanged, default behaviour.
    """
    for attempt in range(policy.max_retries + 1):
        try:
            return fn()
        except _TRANSIENT as exc:
            if attempt >= policy.max_retries:
                raise
            sleep(_wait_seconds(exc, policy, attempt))
    # unreachable: the loop returns or raises
    raise ProviderError("retry loop exhausted", provider="retry")


__all__ = ["RetryPolicy", "call_with_retry"]

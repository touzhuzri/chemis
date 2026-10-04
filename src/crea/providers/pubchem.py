"""PubChem as a `CompoundProvider`. The seam, not the endpoint.

`crea.sources.pubchem` knows PUG REST: URLs, payload shapes, PUG REST's own
error dialect. This module knows none of that. It sits on the `CompoundProvider`
Protocol that Phase 2 reserved, and its whole job is to make PubChem's answers
conform to the contract the rest of the system codes against:

* ``[]`` is an **absence** -- the source was consulted and holds no such entry.
* a raise is a **failure** -- the source could not be consulted, or answered
  wrongly. Never the other way round.

That single distinction is what lets `services.identity` report `NOT_FOUND` and
`UNAVAILABLE` as different things. A provider that swallows a timeout into an
empty list makes an unreachable database look like an empty one, and the agent
then acts on a fact nobody established.

> **A PubChem record is chemical identity evidence and nothing else.** It is
> not literature evidence, it is not experimental evidence, and nothing in
> `crea.core.models.identity` can carry a reaction claim. Phase 3.5's evidence
> ladder is untouched by this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crea.core.errors import ProviderError, ProviderUnavailable, ResourceNotFound
from crea.core.models.identity import ChemicalIdentityCandidate
from crea.providers.base import (
    Availability,
    Capability,
    CompoundProvider,
    CompoundQuery,
    ProviderStatus,
)
from crea.sources.base import Transport
from crea.sources.pubchem import PubChemSource


@dataclass
class PubChemProvider:
    """PubChem behind the `CompoundProvider` Protocol.

    Constructed over a `Transport`, so the offline suite can hand it a fake and
    the live path can hand it `HttpxTransport`. Nothing here reaches the
    network directly, and nothing here is imported by `crea.chem`.
    """

    transport: Transport | None = None
    source: PubChemSource | None = None
    name: str = "pubchem"
    requires_key: bool = False
    capabilities: frozenset[Capability] = frozenset(
        {Capability.COMPOUND_LOOKUP, Capability.STRUCTURE_SEARCH}
    )
    _reason: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.source is None:
            if self.transport is None:
                self._reason = (
                    "no transport was supplied, so PubChem cannot be reached; "
                    "this is a configuration state, not a chemistry result"
                )
                return
            self.source = PubChemSource(transport=self.transport)

    # -- availability -----------------------------------------------------

    def status(self) -> ProviderStatus:
        """Report, do not raise. An unconfigured provider is a normal state.

        PUG REST requires no API key, so `MISSING_KEY` is never the answer
        here. An absent *transport* is, and it is reported as `ERROR` with a
        reason in words rather than discovered later as a stack trace.
        """
        if self.source is None:
            return ProviderStatus(self.name, Availability.ERROR, self._reason)
        return self.source.status()

    def usable(self) -> bool:
        return self.status().availability is Availability.AVAILABLE

    # -- the Protocol -----------------------------------------------------

    def lookup(self, query: CompoundQuery) -> list[ChemicalIdentityCandidate]:
        """Records for this query, or `[]` when the source holds no such entry.

        The one place in Phase 3.6 where a typed absence becomes an empty list,
        because the Protocol says it must. Every other typed error propagates
        untouched so that the caller can tell a 429 from a name nobody uses.
        """
        status = self.status()
        if status.availability is not Availability.AVAILABLE:
            # A provider that cannot answer says so. It does not answer "nothing".
            raise ProviderUnavailable(
                f"{self.name} is not usable: {status.reason or status.availability.value}"
            )

        try:
            return list(self.source.lookup(query))
        except ResourceNotFound:
            # Absence is an answer. This is the *only* error that becomes `[]`.
            return []
        except ProviderError:
            # Rate limits, timeouts, 5xx, bad payloads: all of these are
            # "we could not find out", and none of them may be reported as
            # "there is nothing there". Let them reach the caller as typed.
            raise
        except Exception as exc:  # noqa: BLE001 - anything else is our bug
            raise ProviderError(
                f"{self.name} lookup failed unexpectedly: {exc}", provider=self.name
            ) from exc


__all__ = ["PubChemProvider", "CompoundProvider", "CompoundQuery"]

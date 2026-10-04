"""Provider interfaces, reserved for Phase 2.

Phase 1 implements none of these. The interfaces exist now so that the shape of
the adapter boundary is decided and testable before any of it is written, and
so that adding a provider later does not force a redesign of the layers above.

Two rules are encoded here rather than left to convention:

  1. A provider reports `Availability` instead of raising when a credential is
     missing. An absent API key is a normal state, not an error, and the
     router must be able to degrade with a sentence of explanation rather than
     a stack trace.
  2. `Chemical Core` must never import this package. Structure knowledge stands
     on its own; if `crea.chem` starts reaching for a literature provider, the
     layering has rotted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from crea.core.errors import ProviderUnavailable

if TYPE_CHECKING:  # pragma: no cover - typing only; the annotation is a string
    from crea.core.models.identity import ChemicalIdentityCandidate


class Availability(str, Enum):
    AVAILABLE = "available"
    MISSING_KEY = "missing_key"
    DISABLED = "disabled"
    ERROR = "error"


class Capability(str, Enum):
    COMPOUND_LOOKUP = "compound_lookup"
    STRUCTURE_SEARCH = "structure_search"
    # Phase 3.6. Reading a chemical structure drawing is a capability of its
    # own, not a compound lookup: it takes an image, not an identifier, and it
    # answers with a reading rather than with a database record. Kept in this
    # enum so `ProviderRegistry.usable(...)` can ask for it the way it asks for
    # the others.
    STRUCTURE_RECOGNITION = "structure_recognition"
    LITERATURE_SEARCH = "literature_search"
    METADATA = "metadata"
    OPEN_ACCESS = "open_access"
    REACTION_SEARCH = "reaction_search"
    FULLTEXT = "fulltext"


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    availability: Availability
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.availability is Availability.AVAILABLE


@dataclass(frozen=True)
class CompoundQuery:
    """What a caller wants from a compound database.

    A frozen dataclass rather than a request model: this is a description of a
    question, not a policy object, and it must not grow behaviour. The fields
    are the lookup keys a compound database is actually asked by -- the ones
    Phase 3.6 resolves on -- and they are kept separate because they take
    different routes on the far side. An InChIKey is looked up, not parsed; a
    name is looked up, not guessed at; a CID is a primary key.

    Exactly one of the structure/name keys is expected to be set per call. A
    query carrying several is a caller mistake, and `services.identity` builds
    one query per key rather than a combined one, so that "which key matched"
    stays an answerable question.
    """

    identifier: str = ""        # the provider's own id, e.g. a PubChem CID
    name: str = ""
    smiles: str = ""
    inchi: str = ""
    inchikey: str = ""
    formula: str = ""
    synonyms: tuple[str, ...] = ()
    limit: int = 10


@dataclass(frozen=True)
class LiteratureQuery:
    """What a caller wants from a literature index."""

    text: str
    doi: str = ""
    year_from: int | None = None
    year_to: int | None = None
    limit: int = 25


@dataclass(frozen=True)
class PaperRef:
    """A work identified the way one provider knows it."""

    doi: str | None = None
    source_id: str | None = None


@dataclass(frozen=True)
class ReactionQuery:
    """What a caller wants from a reaction database."""

    product_smiles: str = ""
    reactant_smiles: str = ""
    limit: int = 25


@runtime_checkable
class CompoundProvider(Protocol):
    name: str
    capabilities: frozenset[Capability]
    requires_key: bool

    def status(self) -> ProviderStatus: ...

    def lookup(self, query: CompoundQuery) -> list[ChemicalIdentityCandidate]:
        """Return the records the database holds for this query.

        Shaped as `core.models.identity.ChemicalIdentityCandidate` (Phase 3.6)
        -- the compound half of the seam this Protocol reserved in Phase 2,
        which at the time had no model to point at. Now it does, and the type
        is spelled out rather than left as `object`, because a provider that
        returns whatever it likes is a provider that cannot be tested.

        Returning `[]` means *this source has no such entry*. It does not mean
        *the lookup failed*. A failure is a raise: `ProviderUnavailable` when
        the source cannot be reached at all, `RateLimited` / `InvalidRequest` /
        `InvalidUpstreamResponse` when the call went out and came back wrong.
        The distinction is the whole reason `services.identity` can report
        `NOT_FOUND` and `UNAVAILABLE` as different answers, and a provider that
        swallows an error into an empty list destroys that.
        """
        ...


@runtime_checkable
class LiteratureProvider(Protocol):
    """Declared here so the vocabulary lives in one place.

    The machinery that implements it is `crea.sources.base.BaseProvider`.
    `search` is async because every provider is network-bound; a synchronous
    search would force a blocking call into the event loop.
    """

    name: str
    capabilities: frozenset[Capability]
    requires_key: bool

    def status(self) -> ProviderStatus: ...

    async def search(self, query: LiteratureQuery) -> list[object]: ...

    async def get_metadata(self, ref: "PaperRef") -> object | None: ...

    async def get_access_assessment(self, doi: str) -> object | None: ...

    async def get_fulltext(self, paper: object) -> object: ...


@runtime_checkable
class ReactionProvider(Protocol):
    name: str
    capabilities: frozenset[Capability]
    requires_key: bool

    def status(self) -> ProviderStatus: ...

    def search(self, query: ReactionQuery) -> list[object]: ...


@dataclass
class ProviderRegistry:
    """Holds providers and reports which of them are actually usable."""

    _providers: dict[str, object] = field(default_factory=dict)

    def register(self, provider: object) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> object:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ProviderUnavailable(f"no provider named {name!r}") from exc

    def all(self) -> list[object]:
        return list(self._providers.values())

    def usable(self, capability: Capability | None = None) -> list[object]:
        out = []
        for provider in self._providers.values():
            if capability is not None and capability not in provider.capabilities:
                continue
            if provider.status().usable:
                out.append(provider)
        return out

    def unavailable(self) -> list[ProviderStatus]:
        return [p.status() for p in self._providers.values() if not p.status().usable]

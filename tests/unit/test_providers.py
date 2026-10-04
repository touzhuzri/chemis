"""T-G part 1 -- the provider adapter boundary (Phase 1 scope).

Phase 1 implements no providers. What it does implement is the *contract*, and
that contract is testable: a provider reports availability instead of raising
when a credential is missing, and the registry can say which providers are
usable without touching the network.

The search/dedup/access behaviour of T-G proper is Phase 2 and is listed in
test_scenario_bc_pending.py.
"""

import pytest

from crea.providers.base import (
    Availability,
    Capability,
    CompoundProvider,
    CompoundQuery,
    LiteratureProvider,
    LiteratureQuery,
    ProviderRegistry,
    ProviderStatus,
    ReactionProvider,
    ReactionQuery,
)
from crea.core.errors import ProviderUnavailable


class _Fake:
    def __init__(self, name, availability, capabilities):
        self.name = name
        self.availability = availability
        self.capabilities = frozenset(capabilities)

    def status(self):
        return ProviderStatus(
            name=self.name, availability=self.availability,
            reason="no credential configured" if self.availability is Availability.MISSING_KEY else "",
        )


def test_G00_no_providers_registered_in_phase_one():
    assert ProviderRegistry().all() == []


def test_G01_missing_key_is_a_state_not_an_exception():
    p = _Fake("openalex", Availability.MISSING_KEY, {Capability.LITERATURE_SEARCH})
    s = p.status()
    assert s.availability is Availability.MISSING_KEY
    assert s.usable is False
    assert "no credential" in s.reason


def test_G02_registry_filters_by_availability_and_capability():
    reg = ProviderRegistry()
    reg.register(_Fake("openalex", Availability.AVAILABLE, {Capability.LITERATURE_SEARCH}))
    reg.register(_Fake("unpaywall", Availability.MISSING_KEY, {Capability.OPEN_ACCESS}))
    reg.register(_Fake("pubchem", Availability.AVAILABLE, {Capability.COMPOUND_LOOKUP}))

    assert [p.name for p in reg.usable()] == ["openalex", "pubchem"]
    assert [p.name for p in reg.usable(Capability.LITERATURE_SEARCH)] == ["openalex"]
    assert [s.name for s in reg.unavailable()] == ["unpaywall"]


def test_G03_unknown_provider_raises_domain_error():
    with pytest.raises(ProviderUnavailable):
        ProviderRegistry().get("crossref")


def test_G04_protocols_are_runtime_checkable():
    for proto in (CompoundProvider, LiteratureProvider, ReactionProvider):
        assert proto is not None
    assert CompoundQuery(smiles="c1ccccc1").limit == 10
    assert LiteratureQuery(text="1,4-diphenyl-1,3-diyne synthesis").limit == 25
    assert ReactionQuery(product_smiles="c1ccccc1C#Cc1ccccc1").limit == 25

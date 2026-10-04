"""Live mention resolution against real PubChem. Opt-in, names only.

    CREA_LIVE_TESTS=1 pytest -m live

What this verifies is the part a test double cannot: that the two names the
phase keeps citing really are one identity in the real source, through the
real chain -- mention -> provider -> identity policy -> entity registry ->
one CHEM-id. No papers are downloaded, no images, no DECIMER.

The gate is the Phase 3.1 one: the shell supplies consent
(`CREA_LIVE_TESTS=1`), `.env` supplies credentials and its gate values are
popped by `tests/conftest.py` -- a file lying around is not consent.

A network failure is reported as a network failure: the test skips with the
real reason rather than passing quietly or blaming the code.
"""

import os

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CREA_LIVE_TESTS") != "1",
        reason="set CREA_LIVE_TESTS=1 to run live tests",
    ),
]


def test_phenylacetylene_and_ethynylbenzene_are_one_identity_live():
    from crea.core.errors import ProviderError, ProviderUnavailable
    from crea.core.models.entity import ChemicalMention, MentionResolutionStatus
    from crea.providers.pubchem import PubChemProvider
    from crea.services.entity_registry import ChemicalEntityRegistry
    from crea.services.identity import IdentityResolutionService
    from crea.services.mention_resolution import ChemicalMentionResolver
    from crea.sources.http import HttpxTransport

    provider = PubChemProvider(transport=HttpxTransport())
    registry = ChemicalEntityRegistry()
    resolver = ChemicalMentionResolver(
        registry,
        identity=IdentityResolutionService(compound_provider=provider))

    def mention(text):
        return ChemicalMention(mention_id=text, raw_text=text,
                               normalized_text=text.lower(),
                               source="live", source_id="pubchem-live")

    try:
        first = resolver.resolve(mention("phenylacetylene"))
        second = resolver.resolve(mention("ethynylbenzene"))
    except (ProviderError, ProviderUnavailable) as exc:
        pytest.skip(f"PubChem is unreachable: {exc}")

    # a source that could not answer is a network fact, not a code failure
    for result in (first, second):
        if result.status is MentionResolutionStatus.UNRESOLVED:
            pytest.skip("PubChem did not answer: "
                        + " ; ".join(result.evidence.notes))

    assert first.status is MentionResolutionStatus.RESOLVED
    assert second.status is MentionResolutionStatus.RESOLVED
    # one identity key -> one entity, whatever the paper calls it
    assert first.entity_id == second.entity_id
    entity = registry.get(first.entity_id)
    assert entity.identity_key.startswith("crea-identity-key-v1:InChI=1S/C8H6/")
    assert entity.entity_id.startswith("CHEM-")

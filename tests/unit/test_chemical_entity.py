"""Chemical entity models and the registry's rules.

Phase 3.8.1 (§18). What is under test is the vocabulary and the rules that
keep it honest: one identity key, one entity; names attach and never create;
external identifiers point outward and are never internal ids; anything short
of `RESOLVED` refuses to become an entity.

Resolutions here are built directly -- the identity service itself is
exercised in tests/integration_offline/test_chemical_entity_registry.py.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from crea.core.models.entity import (
    ENTITY_ID_PREFIX,
    ENTITY_ID_WIDTH,
    IDENTITY_KEY_VERSION,
    ChemicalEntity,
    ChemicalMention,
    EntityNotFound,
    EntityRegistrationError,
    ExternalIdentifier,
    MentionResolutionStatus,
    format_entity_id,
    identity_key,
    identity_key_of,
)
from crea.core.models.identity import (
    ChemicalIdentityCandidate,
    ChemicalIdentityResolution,
    IdentityBasis,
    StructureInput,
    StructureResolutionStatus,
)
from crea.services.entity_registry import (
    ChemicalEntityRegistry,
    InMemoryChemicalEntityRepository,
)

PHENYL_INCHI = "InChI=1S/C8H6/c1-2-8-6-4-3-5-7-8/h1,3-7H"


def _candidate(**overrides) -> ChemicalIdentityCandidate:
    base = dict(
        candidate_id="ide-unit", source="PUBCHEM", source_id="733",
        candidate_name="Phenylacetylene",
        canonical_smiles="C#Cc1ccccc1", inchi=PHENYL_INCHI,
        inchikey="UEXCJVNBTNXOEH-UHFFFAOYSA-N",
        identity_basis=IdentityBasis.PROVIDER_IDENTITY,
    )
    base.update(overrides)
    return ChemicalIdentityCandidate(**base)


def _resolution(*, status=StructureResolutionStatus.RESOLVED,
                candidate=None, selected=True, kind="SMILES", value="C#Cc1ccccc1"):
    candidate = candidate or _candidate()
    return ChemicalIdentityResolution(
        status=status,
        input=StructureInput.of(kind, value),
        candidates=[candidate],
        selected=candidate if (selected and status is StructureResolutionStatus.RESOLVED) else None,
        reason="unit test fixture",
    )


# ---------------------------------------------------------------------------
# entity ids
# ---------------------------------------------------------------------------


def test_entity_ids_are_fixed_width_sequential_and_internal():
    assert format_entity_id(1) == f"{ENTITY_ID_PREFIX}00000001"
    assert format_entity_id(2) == f"{ENTITY_ID_PREFIX}00000002"
    assert ENTITY_ID_WIDTH == 8
    assert ENTITY_ID_PREFIX == "CHEM-"
    with pytest.raises(ValueError):
        format_entity_id(0)


def test_the_same_identity_returns_the_same_entity_not_a_new_id():
    registry = ChemicalEntityRegistry()

    first = registry.register_resolution(_resolution())
    second = registry.register_resolution(_resolution(
        candidate=_candidate(input_name="ethynylbenzene")))

    assert first.entity_id == second.entity_id == "CHEM-00000001"
    assert len(registry) == 1


def test_different_identities_get_different_ids_in_order():
    registry = ChemicalEntityRegistry()
    a = registry.register_resolution(_resolution())
    other = _candidate(
        canonical_smiles="C#Cc1ccccc1C", inchi="InChI=1S/C9H8/c1-3-9-7-5-4-6-8-9/h4-8H,1H2",
        inchikey="XXXXX", candidate_id="ide-2")
    b = registry.register_resolution(_resolution(candidate=other))

    assert a.entity_id == "CHEM-00000001"
    assert b.entity_id == "CHEM-00000002"
    assert registry.entity_ids == ["CHEM-00000001", "CHEM-00000002"]


def test_the_entity_id_is_never_a_pubchem_cid():
    registry = ChemicalEntityRegistry()
    entity = registry.register_resolution(_resolution())

    assert entity.entity_id.startswith("CHEM-")
    assert entity.entity_id != "733"
    assert entity.entity_id != entity.source_id


# ---------------------------------------------------------------------------
# identity key
# ---------------------------------------------------------------------------


def test_the_identity_key_is_versioned_and_inchi_based():
    key = identity_key(PHENYL_INCHI)
    assert key == f"{IDENTITY_KEY_VERSION}:{PHENYL_INCHI}"
    assert IDENTITY_KEY_VERSION == "crea-identity-key-v1"

    registry = ChemicalEntityRegistry()
    entity = registry.register_resolution(_resolution())
    assert entity.identity_key == key
    assert entity.identity_key_version == IDENTITY_KEY_VERSION


def test_an_identity_key_needs_a_structure_substrate_not_a_name():
    with pytest.raises(ValueError):
        identity_key("")
    with pytest.raises(ValueError):
        identity_key("   ")
    # a name is not a key substrate: the derivation takes an InChI only
    with pytest.raises(ValueError):
        identity_key("phenylacetylene")


def test_the_key_reflects_the_identity_policy_dimensions():
    """Same InChI -> one entity (the policy's four facets agree); a different
    protonation state has a different InChI and gets its own entity -- the
    same distinctions `measure_facets` draws from the InChI layers."""
    registry = ChemicalEntityRegistry()
    acetate = _candidate(
        canonical_smiles="CC(=O)[O-]", inchi="InChI=1S/C2H4O2/c1-2(3)4/h1H3,(H,3,4)/p-1",
        inchikey="QTBSBXGTEINRQV-UHFFFAOYSA-M", candidate_id="ide-3")
    acid = registry.register_resolution(_resolution())
    base = registry.register_resolution(_resolution(candidate=acetate))

    assert acid.entity_id != base.entity_id
    assert acid.identity_key != base.identity_key


# ---------------------------------------------------------------------------
# names are labels, never identity
# ---------------------------------------------------------------------------


def test_names_merge_onto_one_entity_and_are_never_lost():
    registry = ChemicalEntityRegistry()
    first = registry.register_resolution(_resolution(candidate=_candidate(
        synonyms=[], input_name="phenylacetylene")))
    second = registry.register_resolution(_resolution(candidate=_candidate(
        synonyms=[], input_name="ethynylbenzene")))

    assert first.entity_id == second.entity_id
    assert "phenylacetylene" in first.names
    assert "ethynylbenzene" in first.names
    assert len(registry) == 1


def test_a_name_alone_cannot_register_an_entity():
    """name="phenylacetylene", structure unresolved -> UNRESOLVED, no entity."""
    registry = ChemicalEntityRegistry()
    name_only = ChemicalIdentityResolution(
        status=StructureResolutionStatus.INPUT,
        input=StructureInput.of("NAME", "phenylacetylene"),
        candidates=[], selected=None, reason="no structure was looked up")

    with pytest.raises(EntityRegistrationError) as excinfo:
        registry.register_resolution(name_only)

    assert excinfo.value.code == "NOT_RESOLVED"
    assert excinfo.value.status is StructureResolutionStatus.INPUT
    assert len(registry) == 0


def test_add_name_requires_an_existing_entity():
    registry = ChemicalEntityRegistry()
    entity = registry.register_resolution(_resolution())

    registry.add_name(entity.entity_id, "phenylacetylene")
    assert "phenylacetylene" in registry.get(entity.entity_id).names

    with pytest.raises(EntityNotFound):
        registry.add_name("CHEM-00000999", "anything")


# ---------------------------------------------------------------------------
# refusals: nothing uncertain becomes an entity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [
    StructureResolutionStatus.AMBIGUOUS,
    StructureResolutionStatus.NOT_FOUND,
    StructureResolutionStatus.UNAVAILABLE,
    StructureResolutionStatus.INVALID,
])
def test_an_unresolved_identity_is_never_registered(status):
    registry = ChemicalEntityRegistry()
    candidate = _candidate()
    resolution = ChemicalIdentityResolution(
        status=status,
        input=StructureInput.of("NAME", "vitamin C"),
        candidates=[candidate], selected=None,
        reason="kept as candidates")

    with pytest.raises(EntityRegistrationError) as excinfo:
        registry.register_resolution(resolution)

    assert excinfo.value.code == "NOT_RESOLVED"
    assert excinfo.value.status is status
    assert len(registry) == 0


def test_a_resolved_candidate_without_an_inchi_refuses_the_key():
    registry = ChemicalEntityRegistry()
    no_inchi = _candidate(inchi="")

    with pytest.raises(EntityRegistrationError) as excinfo:
        registry.register_resolution(_resolution(candidate=no_inchi))

    assert excinfo.value.code == "NO_IDENTITY_KEY"
    assert len(registry) == 0


def test_a_resolution_that_selected_nothing_is_not_registered():
    registry = ChemicalEntityRegistry()
    resolution = ChemicalIdentityResolution(
        status=StructureResolutionStatus.RESOLVED,
        input=StructureInput.of("SMILES", "C#Cc1ccccc1"),
        candidates=[_candidate()], selected=None, reason="inconsistent fixture")

    with pytest.raises(EntityRegistrationError):
        registry.register_resolution(resolution)
    assert len(registry) == 0


# ---------------------------------------------------------------------------
# external identifiers and provenance
# ---------------------------------------------------------------------------


def test_a_pubchem_cid_is_an_external_identifier_and_only_that():
    registry = ChemicalEntityRegistry()
    resolution = _resolution(kind="PUBCHEM_CID", value="733",
                             candidate=_candidate(source_id="733"))
    entity = registry.register_resolution(resolution)

    assert entity.entity_id.startswith("CHEM-")
    assert ("pubchem", "733") in [i.key() for i in entity.external_identifiers]
    assert registry.find_by_external_id("pubchem", "733") is entity
    assert registry.find_by_external_id("pubchem", "734") is None
    # the id is internal even though a CID was involved
    assert entity.entity_id != "733"


def test_external_identifiers_merge_and_deduplicate():
    registry = ChemicalEntityRegistry()
    entity = registry.register_resolution(_resolution())
    # registration already recorded the PubChem id; additions must not
    # duplicate each other
    registry.add_external_identifier(entity.entity_id, ExternalIdentifier(
        namespace="chembl", identifier="CHEMBL1", source="manual"))
    registry.add_external_identifier(entity.entity_id, ExternalIdentifier(
        namespace="chembl", identifier="CHEMBL1", source="manual"))

    keys = [i.key() for i in registry.get(entity.entity_id).external_identifiers]
    assert keys.count(("chembl", "CHEMBL1")) == 1
    assert ("pubchem", "733") in keys
    assert registry.find_by_external_id("chembl", "CHEMBL1").entity_id == entity.entity_id


def test_provenance_survives_registration():
    registry = ChemicalEntityRegistry()
    entity = registry.register_resolution(_resolution(candidate=_candidate(
        source="PUBCHEM", source_id="733",
        source_url="https://pubchem.ncbi.nlm.nih.gov/compound/733",
        retrieved_at="2026-10-02T00:00:00+00:00")))

    assert entity.identity_source == "PUBCHEM"
    assert entity.source_id == "733"
    assert entity.source_url.endswith("/733")
    assert entity.retrieved_at == "2026-10-02T00:00:00+00:00"
    assert entity.provenance is not None
    assert entity.provenance.source == "PUBCHEM"


def test_a_later_stronger_basis_is_kept_and_a_weaker_one_is_not():
    registry = ChemicalEntityRegistry()
    provider_first = registry.register_resolution(_resolution(
        candidate=_candidate(identity_basis=IdentityBasis.PROVIDER_IDENTITY)))
    exact_second = registry.register_resolution(_resolution(
        candidate=_candidate(identity_basis=IdentityBasis.EXACT_STRUCTURE)))
    assert exact_second.entity_id == provider_first.entity_id
    assert exact_second.identity_basis is IdentityBasis.EXACT_STRUCTURE

    weaker_third = registry.register_resolution(_resolution(
        candidate=_candidate(identity_basis=IdentityBasis.PROVIDER_IDENTITY)))
    assert weaker_third.identity_basis is IdentityBasis.EXACT_STRUCTURE
    assert any("raised" in note for note in weaker_third.notes)


def test_identity_key_of_a_candidate_requires_the_inchi():
    assert identity_key_of(_candidate()) == identity_key(PHENYL_INCHI)
    with pytest.raises(ValueError):
        identity_key_of(_candidate(inchi=""))


# ---------------------------------------------------------------------------
# the future mention boundary
# ---------------------------------------------------------------------------


def test_a_mention_without_resolution_carries_no_entity():
    mention = ChemicalMention(mention_id="m-1", raw_text="phenylacetylene")
    assert mention.resolution_status is MentionResolutionStatus.UNRESOLVED
    assert mention.entity_id is None

    with pytest.raises(ValidationError):
        ChemicalMention(mention_id="m-2", raw_text="phenylacetylene",
                        resolution_status=MentionResolutionStatus.AMBIGUOUS,
                        entity_id="CHEM-00000001")

    resolved = ChemicalMention(
        mention_id="m-3", raw_text="ethynylbenzene",
        resolution_status=MentionResolutionStatus.RESOLVED,
        entity_id="CHEM-00000001", evidence_refs=["ev-1"])
    assert resolved.entity_id == "CHEM-00000001"


def test_the_repository_protocol_is_the_future_swap_point():
    """The registry talks to storage through the protocol; the in-memory
    implementation is one of a family, not the definition of persistence."""
    from crea.services.entity_registry import ChemicalEntityRepository

    repo = InMemoryChemicalEntityRepository()
    registry = ChemicalEntityRegistry(repository=repo)
    entity = registry.register_resolution(_resolution())

    assert repo.get(entity.entity_id) is entity
    assert repo.find_by_identity_key(entity.identity_key) is entity
    assert repo.find_by_external_id("pubchem", "733") is entity
    # the registry's only storage access is the protocol's five methods
    assert set(ChemicalEntityRepository.__dict__) >= {
        "get", "add", "find_by_identity_key", "find_by_external_id", "all"}
    assert isinstance(entity, ChemicalEntity)

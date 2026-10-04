"""Chemical entity registry through the real identity layer.

Phase 3.8.1 (§18). The chain under test is the one the phase promises:

    StructureInput -> IdentityResolutionService -> RESOLVED -> registry
        -> CHEM-00000001

SMILES goes through RDKit's deterministic parse (the chemistry layer),
names and CIDs go through a compound provider, and nothing anywhere may
turn a word into a structure. The registry remembers; it never decides.

Everything is offline. The compound provider here is the same test double
the Phase 3.6 identity tests use.
"""

from __future__ import annotations

import pytest

from crea.core.models.entity import EntityRegistrationError
from crea.core.models.identity import (
    StructureInput,
    StructureResolutionStatus,
)
from crea.services.entity_registry import ChemicalEntityRegistry
from crea.services.identity import IdentityResolutionService
from tests.fakes.compound import PHENYLACETYLENE, FakeCompoundProvider

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"
PHENYL_SMILES = "C#Cc1ccccc1"


def _service(provider=None) -> IdentityResolutionService:
    return IdentityResolutionService(compound_provider=provider)


def test_the_same_smiles_registers_one_entity_twice():
    """SMILES -> RDKit -> resolution -> registry. Twice in, one id out."""
    registry = ChemicalEntityRegistry()
    service = _service()

    first = registry.register_resolution(
        service.resolve(StructureInput.of("SMILES", PHENYL_SMILES)))
    second = registry.register_resolution(
        service.resolve(StructureInput.of("SMILES", "C#Cc1ccccc1")))

    assert first.entity_id == second.entity_id == "CHEM-00000001"
    assert len(registry) == 1
    # the structure the chemistry layer established is what got registered
    assert first.inchi.startswith("InChI=1S/C8H6/")
    assert first.inchikey == "UEXCJVNBTNXOEH-UHFFFAOYSA-N"
    assert first.identity_key.startswith("crea-identity-key-v1:InChI=1S/C8H6/")


def test_a_name_lookup_registers_the_entity_the_provider_established():
    """NAME -> provider -> RESOLVED -> entity. The name never becomes the
    identity; the provider's structure does."""
    registry = ChemicalEntityRegistry()
    service = _service(FakeCompoundProvider(
        by_name={"phenylacetylene": [PHENYLACETYLENE]}))

    resolution = service.resolve(StructureInput.of("NAME", "phenylacetylene"))
    assert resolution.is_resolved
    entity = registry.register_resolution(resolution)

    assert entity.entity_id == "CHEM-00000001"
    assert entity.identity_basis.value == "provider_identity"
    assert "Phenylacetylene" in entity.names
    assert ("pubchem", "10821") in [i.key() for i in entity.external_identifiers]


def test_two_names_of_one_structure_give_one_entity():
    """§7's hard rule: phenylacetylene and ethynylbenzene, proven the same
    structure by the identity layer, are one entity with two names -- never
    CHEM-00000001 and CHEM-00000002."""
    registry = ChemicalEntityRegistry()
    quiet = PHENYLACETYLENE.model_copy(
        update={"synonyms": [], "input_name": None})
    service = _service(FakeCompoundProvider(by_name={
        "phenylacetylene": [quiet.model_copy(update={"input_name": "phenylacetylene"})],
        "ethynylbenzene": [quiet.model_copy(update={"input_name": "ethynylbenzene"})],
    }))

    for name in ("phenylacetylene", "ethynylbenzene"):
        registry.register_resolution(
            service.resolve(StructureInput.of("NAME", name)))

    assert len(registry) == 1
    names = registry.get("CHEM-00000001").names
    assert "phenylacetylene" in names
    assert "ethynylbenzene" in names


def test_an_ambiguous_name_keeps_its_candidates_and_mints_nothing():
    """vitamin C is three structures. None of them becomes an entity."""
    from tests.fakes.compound import ASCORBATE, ASCORBIC_ACID, SODIUM_ASCORBATE

    registry = ChemicalEntityRegistry()
    service = _service(FakeCompoundProvider(by_name={
        "vitamin C": [ASCORBIC_ACID, ASCORBATE, SODIUM_ASCORBATE]}))

    resolution = service.resolve(StructureInput.of("NAME", "vitamin C"))
    assert resolution.is_ambiguous
    assert len(resolution.candidates) == 3

    with pytest.raises(EntityRegistrationError) as excinfo:
        registry.register_resolution(resolution)

    assert excinfo.value.code == "NOT_RESOLVED"
    assert excinfo.value.status is StructureResolutionStatus.AMBIGUOUS
    assert len(registry) == 0
    # the candidates are still candidates -- nothing was selected for them
    assert resolution.selected is None


def test_an_unavailable_name_lookup_registers_nothing():
    registry = ChemicalEntityRegistry()
    service = _service(FakeCompoundProvider())  # holds nothing

    resolution = service.resolve(StructureInput.of("NAME", "mysteryene"))
    with pytest.raises(EntityRegistrationError):
        registry.register_resolution(resolution)
    assert len(registry) == 0


def test_a_pubchem_cid_registration_keeps_the_cid_external():
    """CID -> provider -> structure -> entity. The CID is recorded as an
    external identifier; the entity id stays CREA's own."""
    registry = ChemicalEntityRegistry()
    service = _service(FakeCompoundProvider(
        by_cid={"10821": [PHENYLACETYLENE]}))

    resolution = service.resolve(StructureInput.of("PUBCHEM_CID", "10821"))
    entity = registry.register_resolution(resolution)

    assert entity.entity_id == "CHEM-00000001"
    assert entity.entity_id != "10821"
    assert registry.find_by_external_id("pubchem", "10821") is entity
    assert entity.identity_source == "PUBCHEM"


def test_stereochemical_distinction_survives_registration():
    """Two structures the policy distinguishes stay two entities."""
    registry = ChemicalEntityRegistry()
    service = _service()

    trans = registry.register_resolution(service.resolve(
        StructureInput.of("SMILES", "F/C=C/F")))
    cis = registry.register_resolution(service.resolve(
        StructureInput.of("SMILES", r"F/C=C\F")))

    assert trans.entity_id != cis.entity_id
    assert trans.identity_key != cis.identity_key


def test_the_resolve_tool_reports_the_entity_id_without_replacing_evidence():
    """§17: the existing tool gains entity_id; it must stay explicit about
    identity_status / structure / source and must not present the id as
    structure evidence."""
    from crea.agent.toolsets import build_registry
    from crea.agent.workspace import IdentityState
    from crea.services.literature import LiteratureService

    registry_entities = ChemicalEntityRegistry()
    sink: list = []
    tools = build_registry(LiteratureService({}), None, sink,
                           identity=IdentityState(service=_service()),
                           entities=registry_entities)

    result = tools.execute(
        "resolve_chemical_identity", {"kind": "SMILES", "value": DIYNE})

    data = result.data
    assert data["status"] == "resolved"
    assert data["chemistry_allowed"] is True
    assert data["entity_id"] == "CHEM-00000001"
    # the id is a label; the structure is still the tool's own output
    assert data["selected"]["canonical_smiles"]
    assert data["selected"]["source"]
    assert data["entity_id"] != data["selected"]["canonical_smiles"]
    assert registry_entities.get("CHEM-00000001").inchi.startswith("InChI=1S/C16H10/")


def test_the_resolve_tool_reports_an_entity_id_only_for_resolved_identities():
    from crea.agent.toolsets import build_registry
    from crea.agent.workspace import IdentityState
    from crea.services.literature import LiteratureService

    registry_entities = ChemicalEntityRegistry()
    tools = build_registry(
        LiteratureService({}), None, [],
        identity=IdentityState(service=_service(FakeCompoundProvider())),
        entities=registry_entities)

    result = tools.execute(
        "resolve_chemical_identity", {"kind": "NAME", "value": "mysteryene"})

    # the provider answered and holds nothing: not_found, not resolved
    assert result.data["status"] == "not_found"
    assert result.data["entity_id"] is None
    assert len(registry_entities) == 0

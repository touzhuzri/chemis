"""The Semantic Spine: input -> resolver -> ChemicalEntity (Tests 1-6).

CREA 4.0 Stage B-1 (§15). Every test walks the real chain -- name ->
provider -> structure -> RDKit -> identity comparison -- with no step
hardcoded past (§16). Provider stubs carry *captured* records (the same
values the live PubChem returned, per tests/fakes/compound.py's provenance
block); the chain logic under test is the production one.

The OPSIN tests come in two flavours: a stub for environments without
Java, and the real worker (local, offline) when its runtime is present.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from crea.core.models.identity import (
    ChemicalIdentityCandidate,
    IdentityEvidenceLevel,
    MatchedBy,
    StructureResolutionStatus,
)
from crea.semantic.identity import identity_key_of_inchi
from crea.semantic.naming import NameLookupHit, NameLookupResult, NameResolver
from crea.semantic.representations import RepresentationError
from crea.semantic.resolver import (
    ChemicalInput,
    InputKind,
    ResolutionStatus,
    SemanticResolver,
)
from crea.services.entity_registry import ChemicalEntityRegistry
from crea.services.identity import IdentityResolutionService
from tests.fakes.compound import (
    ASCORBIC_ACID,
    FakeCompoundProvider,
    PHENYLACETYLENE,
)

#: OPSIN runtime laid out per workers/chemistry/opsin_worker.py.
_OPSIN_HOME = Path(__file__).resolve().parents[2] / "runtime" / "opsin"
_OPSIN_READY = (shutil.which("java") is not None
                and any(_OPSIN_HOME.glob("*.jar")))


class StubNameProvider:
    """A name source that answers from a table. Says so in its own name."""

    def __init__(self, table: dict[str, list], name: str = "stub",
                 fail: bool = False) -> None:
        self._table = table
        self.name = name
        self._fail = fail

    def lookup_name(self, name: str) -> NameLookupResult:
        if self._fail:
            raise RuntimeError(f"{self.name} is unavailable")
        hits = [NameLookupHit(candidate=c, matched_by=c.matched_by)
                for c in self._table.get(name, [])]
        return NameLookupResult(provider=self.name, hits=hits)


class StubOpsin:
    """A deterministic nomenclature stub: 'ethynylbenzene' and nothing else."""

    name = "opsin-stub"

    def lookup_name(self, name: str) -> NameLookupResult:
        if name != "ethynylbenzene":
            return NameLookupResult(provider=self.name, hits=[])
        candidate = ChemicalIdentityCandidate(
            candidate_id="opsin-ethynylbenzene", source="OPSIN",
            source_id="ethynylbenzene", candidate_name="ethynylbenzene",
            canonical_smiles="C(#C)c1ccccc1",
            matched_by=MatchedBy.PRIMARY_NAME)
        return NameLookupResult(
            provider=self.name,
            hits=[NameLookupHit(candidate=candidate,
                                matched_by=MatchedBy.PRIMARY_NAME)])


@pytest.fixture()
def registry():
    return ChemicalEntityRegistry()


@pytest.fixture()
def resolver(registry):
    return SemanticResolver(
        identity_service=IdentityResolutionService(),
        name_resolver=NameResolver([
            StubOpsin(),
            StubNameProvider({"phenylacetylene": [PHENYLACETYLENE]},
                             name="stub-pubchem"),
        ]),
        entity_registry=registry)


# -- Test 1: structure notation -> entity with valid representations ------


def test_1_smiles_becomes_an_entity_with_valid_representations(resolver):
    result = resolver.resolve(ChemicalInput.of("SMILES", "C#Cc1ccccc1"))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.identity_evidence is IdentityEvidenceLevel.EXACT
    entity = result.entity
    assert entity is not None and entity.entity_id.startswith("CHEM-")

    # valid molecular graph + derived notations (RDKit, via the chem layer)
    from crea.chem.normalize import normalize

    molecule = normalize(entity.canonical_smiles)
    assert molecule.canonical_smiles
    kinds = {r.kind: r for r in entity.representations}
    assert "canonical_smiles" in kinds and "standard_inchi" in kinds
    assert "inchikey" in kinds and "molblock" in kinds
    assert kinds["standard_inchi"].value.startswith("InChI=")
    assert kinds["inchikey"].value
    assert "V2000" in kinds["molblock"].value
    assert all(r.source for r in entity.representations), "sourced (§3)"
    # per-field provenance for the identity notations
    assert "canonical_smiles" in entity.field_provenance


# -- Test 2: name -> provider chain -> SAME entity, never a second one ----


def test_2_name_resolves_through_the_provider_chain_to_the_same_entity(
        resolver, registry):
    first = resolver.resolve(ChemicalInput.of("SMILES", "C#Cc1ccccc1"))
    second = resolver.resolve(ChemicalInput.of("NAME", "phenylacetylene"))

    assert second.status is ResolutionStatus.RESOLVED
    assert second.identity_evidence is IdentityEvidenceLevel.SUPPORTED, (
        "a provider-established name earns SUPPORTED, never EXACT (§4)")
    assert second.entity is not None
    # exact structural comparison happened -- and one identity is one entity
    assert second.entity.entity_id == first.entity.entity_id
    assert len(registry.entity_ids) == 1
    # the chain keeps its books
    assert any(p.provider == "stub-pubchem" and p.status == "ok"
               for p in second.provider_results)


# -- Test 3: systematic name through OPSIN -> same structure -------------


def test_3_systematic_name_resolves_via_opsin_stub_to_the_same_entity(
        resolver, registry):
    first = resolver.resolve(ChemicalInput.of("SMILES", "C#Cc1ccccc1"))
    third = resolver.resolve(ChemicalInput.of("NAME", "ethynylbenzene"))
    assert third.status is ResolutionStatus.RESOLVED
    assert third.identity_evidence is IdentityEvidenceLevel.SUPPORTED
    assert third.entity.entity_id == first.entity.entity_id
    assert len(registry.entity_ids) == 1


@pytest.mark.skipif(not _OPSIN_READY,
                    reason="OPSIN runtime (java + jars) not present")
def test_3b_systematic_name_resolves_via_the_real_opsin_worker(registry):
    """The real isolated worker: name -> OPSIN -> structure -> entity."""
    from crea.providers.name_lookup import OpsinNameLookup

    resolver = SemanticResolver(
        identity_service=IdentityResolutionService(),
        name_resolver=NameResolver([OpsinNameLookup()]),
        entity_registry=registry)
    baseline = resolver.resolve(ChemicalInput.of("SMILES", "C#Cc1ccccc1"))
    result = resolver.resolve(ChemicalInput.of("NAME", "ethynylbenzene"))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.entity.entity_id == baseline.entity.entity_id
    assert any(p.provider == "opsin" and p.status == "ok"
               for p in result.provider_results)


# -- Test 4: a wrong name resolves to nothing, and says so ---------------


def test_4_a_wrong_name_is_unresolved_and_never_guessed(resolver, registry):
    result = resolver.resolve(ChemicalInput.of("NAME", "zzznocnamezz"))
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.identity_evidence is IdentityEvidenceLevel.UNRESOLVED
    assert result.entity is None
    assert registry.entity_ids == [], "no entity is invented"
    assert result.diagnostics, "the refusal says why"


# -- Test 5: multiple candidates -> AMBIGUOUS, never candidates[0] ------


def test_5_a_multi_candidate_name_is_ambiguous_and_unpicked(registry):
    # one everyday name, two distinct structures: exactly the case that
    # must never be "resolved" by picking one (§4/§24)
    resolver = SemanticResolver(
        identity_service=IdentityResolutionService(),
        name_resolver=NameResolver([
            StubNameProvider({"vitamin c": [ASCORBIC_ACID, PHENYLACETYLENE]},
                             name="stub-multi"),
        ]),
        entity_registry=registry)
    result = resolver.resolve(ChemicalInput.of("NAME", "vitamin c"))
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.identity_evidence is IdentityEvidenceLevel.AMBIGUOUS
    assert result.entity is None, "never candidates[0]"
    assert len(result.candidates) == 2
    assert registry.entity_ids == []
    assert any("never pick" in d or "distinct structures" in d
               for d in result.diagnostics)


# -- Test 6: different notations, one identity ----------------------------


def test_6_smiles_inchi_and_molblock_are_one_identity(resolver, registry):
    from crea.chem.normalize import normalize
    from crea.chem.representations import molblock

    molecule = normalize("C#Cc1ccccc1")
    by_smiles = resolver.resolve(ChemicalInput.of("SMILES", "C#Cc1ccccc1"))
    by_inchi = resolver.resolve(ChemicalInput.of("INCHI", molecule.inchi))
    by_molblock = resolver.resolve(
        ChemicalInput.of_notation("molblock", molblock(molecule)))

    ids = {r.entity.entity_id for r in (by_smiles, by_inchi, by_molblock)}
    assert len(ids) == 1, "representations never fork an entity (§3)"
    keys = {identity_key_of_inchi(r.entity.inchi)
            for r in (by_smiles, by_inchi, by_molblock)}
    assert len(keys) == 1
    assert len(registry.entity_ids) == 1
    assert by_molblock.status is ResolutionStatus.RESOLVED
    assert by_molblock.identity_evidence is IdentityEvidenceLevel.EXACT


# -- input classification is explicit (§7) --------------------------------


def test_input_classification_is_explicit_and_never_guessed():
    assert ChemicalInput.of("SMILES", "C").kind is InputKind.STRUCTURE
    assert ChemicalInput.of("INCHI", "InChI=1S/C").kind is InputKind.STRUCTURE
    assert ChemicalInput.of("NAME", "benzene").kind is InputKind.NAME
    assert ChemicalInput.of("PUBCHEM_CID", "733").kind is InputKind.IDENTIFIER
    assert ChemicalInput.of("INCHIKEY", "UHOVQNZJYSORNB").kind is InputKind.IDENTIFIER
    assert ChemicalInput.of_notation(
        "molblock", "x").kind is InputKind.STRUCTURE
    with pytest.raises(ValueError):
        ChemicalInput.of("NOT_A_KIND", "x")


def test_an_invalid_representation_fails_explicitly():
    from crea.semantic.representations import representations_from_notation

    with pytest.raises(RepresentationError):
        representations_from_notation("canonical_smiles", "")
    with pytest.raises(RepresentationError):
        representations_from_notation("nonsense_kind", "C")
    with pytest.raises(RepresentationError):
        representations_from_notation("inchikey", "UHOVQNZJYSORNB-UHFFFAOYSA-N")


# -- acceptance 11: the workflow's identity path goes through the spine ---


def test_the_workflow_identity_tool_runs_through_the_semantic_resolver():
    """ResearchWorkflow -> resolve_chemical_identity -> SemanticResolver
    (§14). The tool reports the evidence level the spine derived, and the
    entity the spine minted-or-reused -- without the workflow itself
    changing shape."""
    from crea.agent.offline import build_offline_workflow_stack

    workflow, _library, _sink, registry = build_offline_workflow_stack()
    assert registry.semantic_resolver is not None, (
        "the composition root wires the spine")

    result = registry.execute("resolve_chemical_identity",
                              {"kind": "SMILES", "value": "C#Cc1ccccc1"})
    assert result.status.value == "ok", result.error
    data = result.data
    assert data["status"] == "resolved"
    assert data["identity_evidence"] == "exact"
    assert data["entity_id"] and data["entity_id"].startswith("CHEM-")
    assert data["chemistry_allowed"] is True

    # a name through the same path records its chain, not a guess
    named = registry.execute("resolve_chemical_identity",
                             {"kind": "NAME", "value": "zzznocnamezz"})
    assert named.data["status"] in ("not_found", "unavailable")
    assert named.data["identity_evidence"] == "unresolved"

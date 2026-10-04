"""A compound database that is not PubChem.

Used by the identity tests in place of the real provider, for the cases a live
endpoint cannot be made to produce on demand: a name that matches three
structures, a lookup that fails, a source that answers with records instead of
`[]`. PubChem's name index resolves most names to exactly one preferred entry,
so the *ambiguity rule* cannot be exercised against it -- but the rule is
ours, not PubChem's, and it has to hold whatever the source returns.

Nothing here is a fixture pretending to be an upstream response. It is a test
double, it says so in its own name, and it produces `ChemicalIdentityCandidate`
records directly rather than payloads for a parser to read.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crea.core.errors import ProviderError, ProviderUnavailable, ResourceNotFound
from crea.core.models.identity import ChemicalIdentityCandidate, IdentityBasis, MatchedBy
from crea.providers.base import Availability, Capability, CompoundProvider, CompoundQuery, ProviderStatus

#: Where the captured values below came from, in the same four fields
#: `tests/fakes/fixtures.py` records for a fixture file. §21 requires that a
#: hand-built fixture say `SYNTHETIC` and a captured one say where it was
#: captured from -- these are captured, and the fixtures the bytes live in are
#: named below. Nothing here is presented as an upstream response: the values
#: are *read* from real PubChem records and typed into a test double, and this
#: block is what keeps that honest after the fixture files are forgotten.
FIXTURE_PROVENANCE = {
    "origin": "LIVE_CAPTURE",
    "source": "PUBCHEM",
    "retrieved_at": "2026-10-01T15:40:07+00:00",
    "license": (
        "U.S. Government work / public domain. PubChem is a resource of the "
        "U.S. National Library of Medicine; its records carry no copyright."
    ),
    "description": (
        "values typed from the captured PUG REST responses in "
        "tests/fixtures/providers/pubchem/; not a replay of those bytes"
    ),
    "fixtures": [
        "tests/fixtures/providers/pubchem/name_vitamin_c.json",
        "tests/fixtures/providers/pubchem/name_ascorbate.json",
        "tests/fixtures/providers/pubchem/name_sodium_ascorbate.json",
        "tests/fixtures/providers/pubchem/name_phenylacetylene.json",
    ],
}

#: The three structures behind the informal name "vitamin C", captured from the
#: real PubChem endpoint and kept here as *values*, not as a replay. See
#: `tests/fixtures/providers/pubchem/name_vitamin_c.json` and its siblings for
#: the upstream bytes these were read from.
ASCORBIC_ACID = ChemicalIdentityCandidate(
    candidate_id="ide-pubchem-54670067",
    source="PUBCHEM", source_id="54670067",
    candidate_name="L-Ascorbic Acid",
    canonical_smiles="C(C(C1C(=C(C(=O)O1)O)O)O)O",
    isomeric_smiles="C([C@@H]([C@@H]1C(=C(C(=O)O1)O)O)O)O",
    inchi="InChI=1S/C6H8O6/c7-1-2(8)5-3(9)4(10)6(11)12-5/h2,5,7-10H,1H2/t2-,5+/m0/s1",
    inchikey="CIWBSHSKHKDKBQ-JLAZNSOCSA-N",
    molecular_formula="C6H8O6", molecular_weight=176.12,
    synonyms=["vitamin C", "ascorbic acid", "L-ascorbic acid"],
    matched_by=MatchedBy.SYNONYM, matched_synonym="vitamin C",
    identity_basis=IdentityBasis.PROVIDER_IDENTITY,
)

ASCORBATE = ChemicalIdentityCandidate(
    candidate_id="ide-pubchem-54679076",
    source="PUBCHEM", source_id="54679076",
    candidate_name="Ascorbate",
    canonical_smiles="C(C(C1C(=C(C(=O)O1)O)O)O)O",
    isomeric_smiles="C([C@@H]([C@@H]1C(=C(C(=O)[O-])O)O)O)O",
    inchi="InChI=1S/C6H8O6/c7-1-2(8)5-3(9)4(10)6(11)12-5/h2,5,7-10H,1H2,(H,7,8)/p-1/t2-,5+/m0/s1",
    inchikey="CIWBSHSKHKDKBQ-JLAZNSOCSA-M",
    molecular_formula="C6H7O6", molecular_weight=175.11,
    synonyms=["ascorbate", "L-ascorbate"],
    matched_by=MatchedBy.SYNONYM, matched_synonym="ascorbate",
    identity_basis=IdentityBasis.PROVIDER_IDENTITY,
)

SODIUM_ASCORBATE = ChemicalIdentityCandidate(
    candidate_id="ide-pubchem-23667548",
    source="PUBCHEM", source_id="23667548",
    candidate_name="Sodium ascorbate",
    canonical_smiles="C(C(C1C(=C(C(=O)[O-])O)O)O)O.[Na+]",
    isomeric_smiles="C([C@@H]([C@@H]1C(=C(C(=O)[O-])O)O)O)O.[Na+]",
    inchi="InChI=1S/C6H8O6.Na/c7-1-2(8)5-3(9)4(10)6(11)12-5;/h2,5,7-10H,1H2;/q;+1/p-1/t2-,5+;/m0/s1",
    inchikey="WDJXWJXVJLJVKU-WDSJZBSOSA-M",
    molecular_formula="C6H7NaO6", molecular_weight=198.11,
    synonyms=["sodium ascorbate", "sodium L-ascorbate"],
    matched_by=MatchedBy.PRIMARY_NAME, matched_synonym=None,
    identity_basis=IdentityBasis.PROVIDER_IDENTITY,
)

PHENYLACETYLENE = ChemicalIdentityCandidate(
    candidate_id="ide-pubchem-10821",
    source="PUBCHEM", source_id="10821",
    candidate_name="Phenylacetylene",
    canonical_smiles="C#CC1=CC=CC=C1",
    isomeric_smiles="C#CC1=CC=CC=C1",
    inchi="InChI=1S/C8H6/c1-2-8-6-4-3-5-7-8/h1,3-7H",
    inchikey="UEXCJVNBTNXOEH-UHFFFAOYSA-N",
    molecular_formula="C8H6", molecular_weight=102.13,
    synonyms=["phenylacetylene", "ethynylbenzene", "acetylene, phenyl-"],
    matched_by=MatchedBy.PRIMARY_NAME,
    identity_basis=IdentityBasis.PROVIDER_IDENTITY,
)


@dataclass
class FakeCompoundProvider:
    """A `CompoundProvider` whose answers are set by the test.

    `by_name` / `by_key` / `by_cid` map a lookup value to the candidates that
    lookup should return. A missing key returns `[]` -- the honest absence.
    `fail_with`, when set, makes every lookup raise instead, which is how the
    `UNAVAILABLE` path is tested without an outage.
    """

    by_name: dict[str, list[ChemicalIdentityCandidate]] = field(default_factory=dict)
    by_inchikey: dict[str, list[ChemicalIdentityCandidate]] = field(default_factory=dict)
    by_cid: dict[str, list[ChemicalIdentityCandidate]] = field(default_factory=dict)
    by_inchi: dict[str, list[ChemicalIdentityCandidate]] = field(default_factory=dict)
    fail_with: Exception | None = None
    name: str = "fake-compound"
    requires_key: bool = False
    capabilities: frozenset[Capability] = frozenset({Capability.COMPOUND_LOOKUP})
    queries: list[CompoundQuery] = field(default_factory=list)

    def status(self) -> ProviderStatus:
        if self.fail_with is not None:
            return ProviderStatus(self.name, Availability.ERROR, str(self.fail_with))
        return ProviderStatus(self.name, Availability.AVAILABLE)

    def lookup(self, query: CompoundQuery) -> list[ChemicalIdentityCandidate]:
        self.queries.append(query)
        if self.fail_with is not None:
            raise self.fail_with
        if query.name:
            return list(self.by_name.get(query.name, []))
        if query.inchikey:
            return list(self.by_inchikey.get(query.inchikey, []))
        if query.identifier:
            return list(self.by_cid.get(query.identifier, []))
        if query.inchi:
            return list(self.by_inchi.get(query.inchi, []))
        return []


class FailingCompoundProvider(FakeCompoundProvider):
    """A provider that cannot be consulted at all. Answers nothing; raises."""

    def __init__(self, exc: Exception | None = None) -> None:
        super().__init__(fail_with=exc or ProviderError("the source is unreachable"))


class UnconfiguredCompoundProvider(FakeCompoundProvider):
    """A provider that has no source behind it: `status` says so, `lookup` refuses."""

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.name, Availability.MISSING_KEY, "no source configured")

    def lookup(self, query: CompoundQuery):
        raise ProviderUnavailable("this provider has no source behind it")


__all__ = [
    "ASCORBATE", "ASCORBIC_ACID", "FakeCompoundProvider", "FailingCompoundProvider",
    "PHENYLACETYLENE", "SODIUM_ASCORBATE", "UnconfiguredCompoundProvider",
]

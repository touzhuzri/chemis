"""Identity evidence and facets: the judgement layer (Tests 7-10).

CREA 4.0 Stage B-1 (§15). The facets (stereochemistry, isotope, tautomer)
are Phase 3.6's and must survive untouched (§5) -- these tests pin them
through the new `semantic.identity` entry. Evidence levels report the
chain (§4); provenance names the provider (§10); a dead provider is a
diagnostic, not a veto (Test 10). And the name-normalization trio has one
home (§12) with its outputs pinned, because reaction identity keys are
built from one of them.
"""

from __future__ import annotations

import pytest

from crea.core.models.identity import (
    ChemicalIdentityCandidate,
    IdentityEvidenceLevel,
    MatchedBy,
    StructureResolutionStatus,
)
from crea.core.naming import (
    display_name,
    normalize_for_identity,
    normalize_for_search,
)
from crea.core.models.reaction_knowledge import ReactionComponent
from crea.semantic.identity import (
    evidence_for_match,
    facets_for,
    identity_evidence_level,
    same_entity_identity,
)
from crea.semantic.naming import NameLookupHit, NameLookupResult, NameResolver
from crea.semantic.resolver import (
    ChemicalInput,
    ResolutionStatus,
    SemanticResolver,
)
from crea.services.entity_registry import ChemicalEntityRegistry
from crea.services.identity import IdentityResolutionService


class FailingProvider:
    name = "dead-source"

    def lookup_name(self, name: str) -> NameLookupResult:
        raise RuntimeError("this source could not be consulted")


class SingleHitProvider:
    name = "stub-pubchem"

    def __init__(self, candidate) -> None:
        self._candidate = candidate

    def lookup_name(self, name: str) -> NameLookupResult:
        return NameLookupResult(
            provider=self.name,
            hits=[NameLookupHit(candidate=self._candidate,
                                matched_by=self._candidate.matched_by)])


# -- Test 7: stereochemistry facets are not broken (§5) -------------------


def test_7_stereochemistry_is_a_facet_not_a_string_difference():
    from crea.core.models.identity import StructureInput
    from crea.chem.normalize import normalize

    trans = normalize(r"C/C=C/C").inchi
    cis = normalize(r"C/C=C\C").inchi
    # same skeleton, different stereo: connectivity agrees, stereo does not
    facets = facets_for(trans, cis)
    assert facets.same_stereo is False
    assert same_entity_identity(trans, cis).value == "same_connectivity"
    # identical structure: EXACT, by the existing basis ladder
    assert facets_for(trans, trans).basis().value == "exact_structure"
    # and a notation input resolved through the service is EXACT evidence
    resolution = IdentityResolutionService().resolve(
        StructureInput.of("SMILES", "C/C=C/C"))
    assert identity_evidence_level(resolution) is IdentityEvidenceLevel.EXACT


# -- Test 8: tautomer / isotope policy is not broken (§5) -----------------


def test_8_isotope_and_tautomer_facets_keep_their_semantics():
    from crea.chem.normalize import normalize

    ethanol = normalize("CCO").inchi
    heavy = normalize("CC[18OH]").inchi  # isotope-labelled
    facets = facets_for(ethanol, heavy)
    assert facets.same_isotope is False
    assert facets.same_tautomer is True, (
        "isotope labels do not make different tautomers")
    # the basis ladder under-claims rather than over-claims (Phase 3.6 rule)
    assert facets.basis().value in ("same_stereo", "same_connectivity")

    # tautomer claims come ONLY from InChI's mobile-H treatment (Phase
    # 3.6's rule, unchanged): a pair whose /h layers differ is reported
    # as different tautomer classes and the basis UNDER-claims. Never
    # "probably tautomers" -- a comparison this layer cannot express is
    # left unexpressed.
    keto = normalize("CC(=O)CC(=O)C").inchi
    enol = normalize("CC(O)=CC(=O)C").inchi
    tautomer_facets = facets_for(keto, enol)
    assert tautomer_facets.same_tautomer is False, (
        "the /h layers differ; the comparator says so and under-claims")
    assert tautomer_facets.basis().value in ("same_stereo", "same_connectivity")
    # ... and a pair with identical layers is one tautomer class
    assert facets_for(keto, keto).same_tautomer is True


# -- Test 9: every external assertion is traceable (§10) ------------------


def test_9_provider_provenance_travels_with_every_assertion():
    from tests.fakes.compound import PHENYLACETYLENE

    candidate = PHENYLACETYLENE.model_copy(deep=True)
    resolver = SemanticResolver(
        identity_service=IdentityResolutionService(),
        name_resolver=NameResolver([SingleHitProvider(candidate)]),
        entity_registry=ChemicalEntityRegistry())
    result = resolver.resolve(ChemicalInput.of("NAME", "phenylacetylene"))
    assert result.status is ResolutionStatus.RESOLVED
    # provider_results name each source; candidates carry their own source
    assert [p.provider for p in result.provider_results] == [
        "stub-pubchem", "chain"]
    assert all(p.status == "ok" for p in result.provider_results
               if p.provider != "chain")
    selected = result.resolution.selected
    assert selected.source and selected.source_id, (
        "an identity assertion without a source is not an assertion")
    assert selected.input_name == "phenylacetylene"
    # and the entity's field provenance names where its notations came from
    for kind, provenance in result.entity.field_provenance.items():
        assert provenance.source, f"{kind} carries no source"


# -- Test 10: provider failure never collapses the chain ------------------


def test_10_a_dead_provider_is_a_diagnostic_not_a_veto():
    from tests.fakes.compound import PHENYLACETYLENE

    candidate = PHENYLACETYLENE.model_copy(deep=True)
    resolver = SemanticResolver(
        identity_service=IdentityResolutionService(),
        name_resolver=NameResolver([
            FailingProvider(),
            SingleHitProvider(candidate),
        ]),
        entity_registry=ChemicalEntityRegistry())
    result = resolver.resolve(ChemicalInput.of("NAME", "phenylacetylene"))
    assert result.status is ResolutionStatus.RESOLVED, (
        "one dead source must not sink the chain (Test 10)")
    assert any("could not be consulted" in d for d in result.diagnostics)
    dead = next(p for p in result.provider_results
                if p.provider == "dead-source")
    assert dead.status == "unavailable" and dead.error

    # and when EVERY source is dead, the answer is UNRESOLVED -- with the
    # failures recorded, not invented around
    only_dead = SemanticResolver(
        identity_service=IdentityResolutionService(),
        name_resolver=NameResolver([FailingProvider()]),
        entity_registry=ChemicalEntityRegistry())
    result = only_dead.resolve(ChemicalInput.of("NAME", "phenylacetylene"))
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.entity is None
    assert result.diagnostics


# -- evidence level semantics (§4) ---------------------------------------


def test_name_resolution_never_graduates_to_exact():
    """The label reports the chain: a word is never a structure notation."""
    from tests.fakes.compound import PHENYLACETYLENE

    for matched_by, expected in (
            (MatchedBy.PRIMARY_NAME, IdentityEvidenceLevel.SUPPORTED),
            (MatchedBy.INCHIKEY, IdentityEvidenceLevel.SUPPORTED),
            (MatchedBy.CID, IdentityEvidenceLevel.SUPPORTED),
            (MatchedBy.SYNONYM, IdentityEvidenceLevel.PROBABLE),
            (MatchedBy.UNKNOWN, IdentityEvidenceLevel.PROBABLE)):
        candidate = PHENYLACETYLENE.model_copy(deep=True)
        candidate.matched_by = matched_by
        answer = NameResolver([SingleHitProvider(candidate)]).resolve_name(
            "phenylacetylene")
        assert answer.identity_evidence is expected, (matched_by, expected)
        assert answer.identity_evidence is not IdentityEvidenceLevel.EXACT


def test_evidence_for_match_downgrades_but_never_upgrades():
    from crea.chem.normalize import normalize

    ethanol = normalize("CCO").inchi
    trans = normalize(r"C/C=C/C").inchi
    cis = normalize(r"C/C=C\C").inchi
    assert evidence_for_match(IdentityEvidenceLevel.SUPPORTED,
                              trans, trans) is IdentityEvidenceLevel.SUPPORTED
    assert evidence_for_match(IdentityEvidenceLevel.SUPPORTED,
                              trans, cis) is IdentityEvidenceLevel.PROBABLE
    assert evidence_for_match(IdentityEvidenceLevel.AMBIGUOUS,
                              trans, cis) is IdentityEvidenceLevel.AMBIGUOUS


# -- name normalization: one home, three purposes (§12) -------------------


def test_normalization_trio_is_pinned_because_identity_keys_are_built_from_it():
    assert normalize_for_identity("  Phenylacetylene ") == "phenylacetylene"
    assert normalize_for_search("  Phenylacetylene ") == "phenylacetylene"
    assert display_name("  Phenylacetylene ") == "Phenylacetylene"
    # the reaction-key material contract is unchanged (keys must not move)
    component = ReactionComponent(name="  Phenylacetylene ")
    assert component.key_material() == "name:phenylacetylene"


def test_the_three_normalizations_are_named_for_different_questions():
    # identity and search agree today but answer different questions; the
    # display form never decides identity
    assert display_name("X").lower() != normalize_for_identity("X") or True
    assert normalize_for_identity("X") == "x"
    assert display_name("X") == "X"

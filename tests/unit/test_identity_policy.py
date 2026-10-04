"""Identity policy: what may establish a structure, and what may not.

§8, §9, §10 and §11 are all statements about the same thing -- the standard of
proof. A structure may come from a deterministic parser, a trusted chemical
database, or the user. It may not come from a model, from a guess, from the
first row of a list, or from the fact that a word looks familiar.

The tests below are written to fail loudly if a later edit relaxes any of
that, and the docstrings say which failure each one is guarding.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from crea.core.models.identity import (
    ChemicalIdentityCandidate,
    IdentityBasis,
    IdentityFacets,
    MatchedBy,
    StructureInput,
    StructureResolutionStatus,
)
from crea.services.identity import (
    IdentityResolutionService,
    compare_identities,
    inchi_layers,
    measure_facets,
)
from tests.fakes.compound import (
    ASCORBATE,
    ASCORBIC_ACID,
    FakeCompoundProvider,
    PHENYLACETYLENE,
    SODIUM_ASCORBATE,
)

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "crea"

#: `benzene` is the spec's own example of an easy name. `vitamin C` is its
#: example of a hard one, and the three structures behind it are real PubChem
#: entries that differ in protonation and in salt form.
EASY = "benzene"
HARD = "vitamin C"


# ===========================================================================
# §8 -- a structure never comes from a model
# ===========================================================================


def test_the_identity_layer_contains_no_model_and_makes_no_llm_call():
    """§8's rule in its mechanical form: 不要让 LLM 自己生成 SMILES 作为最终结构.

    This is a static check rather than a behavioural one on purpose. A test
    that drives the service and asserts the answer was not hallucinated can
    only fail *after* a hallucination has reached the user. Checking that the
    dependency is absent fails at the edit that would introduce it.
    """
    files = [
        SRC / "services" / "identity.py",
        SRC / "core" / "models" / "identity.py",
        SRC / "providers" / "pubchem.py",
        SRC / "sources" / "pubchem.py",
    ]
    banned = {"openai", "anthropic", "crea.llm", "crea.agent"}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module)
                imported.add(node.module.split(".")[0])
        assert not (imported & banned), (
            f"{path.name} imports {imported & banned}; identity resolution "
            f"must not depend on a language model"
        )


def test_a_name_resolves_from_the_database_or_not_at_all():
    """§8: 最终结构必须来自确定性 parser、可信化学数据库或用户提供的结构。

    With no provider configured there is nothing to consult, and the answer is
    `UNAVAILABLE`. The one outcome the policy forbids is a structure appearing
    from nowhere -- so the test asserts that no candidate is produced at all.
    """
    result = IdentityResolutionService().resolve(
        StructureInput(kind="NAME", value="phenylacetylene")
    )
    assert result.status is StructureResolutionStatus.UNAVAILABLE
    assert result.candidates == []
    assert result.selected is None

    # and with a database, the structure is the database's, not ours
    result = IdentityResolutionService(
        compound_provider=FakeCompoundProvider(by_name={"phenylacetylene": [PHENYLACETYLENE]})
    ).resolve(StructureInput(kind="NAME", value="phenylacetylene"))
    assert result.require_resolved().source == "PUBCHEM"
    assert result.require_resolved().identity_basis is IdentityBasis.PROVIDER_IDENTITY


def test_a_provider_identity_is_recorded_as_an_assertion_not_a_measurement():
    """§11: a database saying "this is compound 2244" is not a comparison we ran.

    `PROVIDER_IDENTITY` is the basis for a lookup hit and `EXACT_STRUCTURE` is
    the basis for a parse. Keeping them apart is what stops "PubChem has this
    record" from being restated as "we verified this structure".
    """
    result = IdentityResolutionService(
        compound_provider=FakeCompoundProvider(by_name={"benzene": [PHENYLACETYLENE]})
    ).resolve(StructureInput(kind="NAME", value=EASY))
    candidate = result.require_resolved()
    assert candidate.identity_basis is IdentityBasis.PROVIDER_IDENTITY
    assert candidate.identity_basis is not IdentityBasis.EXACT_STRUCTURE
    # and nothing was compared, so no facet may claim agreement
    assert candidate.facets.same_connectivity is None


# ===========================================================================
# §9 -- name ambiguity
# ===========================================================================


def test_an_ambiguous_name_keeps_every_field_the_spec_names():
    """§9's field list, on the real vitamin C family.

    `input_name, candidate_name, synonyms, canonical_smiles, inchi, inchikey,
    source` -- all retained, because a reader deciding between three candidates
    needs all three of them to be describable.
    """
    service = IdentityResolutionService(
        compound_provider=FakeCompoundProvider(
            by_name={HARD: [ASCORBIC_ACID, ASCORBATE, SODIUM_ASCORBATE]}
        )
    )
    result = service.resolve(StructureInput(kind="NAME", value=HARD))
    assert result.status is StructureResolutionStatus.AMBIGUOUS

    for candidate in result.candidates:
        assert candidate.input_name == HARD
        assert candidate.candidate_name
        assert candidate.synonyms
        assert candidate.canonical_smiles
        assert candidate.inchi
        assert candidate.inchikey
        assert candidate.source == "PUBCHEM"


def test_the_three_vitamin_c_candidates_really_are_three_different_structures():
    """The acid, the anion and the salt differ in protonation and in salt form.

    If a future normalisation made these compare equal, the ambiguity would
    vanish silently and the user would be handed a structure they did not
    choose. This test is the alarm for that.
    """
    pairs = [
        (ASCORBIC_ACID, ASCORBATE),
        (ASCORBIC_ACID, SODIUM_ASCORBATE),
        (ASCORBATE, SODIUM_ASCORBATE),
    ]
    for a, b in pairs:
        facets = compare_identities(a, b)
        assert facets.same_connectivity is False or facets.same_tautomer is False or (
            facets.basis() is not IdentityBasis.EXACT_STRUCTURE
        ), f"{a.candidate_name} and {b.candidate_name} must not be the same molecule"


def test_no_rule_this_layer_applies_may_collapse_the_three():
    """The spec's sentence: 如果多个结构无法用清晰规则区分，应输出 AMBIGUOUS.

    There is no tie-break by name length, by CID order, by molecular weight or
    by which one looks more common. The absence of such a rule is the policy.
    """
    service = IdentityResolutionService(
        compound_provider=FakeCompoundProvider(
            by_name={HARD: [SODIUM_ASCORBATE, ASCORBATE, ASCORBIC_ACID]}
        )
    )
    result = service.resolve(StructureInput(kind="NAME", value=HARD))
    # reversed insertion order must not change the outcome or the pick
    assert result.status is StructureResolutionStatus.AMBIGUOUS
    assert result.selected is None
    assert [c.source_id for c in result.candidates] == [
        "23667548", "54679076", "54670067"
    ], "candidates are preserved in the order the source returned them"


# ===========================================================================
# §10 -- synonyms
# ===========================================================================


def test_a_synonym_is_usable_for_matching_and_is_not_identity_proof():
    """§10: synonym ≠ exact identity proof.

    A synonym that happens to be a registered trade name is a weaker link than
    a CAS number that is the entry's own identifier. The record says which of
    those happened; it does not upgrade the first into the second.
    """
    hit = ASCORBIC_ACID.model_copy(
        update={"matched_by": MatchedBy.SYNONYM, "matched_synonym": "vitamin C"}
    )
    result = IdentityResolutionService(
        compound_provider=FakeCompoundProvider(by_name={HARD: [hit]})
    ).resolve(StructureInput(kind="NAME", value=HARD))

    candidate = result.require_resolved()
    assert candidate.matched_by is MatchedBy.SYNONYM
    assert candidate.matched_synonym == "vitamin C"
    # a synonym match rests on the database's assertion, on our measurement
    assert candidate.identity_basis is IdentityBasis.PROVIDER_IDENTITY
    assert candidate.identity_basis is not IdentityBasis.EXACT_STRUCTURE
    assert candidate.facets.same_connectivity is None


def test_a_synonym_hit_and_a_primary_name_hit_for_one_entry_agree():
    """Two spellings of one compound resolve to one structure, not to a conflict.

    The service collapses records that describe the same structure, which is
    what makes "several candidates" mean "several possibilities".
    """
    by_synonym = ASCORBIC_ACID.model_copy(
        update={"candidate_id": "ide-syn", "matched_by": MatchedBy.SYNONYM,
                "matched_synonym": "vitamin C"}
    )
    result = IdentityResolutionService(
        compound_provider=FakeCompoundProvider(by_name={HARD: [ASCORBIC_ACID, by_synonym]})
    ).resolve(StructureInput(kind="NAME", value=HARD))

    assert result.status is StructureResolutionStatus.RESOLVED
    assert len(result.candidates) == 1
    assert result.require_resolved().inchikey == ASCORBIC_ACID.inchikey


# ===========================================================================
# §11 -- the four facets, measured on real structures
# ===========================================================================


def test_identical_structures_agree_on_every_facet():
    facets = measure_facets(ASCORBIC_ACID.inchi, ASCORBIC_ACID.inchi)
    assert facets.same_connectivity is True
    assert facets.same_stereo is True
    assert facets.same_isotope is True
    assert facets.same_tautomer is True
    assert facets.basis() is IdentityBasis.EXACT_STRUCTURE


def test_same_skeleton_and_different_stereochemistry_is_not_the_same_molecule():
    """The case §11 names: 两个结构可能有相同骨架和不同立体化学，不能说 same molecule.

    L-ascorbic acid and D-isoascorbic acid share a connectivity and differ in
    stereochemistry. The facets record both facts at once and the derived basis
    refuses to call them identical.
    """
    l_ascorbic = ASCORBIC_ACID
    # D-isoascorbic acid: the same skeleton, opposite stereochemistry
    d_isoascorbic = ChemicalIdentityCandidate(
        source="PUBCHEM", source_id="164686",
        candidate_name="D-Isoascorbic acid",
        canonical_smiles=l_ascorbic.canonical_smiles,
        isomeric_smiles="C([C@@H]([C@H]1C(=C(C(=O)O1)O)O)O)O",
        inchi="InChI=1S/C6H8O6/c7-1-2(8)5-3(9)4(10)6(11)12-5/h2,5,7-10H,1H2/t2-,5-/m1/s1",
        inchikey="CIWBSHSKHKDKBQ-BSAARUPYSA-N",
        molecular_formula="C6H8O6",
    )
    facets = compare_identities(l_ascorbic, d_isoascorbic)

    assert facets.same_connectivity is True, "the skeleton is shared"
    assert facets.same_stereo is False, "and the stereochemistry is not"
    assert facets.basis() is IdentityBasis.SAME_CONNECTIVITY
    assert facets.basis() is not IdentityBasis.EXACT_STRUCTURE


def test_a_deuterated_analogue_is_not_the_same_molecule_either():
    """Same skeleton, same stereochemistry, different isotopes.

    Ethanol and ethanol-d6 share a connectivity. They are not interchangeable in
    an experiment, and the facets must be able to say so without denying the
    similarity.
    """
    ethanol = ChemicalIdentityCandidate(
        source="PUBCHEM", source_id="702", candidate_name="Ethanol",
        canonical_smiles="CCO",
        inchi="InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3",
        inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N", molecular_formula="C2H6O",
    )
    ethanol_d6 = ChemicalIdentityCandidate(
        source="PUBCHEM", source_id="102138", candidate_name="Ethyl alcohol-d6",
        canonical_smiles="[2H]C([2H])([2H])C([2H])([2H])O[2H]",
        inchi="InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3/i1D3,2D2,3D",
        inchikey="LFQSCWFLJHTTHZ-LIDOUZCJSA-N", molecular_formula="C2H6O",
    )
    facets = compare_identities(ethanol, ethanol_d6)

    assert facets.same_connectivity is True
    assert facets.same_isotope is False
    assert facets.basis() is not IdentityBasis.EXACT_STRUCTURE


def test_different_protonation_states_are_different_structures():
    """Acetic acid and acetate differ in where the proton is.

    That is not a stereochemical difference and not a salt: it is a different
    species with a different formula, and the facets route it through
    connectivity rather than pretending it is a subtlety of the same molecule.
    """
    acetic = ChemicalIdentityCandidate(
        source="PUBCHEM", source_id="176", candidate_name="Acetic acid",
        canonical_smiles="CC(=O)O",
        inchi="InChI=1S/C2H4O2/c1-2(3)4/h1H3,(H,3,4)",
        inchikey="QTBSBXVTEAMEQO-UHFFFAOYSA-N", molecular_formula="C2H4O2",
    )
    acetate = ChemicalIdentityCandidate(
        source="PUBCHEM", source_id="175", candidate_name="Acetate",
        canonical_smiles="CC(=O)[O-]",
        inchi="InChI=1S/C2H4O2/c1-2(3)4/h1H3,(H,3,4)/p-1",
        inchikey="QTBSBXVTEAMEQO-UHFFFAOYSA-M", molecular_formula="C2H3O2",
    )
    facets = compare_identities(acetic, acetate)
    assert facets.same_connectivity is True, "the heavy-atom graph is shared"
    assert facets.same_tautomer is False, (
        "and the proton is not where it was: /p-1 is the whole difference"
    )
    assert facets.basis() is not IdentityBasis.EXACT_STRUCTURE


def test_uncomparable_inputs_yield_unknown_and_never_agreement():
    """Two candidates with no structure at all cannot be said to match.

    Saying so would report agreement between two empty strings, which is the
    failure mode that turns "we have nothing" into "they are the same".
    """
    empty_a = ChemicalIdentityCandidate(source="PUBCHEM", source_id="1")
    empty_b = ChemicalIdentityCandidate(source="PUBCHEM", source_id="2")
    facets = compare_identities(empty_a, empty_b)
    assert facets.same_connectivity is None
    assert facets.basis() is IdentityBasis.UNKNOWN

    assert measure_facets("", "").basis() is IdentityBasis.UNKNOWN
    assert measure_facets("not an inchi", ASCORBIC_ACID.inchi).basis() is IdentityBasis.UNKNOWN


def test_inchi_layers_split_the_questions_the_standard_asks_separately():
    """The audit surface: a reader can point at the `/t` that disagreed."""
    layers = inchi_layers(ASCORBIC_ACID.inchi)
    assert layers["formula"] == "C6H8O6"
    assert "c" in layers and "h" in layers
    assert "t" in layers, "L-ascorbic acid is stereogenic; the /t layer must be there"

    assert inchi_layers("C#Cc1ccccc1") == {}, "a SMILES is not an InChI"


def test_no_identity_model_carries_a_confidence_or_a_probability():
    """Phase 3.6 introduces no verdict field of its own.

    The evidence ladder from Phase 3.5 is untouched and the identity models do
    not grow a "how sure are we" column, because none of these paths produces
    a number that would deserve one.
    """
    for model in (ChemicalIdentityCandidate, IdentityFacets):
        fields = set(model.model_fields)
        for forbidden in ("confidence", "probability", "score", "certainty",
                          "likelihood", "success_rate"):
            assert forbidden not in fields, f"{model.__name__} grew {forbidden}"

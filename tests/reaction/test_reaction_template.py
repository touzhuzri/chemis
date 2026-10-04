"""The reaction knowledge model (Stage 3.1): structured templates.

Pins that a `ReactionTemplate` is a formal object with identity separated from
name, a structured class family, a structured transformation, substrate SMARTS,
distinct reagents/conditions, an independent mechanism, stored selectivity and
limitations, and honest CURATED provenance -- and that a match is a structural
statement, never an experimental-success guarantee (§20).
"""

from __future__ import annotations

from crea.chem.normalize import normalize
from crea.core.models.reaction_template import (
    EvidenceStatus,
    ReactionClassFamily,
    ReactionTemplate,
    Transformation,
)
from crea.services.reaction_library import ReactionMatcher, SEED_REACTION_TEMPLATES


def _by_id():
    return {t.template_id: t for t in SEED_REACTION_TEMPLATES}


# -- model ---------------------------------------------------------------


def test_every_seed_reaction_is_a_structured_object():
    assert len(SEED_REACTION_TEMPLATES) == 46
    for template in SEED_REACTION_TEMPLATES:
        assert template.template_id
        assert template.name
        assert template.class_family
        assert template.transformation.input_features
        assert template.transformation.output_features
        assert template.substrate_patterns
        assert template.evidence_status is EvidenceStatus.CURATED


def test_template_ids_are_locally_unique():
    ids = [t.template_id for t in SEED_REACTION_TEMPLATES]
    assert len(ids) == len(set(ids))


# -- classification ------------------------------------------------------


def test_reaction_class_family_is_structured():
    by_id = _by_id()
    assert by_id["primary-alcohol-oxidation"].class_family is ReactionClassFamily.OXIDATION
    assert by_id["alkyl-halide-substitution"].class_family is ReactionClassFamily.SUBSTITUTION
    assert by_id["alkene-hydrogenation"].class_family is ReactionClassFamily.REDUCTION
    assert by_id["alkyl-halide-elimination"].class_family is ReactionClassFamily.ELIMINATION
    assert by_id["alkene-hydrohalogenation"].class_family is ReactionClassFamily.ADDITION


def test_main_families_are_covered():
    families = {t.class_family for t in SEED_REACTION_TEMPLATES}
    # substitution/addition/elimination/oxidation/reduction/rearrangement/
    # condensation are all covered; coupling is a two-substrate transformation
    # the single-molecule matcher deliberately does not encode yet.
    assert families >= {
        ReactionClassFamily.SUBSTITUTION, ReactionClassFamily.ADDITION,
        ReactionClassFamily.ELIMINATION, ReactionClassFamily.OXIDATION,
        ReactionClassFamily.REDUCTION, ReactionClassFamily.REARRANGEMENT,
        ReactionClassFamily.CONDENSATION}


# -- transformation ------------------------------------------------------


def test_transformation_is_structured_not_a_name():
    template = _by_id()["primary-alcohol-oxidation"]
    assert template.transformation.input_features == ["primary_alcohol"]
    assert template.transformation.output_features == ["aldehyde"]
    assert template.transformation.note == "primary_alcohol -> aldehyde"


def test_transformation_can_express_multiple_inputs():
    # no seed reaction has multiple inputs yet, but the model must support it
    transformation = Transformation(input_features=["aryl_halide", "aryl_boron"],
                                    output_features=["biaryl"])
    assert transformation.label() == "aryl_halide+aryl_boron -> biaryl"


# -- mechanism is not derived from class ---------------------------------


def test_mechanism_is_stored_not_guessed_from_class():
    template = _by_id()["alkyl-halide-substitution"]
    assert template.class_family is ReactionClassFamily.SUBSTITUTION
    assert "SN1" in template.mechanism_class and "SN2" in template.mechanism_class


# -- selectivity is stored ------------------------------------------------


def test_selectivity_is_stored_not_inferred():
    template = _by_id()["alkene-hydroboration-oxidation"]
    assert template.selectivity is not None
    assert template.selectivity.regioselectivity == "anti-Markovnikov"
    assert template.selectivity.stereoselectivity == "syn"


# -- provenance / evidence / identity ------------------------------------


def test_seed_reactions_are_curated_not_literature():
    for template in SEED_REACTION_TEMPLATES:
        assert template.source_type == "CURATED"
        assert template.evidence_status is EvidenceStatus.CURATED
        assert template.provenance is not None


def test_internal_id_is_separate_from_external_references():
    for template in SEED_REACTION_TEMPLATES:
        assert template.template_id, "internal record id"
        assert isinstance(template.external_references, list)
        # the seed library has no external reaction database references
        assert template.external_references == []


def test_product_pattern_not_encoded_is_honest():
    template = _by_id()["alkene-hydrogenation"]
    assert template.product_patterns == []
    assert template.product_pattern_note == "product pattern not yet encoded"


# -- matcher: positive / negative / no false positives -------------------


def test_matcher_positive_matches_continue_to_work():
    matcher = ReactionMatcher()
    names = [m.template.name for m in matcher.match(normalize("CCO"))]
    assert "Primary Alcohol Oxidation" in names


def test_matcher_negative_and_no_false_positives():
    matcher = ReactionMatcher()
    # benzene matches EAS, never alkene or alcohol chemistry
    benzene = [m.template.name for m in matcher.match(normalize("c1ccccc1"))]
    assert "Aromatic Nitration" in benzene
    assert "Catalytic Hydrogenation" not in benzene
    assert "Primary Alcohol Oxidation" not in benzene
    # ethanol must not match alkene hydration
    names = [m.template.name for m in matcher.match(normalize("CCO"))]
    assert "Acid-catalyzed Hydration" not in names
    # aryl halide must not match aliphatic SN2 substitution
    aryl = [m.template.name for m in matcher.match(normalize("c1ccccc1Br"))]
    assert "Nucleophilic Substitution" not in aryl
    assert "SN2 Substitution" not in aryl


# -- matching is not a success guarantee ----------------------------------


def test_a_match_is_a_structural_statement_not_a_guarantee():
    matcher = ReactionMatcher()
    match = matcher.match(normalize("CCO"))[0]
    assert "not a guarantee" in match.note
    assert "substrate pattern matched" in match.note


# -- Stage 3.3: library integrity + cross-family no false positives --------


def test_all_substrate_smarts_compile():
    from rdkit import Chem

    for template in SEED_REACTION_TEMPLATES:
        for pattern in template.substrate_patterns:
            assert Chem.MolFromSmarts(pattern) is not None, template.template_id
        for pattern in template.product_patterns:
            assert Chem.MolFromSmarts(pattern) is not None, template.template_id


def test_no_false_positive_across_families():
    matcher = ReactionMatcher()
    # ethene matches alkene addition/hydrogenation
    ethene = [m.template.name for m in matcher.match(normalize("C=C"))]
    assert "Catalytic Hydrogenation" in ethene
    # benzene (a pi system) must NOT match alkene hydrogenation
    benzene = [m.template.name for m in matcher.match(normalize("c1ccccc1"))]
    assert "Catalytic Hydrogenation" not in benzene
    # fluorobenzene must NOT match aliphatic SN2 substitution
    fluoro = [m.template.name for m in matcher.match(normalize("c1ccccc1F"))]
    assert "SN2 Substitution" not in fluoro
    # acetone matches carbonyl reduction
    acetone = [m.template.name for m in matcher.match(normalize("CC(C)=O"))]
    assert "Ketone Reduction" in acetone
    # an alkane matches no carbonyl/alkene chemistry
    propane = [m.template.name for m in matcher.match(normalize("CCC"))]
    assert propane == []

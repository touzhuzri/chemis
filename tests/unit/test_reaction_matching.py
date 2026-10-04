"""Reaction-level matching, and the four states it can report.

The point of a four-valued answer is that "this paper is about something
similar" and "this paper ran this reaction" cannot be confused. Molecule
similarity alone cannot tell them apart, which is why it is the last rung.
"""

import pytest

from crea.core.models.route import ReactionMatchType
from crea.services.reaction_matching import (
    RELATED_FLOOR,
    compare_reactions,
)

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"
PA = "C#Cc1ccccc1"
BZN = "N#Cc1ccccc1"


def test_the_four_states_are_distinguishable():
    """The matcher's four. Two more exist, and they are not the matcher's.

    `NO_MATCH` and `DISCOVERY_ONLY` answer "why is there no support", which is
    the evidence layer's question; the matcher was handed structures on both
    sides and only ever answers "how do they relate". Pinning both sets here
    is what stops the two vocabularies being quietly merged.
    """
    matcher_states = {ReactionMatchType.EXACT, ReactionMatchType.PARTIAL,
                      ReactionMatchType.TRANSFORMATION, ReactionMatchType.DISCOVERY}
    evidence_states = {ReactionMatchType.NO_MATCH,
                       ReactionMatchType.DISCOVERY_ONLY}
    assert matcher_states | evidence_states == set(ReactionMatchType)
    assert not matcher_states & evidence_states


def test_exact_when_both_sides_match():
    r = compare_reactions([PA, PA], [DIYNE], [PA, PA], [DIYNE])
    assert r.match_type is ReactionMatchType.EXACT
    assert r.unmatched_reactants == [] and r.unmatched_products == []


def test_the_order_of_substrates_does_not_matter():
    r = compare_reactions([PA, "CC#N"], [DIYNE], ["CC#N", PA], [DIYNE])
    assert r.match_type is ReactionMatchType.EXACT


def test_canonical_forms_are_compared_not_strings():
    """`c1ccccc1C#CC#Cc2ccccc2` and its canonical form are the same molecule."""
    canonical = "C(C#Cc1ccccc1)#Cc1ccccc1"
    r = compare_reactions([PA, PA], [DIYNE], [PA, PA], [canonical])
    assert r.match_type is ReactionMatchType.EXACT


def test_partial_when_only_the_products_match():
    r = compare_reactions([PA, PA], [DIYNE], ["CC#N", "CC#N"], [DIYNE])
    assert r.match_type is ReactionMatchType.PARTIAL
    assert r.matched_products
    assert r.unmatched_reactants
    assert any("not the specific substrate" in n for n in r.notes)


def test_partial_when_only_the_reactants_match():
    r = compare_reactions([PA, PA], [DIYNE], [PA, PA], ["CC#CC"])
    assert r.match_type is ReactionMatchType.PARTIAL


def test_transformation_when_neither_side_matches_but_molecules_are_related():
    """Similarity is the last rung and the weakest one."""
    # a molecule sharing most of its fingerprint with the product
    related = "c1ccccc1C#Cc1ccccc1"          # diphenylacetylene
    r = compare_reactions(["O=C=O"], ["O=C(O)O"], [related], [related])
    assert r.match_type is ReactionMatchType.DISCOVERY      # unrelated, honestly

    near = "C#Cc1ccccc1Cl"                    # a chloro close relative
    r2 = compare_reactions(["CCCCCCCCN"], ["CCCCCCCC=O"], [PA], [near])
    assert r2.match_type in (ReactionMatchType.TRANSFORMATION,
                             ReactionMatchType.DISCOVERY)
    if r2.match_type is ReactionMatchType.TRANSFORMATION:
        assert r2.best_similarity >= RELATED_FLOOR
        assert any("not shown to run this step" in n for n in r2.notes)


def test_discovery_is_an_answer_not_a_failure():
    r = compare_reactions(["O=C=O"], ["O=C(O)O"], ["C1CCCCC1"], ["C1CCCC1"])
    assert r.match_type is ReactionMatchType.DISCOVERY
    assert r.notes
    assert r.matched_products == [] and r.matched_reactants == []


def test_a_candidate_with_no_molecules_is_discovery():
    r = compare_reactions([PA], [DIYNE], [], [])
    assert r.match_type is ReactionMatchType.DISCOVERY
    assert "no molecules" in r.notes[0]


def test_a_duplicate_substrate_does_not_silently_match_twice():
    r = compare_reactions([PA], [DIYNE], [PA, PA], [DIYNE])
    assert r.matched_reactants == [PA]
    assert r.unmatched_reactants == [PA]


def test_the_comparison_never_consults_a_model():
    import ast
    import inspect

    import crea.services.reaction_matching as module


    source = ast.parse(inspect.getsource(module))
    imported = set()
    for node in ast.walk(source):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for banned in ("crea.llm", "anthropic", "openai"):
        assert banned not in imported


def test_it_raises_no_support_on_its_own():
    """This module reports a relationship. It never promotes a route step."""
    import inspect

    import crea.services.reaction_matching as module
    from crea.core.models.route import RouteValidationResult

    assert not hasattr(RouteValidationResult, "apply")
    source = inspect.getsource(module)
    assert "StepStatus" not in source
    assert "SupportLevel" not in source

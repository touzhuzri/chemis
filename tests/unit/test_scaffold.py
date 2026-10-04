"""T-D -- layer 3a, Murcko scaffold."""

from crea.chem.normalize import normalize
from crea.chem.scaffold import compare_scaffolds
from tests.conftest import (
    BENZENE,
    DIPHENYLACETYLENE,
    DIPHENYL_BUTADIYNE,
    O_XYLENE,
    P_XYLENE,
    PHENYLACETYLENE,
    PYRIDINE,
    TOLUENE,
)


def test_D01_acyclic_substituents_are_dropped():
    # Toluene's scaffold is benzene: Murcko keeps ring systems and the linkers
    # between them, nothing else.
    s = compare_scaffolds(normalize(TOLUENE), normalize(BENZENE))
    assert s.scaffold_a == "c1ccccc1"
    assert s.relation == "identical"


def test_D02_linker_between_two_rings_is_kept():
    s = compare_scaffolds(normalize(DIPHENYLACETYLENE), normalize(BENZENE))
    assert "#C" in s.scaffold_a  # the linker is retained, unlike a methyl
    assert s.relation != "identical"


def test_D03_scaffold_cannot_distinguish_positional_isomers():
    s = compare_scaffolds(normalize(O_XYLENE), normalize(P_XYLENE))
    assert s.scaffold_a == s.scaffold_b == "c1ccccc1"
    assert s.relation == "identical"


def test_D04_generic_comparison_across_heteroatom_series():
    s = compare_scaffolds(normalize(BENZENE), normalize(PYRIDINE))
    assert s.generic_a == s.generic_b
    assert s.relation in ("generic_match", "substructure")


def test_D05_diyne_and_diphenylacetylene_differ():
    a = compare_scaffolds(normalize(DIPHENYL_BUTADIYNE), normalize(DIPHENYLACETYLENE))
    assert a.scaffold_a != a.scaffold_b
    # neither scaffold contains the other, so they fall back to sharing a ring system
    assert a.relation == "ring_system_shared"


def test_D06_caveat_is_always_carried():
    s = compare_scaffolds(normalize(PHENYLACETYLENE), normalize(TOLUENE))
    assert "not an identity claim" in s.caveat

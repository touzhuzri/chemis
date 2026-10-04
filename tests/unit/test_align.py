"""T-C -- layer 2, maximum common substructure."""

import pytest

from crea.chem.align import find_core_match
from crea.chem.normalize import normalize, rebuild
from tests.conftest import (
    BENZENE,
    CYCLOHEXANE,
    DIPHENYLACETYLENE,
    DIPHENYL_BUTADIYNE,
    M_XYLENE,
    O_XYLENE,
    P_XYLENE,
    PHENYLACETYLENE,
    PYRIDINE,
)


def test_C01_self_match_covers_everything(diyne):
    core = find_core_match(diyne, normalize(DIPHENYL_BUTADIYNE))
    assert core.found is True
    assert core.coverage_a == 1.0
    assert core.coverage_b == 1.0
    assert core.num_atoms == diyne.descriptors.heavy_atom_count


def test_C02_mcs_cannot_tell_positional_isomers_apart():
    # This test documents a limitation, not a capability: the MCS of ortho- and
    # meta-xylene is the same 7-atom substructure. The attachment layer is what
    # resolves them; see T-F.
    core = find_core_match(normalize(O_XYLENE), normalize(M_XYLENE))
    assert core.found is True
    assert core.num_atoms == 7
    assert core.num_bonds == 7
    assert core.coverage_a == pytest.approx(7 / 8)


def test_C03_aromatic_atoms_do_not_map_onto_aliphatic_ones():
    core = find_core_match(normalize(BENZENE), normalize(CYCLOHEXANE))
    if core.found and core.best_alignment:
        mols = (rebuild(normalize(BENZENE)), rebuild(normalize(CYCLOHEXANE)))
        for ia, ib in core.best_alignment.atom_map.items():
            a = mols[0].GetAtomWithIdx(ia)
            b = mols[1].GetAtomWithIdx(ib)
            # CompareOrderExact treats an aromatic bond and a single bond as
            # different, so a ring of aromatic carbons cannot be matched to a
            # ring of sp3 carbons.
            assert not (a.GetIsAromatic() and not b.GetIsAromatic())
    else:
        assert core.found is False


def test_C04_nitrogen_does_not_match_carbon():
    core = find_core_match(normalize(BENZENE), normalize(PYRIDINE))
    if core.found and core.best_alignment:
        for ia, ib in core.best_alignment.atom_map.items():
            assert normalize(BENZENE).atoms[ia].symbol == normalize(PYRIDINE).atoms[ib].symbol


def test_C05_strict_profile_is_stricter_than_relaxed():
    relaxed = find_core_match(normalize(BENZENE), normalize(CYCLOHEXANE), profile="relaxed")
    strict = find_core_match(normalize(BENZENE), normalize(CYCLOHEXANE), profile="strict")
    assert strict.num_atoms <= relaxed.num_atoms


def test_C06_no_common_substructure_is_reported_not_raised():
    core = find_core_match(normalize(BENZENE), normalize("N#N"))
    assert core.found is False
    assert core.num_atoms == 0
    assert core.mcs_smarts == ""


def test_C07_differences_are_connected_blocks_with_boundary_bonds():
    core = find_core_match(normalize(O_XYLENE), normalize(P_XYLENE))
    assert core.unmatched_components_a, "expected an unmatched region on A"
    for comp in core.unmatched_components_a:
        assert comp.heavy_atom_count == len(comp.atoms)
        assert comp.canonical_smiles_with_star
        assert "*" in comp.canonical_smiles_with_star
        assert comp.attachment_note


def test_C08_alternatives_are_distinct_correspondences():
    core = find_core_match(normalize(DIPHENYLACETYLENE), normalize(PHENYLACETYLENE))
    assert core.found is True
    assert core.best_alignment is not None
    keys = [tuple(sorted(core.best_alignment.atom_map.items()))]
    for alt in core.alternative_alignments:
        k = tuple(sorted(alt.atom_map.items()))
        assert k not in keys, "an 'alternative' that is the same atom map is not an alternative"
        keys.append(k)
        assert alt.score <= core.best_alignment.score


def test_C09_params_are_recorded_for_reproducibility():
    core = find_core_match(normalize(O_XYLENE), normalize(P_XYLENE), profile="strict", timeout=7)
    assert core.params.profile == "strict"
    assert core.params.timeout_seconds == 7
    assert core.params.bond_compare == "CompareOrderExact"
    assert core.params.atom_compare == "CompareElements"
    assert core.runtime_ms >= 0

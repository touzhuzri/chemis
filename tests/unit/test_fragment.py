"""T-E -- layer 3b, fragment decomposition."""

import pytest

from crea.chem.fragment import decompose_molecule
from crea.chem.normalize import normalize
from tests.conftest import (
    ASPIRIN,
    BENZENE,
    DIPHENYLACETYLENE,
    DIPHENYL_BUTADIYNE,
    O_XYLENE,
    PHENYLACETYLENE,
)


def test_E01_recap_splits_a_leo_los_group():
    frags = decompose_molecule(normalize(ASPIRIN), "recap")
    assert frags, "aspirin has a cleavable ester and should split"
    assert all(f.origin_method == "recap" for f in frags)


def test_E02_brics_splits_aspirin():
    frags = decompose_molecule(normalize(ASPIRIN), "brics")
    assert len(frags) >= 2
    assert sum(f.n_attachment_points for f in frags) >= 2


def test_E03_brics_leaves_a_xylene_whole():
    # BRICS only cuts bonds it considers synthetically meaningful. A xylene has
    # none, so the decomposition yields one uncut fragment. This is correct
    # behaviour and the reason a positional-isomer question cannot be answered
    # at the BRICS layer.
    frags = decompose_molecule(normalize(O_XYLENE), "brics")
    assert len(frags) == 1
    assert frags[0].n_attachment_points == 0


def test_E04_brics_does_not_cut_alkynes_in_this_rdkit_build():
    # Recorded, not asserted as a design goal: verified against rdkit
    # 2026.03.6, where BRICSDecompose returns the diyne intact.
    frags = decompose_molecule(normalize(DIPHENYL_BUTADIYNE), "brics")
    assert len(frags) == 1


def test_E05_ring_linker_splits_rings_from_linkers():
    frags = decompose_molecule(normalize(DIPHENYLACETYLENE), "ring_linker")
    smiles = sorted(f.fragment_smiles for f in frags)
    assert smiles == ["C#C", "c1ccccc1", "c1ccccc1"]
    ring = next(f for f in frags if f.fragment_smiles == "c1ccccc1")
    assert ring.n_attachment_points == 1
    linker = next(f for f in frags if f.fragment_smiles == "C#C")
    assert linker.n_attachment_points == 2


def test_E06_every_attachment_traces_back_to_the_parent():
    m = normalize(DIPHENYL_BUTADIYNE)
    frags = decompose_molecule(m, "ring_linker")
    n_atoms = m.descriptors.heavy_atom_count
    for frag in frags:
        for ap in frag.attachments:
            assert 0 <= ap.parent_atom_idx < n_atoms
            assert 0 <= ap.parent_bond_idx < len(m.bonds)
            assert ap.parent_side_elements
            assert ap.rcad.startswith("*")


def test_E07_fragments_reassemble_to_the_parent():
    m = normalize(DIPHENYL_BUTADIYNE)
    frags = decompose_molecule(m, "ring_linker")
    covered = sum(f.heavy_atom_count for f in frags)
    assert covered == m.descriptors.heavy_atom_count
    assert sum(f.n_rings for f in frags) == m.descriptors.num_rings


def test_E08_unknown_method_is_rejected():
    from crea.core.errors import FragmentationError

    with pytest.raises(FragmentationError):
        decompose_molecule(normalize(BENZENE), "not_a_method")


def test_E09_whole_molecule_decomposition():
    frags = decompose_molecule(normalize(PHENYLACETYLENE), "whole")
    assert len(frags) == 1
    assert frags[0].n_attachment_points == 0

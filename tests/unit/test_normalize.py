"""T-A -- normalization."""

import pytest

from crea.chem.normalize import normalize, rebuild
from crea.core.errors import NormalizationError
from tests.conftest import ASPIRIN, DIPHENYL_BUTADIYNE


def test_A01_canonical_form_and_key():
    m = normalize(DIPHENYL_BUTADIYNE)
    assert m.canonical_smiles == "C(C#Cc1ccccc1)#Cc1ccccc1"
    assert m.inchikey == "HMQFJYLWNWIYKQ-UHFFFAOYSA-N"
    assert m.mol_id == m.inchikey
    assert m.descriptors.molecular_formula == "C16H10"
    assert m.descriptors.heavy_atom_count == 16
    assert m.descriptors.num_rings == 2


def test_A02_alternative_notation_gives_identical_molecule():
    a = normalize("c1ccccc1C#CC#Cc2ccccc2")
    b = normalize("C(#CC#Cc1ccccc1)c1ccccc1")
    assert a.canonical_smiles == b.canonical_smiles
    assert a.inchikey == b.inchikey
    assert a.mol_id == b.mol_id


def test_A03_rebuild_round_trips():
    m = normalize(DIPHENYL_BUTADIYNE)
    r = rebuild(m)
    from rdkit import Chem

    assert Chem.MolToSmiles(r) == m.canonical_smiles


def test_A04_counter_ion_is_removed_and_recorded():
    m = normalize("CC(=O)Oc1ccccc1C(=O)[O-].[Na+]", profile="parent")
    # the anion is retained as an anion: Cleanup reionizes but the Uncharger is
    # deliberately off, so protonation state is never silently rewritten
    assert m.descriptors.molecular_formula == "C9H7O4-"
    assert m.canonical_smiles == "CC(=O)Oc1ccccc1C(=O)[O-]"
    assert m.descriptors.heavy_atom_count == 13
    assert any("FragmentParent" in a for a in m.normalization_actions)
    assert m.warnings  # the removal is never silent


def test_A05_unparseable_input_raises_domain_error():
    with pytest.raises(NormalizationError):
        normalize("c1ccccc")  # unclosed ring
    with pytest.raises(NormalizationError):
        normalize("")


def test_A06_ring_sizes_on_fused_system():
    m = normalize("c1ccc2ccccc2c1")
    # `AtomRings()` is SSSR-based, so a fused bicyclic reports the two
    # six-membered rings, not the ten-membered perimeter. Ring *count* comes
    # from CalcNumRings and is the number to trust.
    sizes = {s for a in m.atoms for s in a.ring_sizes}
    assert sizes == {6}
    assert m.descriptors.num_rings == 2
    assert m.descriptors.num_aromatic_rings == 2


def test_A07_identity_profile_keeps_the_whole_salt():
    salt = normalize("CC(=O)[O-].[Na+]", profile="identity")
    parent = normalize("CC(=O)[O-].[Na+]", profile="parent")
    # `identity` performs no standardization at all, so the counter-ion stays
    # in the record; `parent` drops it. The difference has to be visible.
    assert salt.descriptors.heavy_atom_count == 5  # C, C, O, O, Na
    assert parent.descriptors.heavy_atom_count == 4  # CH3-C(=O)O-
    assert "FragmentParent" in " ".join(parent.normalization_actions)
    assert parent.normalization_actions != salt.normalization_actions


def test_A08_atom_and_bond_vectors_are_consistent():
    m = normalize(ASPIRIN)
    assert len(m.atoms) == m.descriptors.heavy_atom_count
    assert all(a.mol_id == m.mol_id for a in m.atoms)
    assert all(b.mol_id == m.mol_id for b in m.bonds)
    idx = {a.atom_idx for a in m.atoms}
    assert all({b.a_idx, b.b_idx} <= idx for b in m.bonds)

"""Phase 1.1 -- attachment-aware matching regression.

The single question this file exists to answer:

    Can the engine tell "these two share a benzene ring" apart from
    "these two share a benzene ring *and* the linker attached to it is
    different"?

Scaffold comparison answers the first and is blind to the second. Fingerprint
similarity blurs both. Only the attachment layer separates them, and the whole
value of the Chemical Core rests on that.

No BRICS. BRICS is a synthesizability decomposition and, verified against
rdkit 2026.03.6, it does not cut alkynes at all. These tests use the
`ring_linker` decomposition, which is the one designed for this question.
"""

import pytest

from crea.chem.align import find_core_match
from crea.chem.attachment import compare_attachments
from crea.chem.fragment import decompose_molecule
from crea.chem.normalize import normalize
from crea.chem.scaffold import compare_scaffolds
from crea.core.models.fragments import FRAGMENT_FORMULA_CAVEAT

# --- the seven molecules under test -----------------------------------------
O_XYLENE = "Cc1ccccc1C"
M_XYLENE = "Cc1cccc(C)c1"
P_XYLENE = "Cc1ccc(C)cc1"
PHENYLACETYLENE = "C#Cc1ccccc1"
DIPHENYLACETYLENE = "c1ccccc1C#Cc1ccccc1"
DIPHENYL_BUTADIYNE = "c1ccccc1C#CC#Cc2ccccc2"
BENZYL_ALCOHOL = "OCc1ccccc1"
PHENETHYL_ALCOHOL = "OCCc1ccccc1"


def frags(smiles, method="ring_linker"):
    return decompose_molecule(normalize(smiles), method)


def by_smiles(fragment_list, smiles):
    return next(f for f in fragment_list if f.fragment_smiles == smiles)


def ring(fragment_list):
    return by_smiles(fragment_list, "c1ccccc1")


# ---------------------------------------------------------------------------
# 1 & 2. positional isomers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "other,relation",
    [(P_XYLENE, "para"), (M_XYLENE, "meta")],
)
def test_P11_01_positional_isomers_are_separated_only_at_the_attachment_layer(other, relation):
    fa, fb = frags(O_XYLENE), frags(other)
    ma, mb = normalize(O_XYLENE), normalize(other)

    # scaffold: blind
    sc = compare_scaffolds(ma, mb)
    assert sc.relation == "identical"
    assert sc.scaffold_a == sc.scaffold_b == "c1ccccc1"

    # common core: blind -- the same 7-atom substructure either way
    core = find_core_match(ma, mb)
    assert core.found is True
    assert core.num_atoms == 7
    assert core.mcs_smarts == "[#6&!R]-&!@[#6]1:&@[#6]:&@[#6]:&@[#6]:&@[#6]:&@[#6]:&@1"

    # fragments: blind -- the same ring, the same number of fragments
    assert ring(fa).fragment_smiles == ring(fb).fragment_smiles == "c1ccccc1"
    assert len(fa) == len(fb) == 3

    # attachment layer: sees it
    ra, rb = ring(fa), ring(fb)
    assert ra.rcads != rb.rcads, "ortho and the other isomer must not share an RCAD"
    assert (ra.position_relation, rb.position_relation) == ("ortho", relation)

    am = compare_attachments(ra, rb)
    assert am.rcad_equal is False
    assert am.position_a == "ortho" and am.position_b == relation
    # and the difference is visible in the local environment, not only the name
    assert am.local_env_match["radius1"] is True
    assert am.local_env_match["radius2"] is False
    assert am.local_env_match["distance_matrix"] is False


# ---------------------------------------------------------------------------
# 3, 4, 5. the alkyne family: same ring, different linker
# ---------------------------------------------------------------------------


def test_P11_02_phenylacetylene_is_phenyl_plus_alkyne():
    f = frags(PHENYLACETYLENE)
    assert sorted(x.fragment_smiles for x in f) == ["C#C", "c1ccccc1"]
    alkyne, benzene = by_smiles(f, "C#C"), ring(f)
    assert alkyne.n_attachment_points == 1
    assert alkyne.rcads == ("*C#C",)
    assert benzene.n_attachment_points == 1
    assert benzene.rcads == ("*c1ccccc1",)


def test_P11_03_diphenylacetylene_is_two_phenyl_joined_by_one_alkyne():
    f = frags(DIPHENYLACETYLENE)
    assert sorted(x.fragment_smiles for x in f) == ["C#C", "c1ccccc1", "c1ccccc1"]
    alkyne = by_smiles(f, "C#C")
    assert alkyne.n_attachment_points == 2
    assert alkyne.rcads == ("*C#C*", "*C#C*")
    assert sum(1 for x in f if x.fragment_smiles == "c1ccccc1") == 2


def test_P11_04_diyne_is_two_phenyl_joined_by_a_butyne_linker():
    f = frags(DIPHENYL_BUTADIYNE)
    assert sorted(x.fragment_smiles for x in f) == ["C#CC#C", "c1ccccc1", "c1ccccc1"]
    linker = by_smiles(f, "C#CC#C")
    assert linker.n_attachment_points == 2
    assert linker.rcads == ("*C#CC#C*", "*C#CC#C*")
    assert sum(1 for x in f if x.fragment_smiles == "c1ccccc1") == 2


def test_P11_05_the_three_share_a_ring_and_differ_only_in_the_linker():
    """The whole point of the attachment layer, in one test."""
    families = {
        "phenylacetylene": (PHENYLACETYLENE, "*C#C", 1),
        "diphenylacetylene": (DIPHENYLACETYLENE, "*C#C*", 2),
        "1,4-diphenyl-1,3-diyne": (DIPHENYL_BUTADIYNE, "*C#CC#C*", 2),
    }

    ring_rcads, linker_rcads = set(), set()
    for name, (smiles, linker_rcad, n_attach) in families.items():
        f = frags(smiles)
        r = ring(f)
        ring_rcads.add(r.rcads[0])
        linkers = [x for x in f if x.fragment_smiles != "c1ccccc1"]
        assert len(linkers) == 1, f"{name} should have exactly one linker"
        assert set(linkers[0].rcads) == {linker_rcad}, f"{name} linker {linkers[0].rcads}"
        assert linkers[0].n_attachment_points == n_attach
        linker_rcads.update(linkers[0].rcads)

    # every member contributes the *same* ring attachment descriptor ...
    assert ring_rcads == {"*c1ccccc1"}
    # ... and a linker descriptor that is different for all three
    assert linker_rcads == {"*C#C", "*C#C*", "*C#CC#C*"}

    # terminality is what separates the two-alkyne linkers
    terminal = by_smiles(frags(PHENYLACETYLENE), "C#C").role
    internal = by_smiles(frags(DIPHENYLACETYLENE), "C#C").role
    diyne = by_smiles(frags(DIPHENYL_BUTADIYNE), "C#CC#C").role
    assert (terminal.coupling_handle_type, terminal.is_terminal) == ("terminal_alkyne", True)
    assert (internal.coupling_handle_type, internal.is_terminal) == ("internal_alkyne", False)
    assert (diyne.coupling_handle_type, diyne.is_terminal) == ("internal_alkyne", False)


def test_P11_06_linker_topology_differs_in_the_local_environment():
    a = by_smiles(frags(DIPHENYLACETYLENE), "C#C")
    b = by_smiles(frags(DIPHENYL_BUTADIYNE), "C#CC#C")
    am = compare_attachments(a, b)
    assert am.local_env_match["radius1"] is True      # both start *C#C
    assert am.local_env_match["radius2"] is False     # but they diverge past it
    assert am.local_env_match["distance_matrix"] is False


# ---------------------------------------------------------------------------
# 6 & 7. benzyl vs phenethyl
# ---------------------------------------------------------------------------


def test_P11_07_benzyl_and_phenethyl_share_a_ring_but_not_a_linker():
    fa, fb = frags(BENZYL_ALCOHOL), frags(PHENETHYL_ALCOHOL)
    ma, mb = normalize(BENZYL_ALCOHOL), normalize(PHENETHYL_ALCOHOL)

    # the ring is genuinely the same
    assert ring(fa).fragment_smiles == ring(fb).fragment_smiles == "c1ccccc1"
    assert ring(fa).rcads == ring(fb).rcads == ("*c1ccccc1",)

    # the scaffold cannot tell them apart
    sc = compare_scaffolds(ma, mb)
    assert sc.relation == "identical"
    assert sc.scaffold_a == sc.scaffold_b == "c1ccccc1"

    # the linker is not
    assert by_smiles(fa, "CO").rcads == ("*CO",)
    assert by_smiles(fb, "CCO").rcads == ("*CCO",)
    assert by_smiles(fa, "CO").n_attachment_points == 1
    assert by_smiles(fb, "CCO").n_attachment_points == 1

    # The common core is toluene: RDKit maximizes covered bonds over covered
    # atoms, so the shared ring plus the benzylic carbon wins over a smaller
    # sliver that would reach further down the chain. That leaves the actual
    # difference, and it is exactly the point -- the hydroxyl sits directly on
    # the ring in benzyl, and one carbon further out in phenethyl.
    core = find_core_match(ma, mb)
    assert core.found is True
    assert core.num_atoms == 7
    assert core.common_substructure_smiles == "Cc1ccccc1"
    assert core.coverage_a == pytest.approx(7 / 8)
    assert core.coverage_b == pytest.approx(7 / 9, abs=1e-4)

    a_diff = [c for c in core.unmatched_components_a]
    b_diff = [c for c in core.unmatched_components_b]
    assert sum(c.heavy_atom_count for c in a_diff) == 1      # just the O
    assert sum(c.heavy_atom_count for c in b_diff) == 2      # the extra C and the O
    # each difference is a connected block that names where it was attached
    assert all(c.canonical_smiles_with_star and c.attachment_note
               for c in a_diff + b_diff)
    # and these are exactly the capping artefacts the fragment formula
    # limitation warns about: a lone O reads H2O, a lone CH2O reads CH4O
    assert [c.formula for c in a_diff] == ["H2O"]


def test_P11_08_benzyl_and_phenethyl_ring_fragments_are_attachment_identical():
    am = compare_attachments(ring(frags(BENZYL_ALCOHOL)), ring(frags(PHENETHYL_ALCOHOL)))
    assert am.rcad_equal is True
    assert am.position_a == am.position_b == "not_applicable"
    assert am.local_env_match["radius3"] is True


# ---------------------------------------------------------------------------
# attachment atom / attachment bond traceability across the whole set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "smiles",
    [O_XYLENE, M_XYLENE, P_XYLENE, PHENYLACETYLENE, DIPHENYLACETYLENE,
     DIPHENYL_BUTADIYNE, BENZYL_ALCOHOL, PHENETHYL_ALCOHOL],
)
def test_P11_09_every_attachment_names_a_real_parent_atom_and_bond(smiles):
    m = normalize(smiles)
    n_atoms = m.descriptors.heavy_atom_count
    n_bonds = len(m.bonds)
    seen = 0
    for f in decompose_molecule(m, "ring_linker"):
        for ap in f.attachments:
            seen += 1
            assert 0 <= ap.parent_atom_idx < n_atoms
            assert 0 <= ap.parent_bond_idx < n_bonds
            bond = m.bonds[ap.parent_bond_idx]
            # the recorded bond really does touch the recorded anchor
            assert ap.parent_atom_idx in (bond.a_idx, bond.b_idx)
            assert bond.order.lower() == ap.bond_order.lower()
            # the far side of that bond is what the fragment left behind
            other = bond.b_idx if bond.a_idx == ap.parent_atom_idx else bond.a_idx
            assert m.atoms[other].symbol in ap.parent_side_elements
            assert ap.local_env.anchor_props["symbol"] == m.atoms[ap.parent_atom_idx].symbol
    assert seen > 0


# ---------------------------------------------------------------------------
# fragment formula: the recorded design limitation
# ---------------------------------------------------------------------------


def test_P11_10_fragment_formula_carries_its_capping_limitation():
    linker = by_smiles(frags(BENZYL_ALCOHOL), "CO")
    assert linker.n_attachment_points == 1
    assert linker.free_valences == 1
    # CH2-OH with an open valence reads CH4O, not the CH3O it would be capped to
    assert linker.formula == "CH4O"
    assert linker.formula_caveat == FRAGMENT_FORMULA_CAVEAT
    assert "capping" in FRAGMENT_FORMULA_CAVEAT.lower()
    assert "heavy_atom_count" in FRAGMENT_FORMULA_CAVEAT


def test_P11_11_free_valences_equal_the_number_of_cut_bonds():
    for smiles in (O_XYLENE, DIPHENYLACETYLENE, DIPHENYL_BUTADIYNE, PHENETHYL_ALCOHOL):
        f = decompose_molecule(normalize(smiles), "ring_linker")
        for frag in f:
            assert frag.free_valences == frag.n_attachment_points
            assert frag.n_rings <= normalize(smiles).descriptors.num_rings

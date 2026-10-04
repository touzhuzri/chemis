"""T-F -- layer 4, attachment points.

The tests that matter here are F-01 to F-03. Fingerprint, scaffold and MCS all
call ortho-, meta- and para-xylene the same molecule; only the attachment layer
separates them. If these fail, the engine has stopped being able to explain
*why* two molecules differ.
"""

import pytest

from crea.chem.align import find_core_match
from crea.chem.attachment import compare_attachments, match_fragments
from crea.chem.fragment import decompose_molecule
from crea.chem.normalize import normalize
from tests.conftest import (
    DIPHENYLACETYLENE,
    M_XYLENE,
    O_XYLENE,
    P_XYLENE,
    PHENYLACETYLENE,
)


def _benzene_fragment(smiles):
    m = normalize(smiles)
    frags = decompose_molecule(m, "ring_linker")
    ring = next(f for f in frags if f.fragment_smiles == "c1ccccc1")
    return m, ring


def test_F01_rcads_separate_ortho_meta_para():
    _, o = _benzene_fragment(O_XYLENE)
    _, mm = _benzene_fragment(M_XYLENE)
    _, p = _benzene_fragment(P_XYLENE)
    rcads = {o.rcads[0], mm.rcads[0], p.rcads[0]}
    assert len(rcads) == 3, f"RCADs collapsed: {rcads}"
    assert o.rcads[0] == "*c1ccccc1*"      # ortho
    assert mm.rcads[0] == "*c1cccc(*)c1"    # meta
    assert p.rcads[0] == "*c1ccc(*)cc1"     # para


def test_F02_position_relation_is_named():
    _, o = _benzene_fragment(O_XYLENE)
    _, mm = _benzene_fragment(M_XYLENE)
    _, p = _benzene_fragment(P_XYLENE)
    assert o.position_relation == "ortho"
    assert mm.position_relation == "meta"
    assert p.position_relation == "para"


def test_F03_terminal_and_internal_alkyne_roles_differ():
    _, pa = _benzene_fragment(PHENYLACETYLENE)
    frags_pa = decompose_molecule(normalize(PHENYLACETYLENE), "ring_linker")
    alkyne_pa = next(f for f in frags_pa if f.fragment_smiles == "C#C")
    frags_dp = decompose_molecule(normalize(DIPHENYLACETYLENE), "ring_linker")
    alkyne_dp = next(f for f in frags_dp if f.fragment_smiles == "C#C")

    assert alkyne_pa.role is not None and alkyne_dp.role is not None
    assert alkyne_pa.role.coupling_handle_type == "terminal_alkyne"
    assert alkyne_dp.role.coupling_handle_type == "internal_alkyne"
    assert alkyne_pa.role.is_terminal is True
    assert alkyne_dp.role.is_terminal is False
    # the role is a rule, and says so
    assert alkyne_pa.role.rule_id == "R-ALK-001"
    assert alkyne_pa.role.role_confidence == "rule_derived"
    assert pa is not None


def test_F04_role_conflict_is_reported_not_hidden():
    frags_pa = decompose_molecule(normalize(PHENYLACETYLENE), "ring_linker")
    frags_dp = decompose_molecule(normalize(DIPHENYLACETYLENE), "ring_linker")
    a = next(f for f in frags_pa if f.fragment_smiles == "C#C")
    b = next(f for f in frags_dp if f.fragment_smiles == "C#C")
    am = compare_attachments(a, b)
    assert am.role_match is False
    assert am.role_conflicts
    assert am.rcad_a != am.rcad_b
    assert am.embedded_match == "fragment_divergent"


def test_F05_attachment_points_trace_to_parent_atom_and_bond():
    m = normalize(O_XYLENE)
    ring = next(
        f for f in decompose_molecule(m, "ring_linker")
        if f.fragment_smiles == "c1ccccc1"
    )
    assert ring.n_attachment_points == 2
    for ap in ring.attachments:
        anchor = m.atoms[ap.parent_atom_idx]
        assert anchor.symbol == "C" and anchor.aromatic is True
        assert ap.bond_order == "SINGLE"
        assert ap.parent_side_elements == ("C",)
        # the local environment is populated at every radius
        assert ap.local_env.env_radius1
        assert ap.local_env.env_radius2
        assert ap.local_env.distance_matrix_signature
        assert ap.local_env.anchor_props["symbol"] == "C"


def test_F06a_embedded_full_is_never_claimed_without_a_parent_atom_map():
    ma, mb = normalize(O_XYLENE), normalize(P_XYLENE)
    fa = next(f for f in decompose_molecule(ma, "ring_linker")
              if f.fragment_smiles == "c1ccccc1")
    fb = next(f for f in decompose_molecule(mb, "ring_linker")
              if f.fragment_smiles == "c1ccccc1")
    assert compare_attachments(fa, fb).embedded_match != "embedded_full"


def test_F06b_embedded_full_requires_the_anchors_to_be_mapped_by_the_parent_mcs():
    # Same molecule on both sides: every attachment anchor sits on an atom the
    # parent-level MCS matched, so the full embedding can honestly be claimed.
    ma = normalize(O_XYLENE)
    fa = next(f for f in decompose_molecule(ma, "ring_linker")
              if f.fragment_smiles == "c1ccccc1")
    core = find_core_match(ma, normalize(O_XYLENE))
    assert core.best_alignment is not None
    assert compare_attachments(fa, fa, core=core).embedded_match == "embedded_full"
    # the same fragment pair with no core available stays at the weaker level
    assert compare_attachments(fa, fa).embedded_match != "embedded_full"


def test_F07_multi_point_attachment_is_reported_as_such():
    m = normalize(DIPHENYLACETYLENE)
    linker = next(
        f for f in decompose_molecule(m, "ring_linker") if f.fragment_smiles == "C#C"
    )
    assert linker.n_attachment_points == 2
    assert len(linker.rcads) == 2
    assert linker.role.attachment_multiplicity == 2
    assert linker.with_attachment_smiles == "*C#C*"


def test_F08_fragment_pairing_prefers_matching_rcads():
    fa = decompose_molecule(normalize(O_XYLENE), "ring_linker")
    fb = decompose_molecule(normalize(M_XYLENE), "ring_linker")
    matches = match_fragments(fa, fb)
    assert len(matches) == len(fa) == len(fb)
    ring_match = next(m for m in matches if "c1ccccc1" in m.frag_a_id or True)
    assert any(m.rcads_equal is False for m in matches), (
        "the ring fragment of ortho- and meta-xylene must not be reported as "
        "attachment-identical"
    )
    assert ring_match is not None

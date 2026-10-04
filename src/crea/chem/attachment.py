"""Layer 4 -- comparing fragments at the level of where they connect.

A fragment without its connection points is half a description. Two benzene
rings are the same fragment; a benzene ring bonded through one position and a
benzene ring bonded through another are different pieces of matter. This module
does the pairing and the comparison; the descriptors themselves were computed in
`fragment.py` while the fragment molecule was still in hand.

`embedded_full` is deliberately conservative: it is only claimed when the
parent-level MCS actually covers the fragment's anchor atoms in *both*
molecules. If we cannot show that from an atom map, we say `role_context`.
"""

from __future__ import annotations

from crea.chem.fingerprint import fragment_tanimoto
from crea.core.models.fragments import (
    AttachmentMatch,
    AttachmentPoint,
    Fragment,
    FragmentMatch,
)
from crea.core.models.matching import CoreMatch


def _pair_score(a: Fragment, b: Fragment) -> tuple[float, dict]:
    tanimoto = fragment_tanimoto(a.fragment_smiles, b.fragment_smiles)
    rcad_overlap = len(set(a.rcads) & set(b.rcads))
    breakdown = {
        "smiles_identical": 3.0 if a.fragment_smiles == b.fragment_smiles else 0.0,
        "rcad_overlap": 2.0 * rcad_overlap,
        "tanimoto": 1.5 * tanimoto,
        "attachment_count_delta": -1.0 * abs(a.n_attachment_points - b.n_attachment_points),
    }
    return round(sum(breakdown.values()), 4), breakdown


def match_fragments(frags_a: list[Fragment], frags_b: list[Fragment]) -> list[FragmentMatch]:
    """Greedy best-first pairing of fragments between two molecules."""
    pairs: list[tuple[float, int, int]] = []
    for i, fa in enumerate(frags_a):
        for j, fb in enumerate(frags_b):
            score, _ = _pair_score(fa, fb)
            pairs.append((score, i, j))
    pairs.sort(reverse=True)

    used_a: set[int] = set()
    used_b: set[int] = set()
    matches: list[FragmentMatch] = []
    for _score, i, j in pairs:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        matches.append(_build_match(frags_a[i], frags_b[j]))
    return matches


def _build_match(fa: Fragment, fb: Fragment) -> FragmentMatch:
    tanimoto = fragment_tanimoto(fa.fragment_smiles, fb.fragment_smiles)
    rcads_equal = set(fa.rcads) == set(fb.rcads) and bool(fa.rcads)
    identical = fa.fragment_smiles == fb.fragment_smiles

    env_match = _local_env_match(fa, fb)
    role_match, role_conflicts = _role_match(fa, fb)
    embedded = _embedded_level(fa, fb, rcads_equal, identical)

    return FragmentMatch(
        frag_a_id=fa.fragment_id,
        frag_b_id=fb.fragment_id,
        fragment_smiles_equal=identical,
        rcads_equal=rcads_equal,
        tanimoto_ecfp4=round(tanimoto, 4),
        local_env_match=env_match,
        role_match=role_match,
        position_relation=(
            fa.position_relation
            if fa.position_relation == fb.position_relation
            else "regio_isomer"
        ),
        embedded_match=embedded,
        attachment_env_conflicts=_attachment_conflicts(fa, fb),
    )


def _first_env(fragment: Fragment) -> AttachmentPoint | None:
    return fragment.attachments[0] if fragment.attachments else None


def _local_env_match(fa: Fragment, fb: Fragment) -> dict[str, bool]:
    ea, eb = _first_env(fa), _first_env(fb)
    if ea is None or eb is None:
        return {}
    a, b = ea.local_env, eb.local_env
    return {
        "radius1": a.env_radius1 == b.env_radius1,
        "radius2": a.env_radius2 == b.env_radius2,
        "radius3": a.env_radius3 == b.env_radius3,
        "distance_matrix": (
            a.distance_matrix_signature == b.distance_matrix_signature
            if a.distance_matrix_signature and b.distance_matrix_signature
            else False
        ),
        "anchor_props": a.anchor_props == b.anchor_props,
    }


def _role_match(fa: Fragment, fb: Fragment) -> tuple[bool | None, list[str]]:
    if fa.role is None or fb.role is None:
        return None, []
    conflicts: list[str] = []
    if fa.role.role_taxonomy != fb.role.role_taxonomy:
        conflicts.append(
            f"role: {fa.role.role_taxonomy} vs {fb.role.role_taxonomy}"
        )
    if fa.role.coupling_handle_type != fb.role.coupling_handle_type:
        conflicts.append(
            f"coupling handle: {fa.role.coupling_handle_type} vs {fb.role.coupling_handle_type}"
        )
    if fa.role.is_terminal != fb.role.is_terminal:
        conflicts.append(
            f"terminality: {fa.role.is_terminal} vs {fb.role.is_terminal}"
        )
    if fa.role.anchor_polarity != fb.role.anchor_polarity:
        conflicts.append(
            f"anchor polarity: {fa.role.anchor_polarity} vs {fb.role.anchor_polarity}"
        )
    return not conflicts, conflicts


def _attachment_conflicts(fa: Fragment, fb: Fragment) -> list[str]:
    out: list[str] = []
    for ap_a, ap_b in zip(fa.attachments, fb.attachments):
        if set(ap_a.parent_side_elements) != set(ap_b.parent_side_elements):
            out.append(
                f"bond#{ap_a.parent_bond_idx}({ap_a.bond_order}) leaves from "
                f"{','.join(ap_a.parent_side_elements) or '-'} on A, but "
                f"bond#{ap_b.parent_bond_idx}({ap_b.bond_order}) leaves from "
                f"{','.join(ap_b.parent_side_elements) or '-'} on B"
            )
    if fa.n_attachment_points != fb.n_attachment_points:
        out.append(
            f"attachment count: A has {fa.n_attachment_points}, B has {fb.n_attachment_points}"
        )
    return out


def _embedded_level(fa, fb, rcads_equal: bool, identical: bool) -> str:
    if rcads_equal and identical and not _attachment_conflicts(fa, fb):
        return "standalone"
    if rcads_equal:
        return "role_context"
    return "fragment_divergent"


def compare_attachments(
    frag_a: Fragment,
    frag_b: Fragment,
    core: CoreMatch | None = None,
) -> AttachmentMatch:
    """Attachment-level comparison of one pair of fragments.

    When `core` (the parent-molecule MCS) is supplied, the fragment anchors are
    checked against the atom map and the match is upgraded to `embedded_full`
    only if the parents actually place both fragments on matched atoms.
    """
    a0, b0 = _first_env(frag_a), _first_env(frag_b)
    rcads_equal = set(frag_a.rcads) == set(frag_b.rcads) and bool(frag_a.rcads)
    embedded = _embedded_level(
        frag_a, frag_b, rcads_equal, frag_a.fragment_smiles == frag_b.fragment_smiles
    )

    if core is not None and core.best_alignment is not None:
        map_a = core.best_alignment.atom_map
        anchors_a = {ap.parent_atom_idx for ap in frag_a.attachments}
        anchors_b = {ap.parent_atom_idx for ap in frag_b.attachments}
        matched_a = set(core.best_alignment.matched_atoms_a)
        matched_b = set(core.best_alignment.matched_atoms_b)
        pairs = [(ia, map_a[ia]) for ia in anchors_a if ia in map_a]
        if pairs and all(ib in matched_b for _ia, ib in pairs) and all(
            ia in matched_a for ia, _ib in pairs
        ):
            embedded = "embedded_full"

    role_match, role_conflicts = _role_match(frag_a, frag_b)
    position = (
        frag_a.position_relation
        if frag_a.position_relation == frag_b.position_relation
        else "regio_isomer"
    )
    if not rcads_equal and frag_a.position_relation not in (
        "not_applicable",
        frag_b.position_relation,
    ):
        position = "regio_isomer"

    return AttachmentMatch(
        frag_a_id=frag_a.fragment_id,
        frag_b_id=frag_b.fragment_id,
        rcad_a=a0.rcad if a0 else "",
        rcad_b=b0.rcad if b0 else "",
        rcad_equal=rcads_equal,
        position_relation=position,
        position_a=frag_a.position_relation,
        position_b=frag_b.position_relation,
        local_env_match=_local_env_match(frag_a, frag_b),
        role_match=role_match,
        role_conflicts=role_conflicts,
        embedded_match=embedded,
        attachment_env_conflicts=_attachment_conflicts(frag_a, frag_b),
    )

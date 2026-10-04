"""Layer 3b / 4 -- fragment decomposition and attachment point annotation.

Two decomposition families are offered, and they answer different questions:

  * `ring_linker` -- cut every bond joining a ring atom to an acyclic atom.
    Yields ring systems plus the acyclic linkers between them. This is the
    decomposition that answers "which parts are shared, and where do they
    connect", and it is what makes ortho- / meta- / para-xylene separable.
  * `brics` / `recap` -- RDKit's synthesizability-aware decompositions. They
    only cut bonds they consider synthetically meaningful, which means a
    molecule with no such bonds (a xylene) stays whole. That is correct
    behaviour, not a bug, and the difference is reported as-is.

Every attachment point records the atom index and bond index *in the parent
molecule*, so a fragment can always be traced back to where it came from.
"""

from __future__ import annotations

import os
from functools import lru_cache

from rdkit import Chem
from rdkit.Chem import BRICS, Recap as RECAP, rdMolDescriptors
from rdkit.Chem import FunctionalGroups

from crea.chem.normalize import rebuild
from crea.core.errors import FragmentationError
from crea.core.models.fragments import (
    AttachmentPoint,
    Fragment,
    LocalEnvironment,
    OriginMethod,
    PositionRelation,
    RoleSignature,
)
from crea.core.models.molecule import Molecule
from crea.chem.version import rdkit_version
from crea.core.provenance import timer

_MAX_DM_ATOMS = 64  # distance-matrix signature is O(n^2) in output size
_BRICS_METHODS = {"brics"}
_METHODS = {"brics", "recap", "ring_linker", "ring_system", "whole"}


# --------------------------------------------------------------------------
# Functional groups
#
# `BaseFeatures.fdef` was tried first and rejected: in this RDKit build it yields
# only pharmacophore-style families (Aromatic, Hydrophobe, Donor, Acceptor), not
# chemical functional groups. The FunctionalGroups hierarchy gives named
# chemical groups (Terminal Alkyne, Boronic Acid, Halogen, Primary aromatic) and
# is what the role rules below are written against.
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _fg_patterns() -> tuple[tuple[str, str], ...]:
    try:
        roots = FunctionalGroups.BuildFuncGroupHierarchy()
    except Exception:  # noqa: BLE001 - hierarchy data is optional
        return ()

    out: list[tuple[str, str]] = []

    def walk(node) -> None:
        smarts = getattr(node, "smarts", None)
        name = getattr(node, "name", None)
        if smarts and name:
            out.append((str(name), str(smarts)))
        for child in (getattr(node, "children", None) or []):
            walk(child)

    for root in roots:
        walk(root)
    return tuple(out)


def _fg_labels(mol, atom_idx: int, radius: int = 2) -> list[str]:
    if mol.GetNumAtoms() == 0:
        return []
    dm = Chem.GetDistanceMatrix(mol)
    labels: list[str] = []
    for name, smarts in _fg_patterns():
        pattern = Chem.MolFromSmarts(smarts)
        if pattern is None:
            continue
        for match in mol.GetSubstructMatches(pattern, uniquify=True, maxMatches=16):
            if any(dm[atom_idx][i] <= radius for i in match):
                labels.append(name)
                break
    return sorted(set(labels))


# --------------------------------------------------------------------------
# Role classification -- explicit rules, each one carrying its id
# --------------------------------------------------------------------------


def _role_signature(piece, anchor_idx: int, n_attachments: int,
                    fg: list[str]) -> RoleSignature:
    atom = piece.GetAtomWithIdx(anchor_idx)
    # `str(GetHybridization())` renders as the full enum path, e.g.
    # "rdkit.Chem.rdchem.HybridizationType.SP"; comparing it against
    # "HYBRIDIZATION_SP" silently never matches, which is how the alkyne rule
    # ended up never firing.
    sp_neighbors = [
        n for n in atom.GetNeighbors()
        if str(n.GetHybridization()).split(".")[-1] == "SP"
    ]

    if sp_neighbors:
        other = sp_neighbors[0]
        terminal = other.GetTotalNumHs() > 0
        return RoleSignature(
            role_taxonomy="alkyne_coupling_handle",
            attachment_multiplicity=n_attachments,
            anchor_polarity="nucleophilic" if terminal else "neutral",
            coupling_handle_type="terminal_alkyne" if terminal else "internal_alkyne",
            is_terminal=terminal,
            rule_id="R-ALK-001",
        )

    halogens = {
        "Br", "Cl", "I", "F"
    }
    aryl_halide = any(
        str(n.GetSymbol()) in halogens for n in atom.GetNeighbors()
    ) and atom.GetIsAromatic()
    if aryl_halide:
        return RoleSignature(
            role_taxonomy="aryl_electrophile",
            attachment_multiplicity=n_attachments,
            anchor_polarity="electrophilic",
            leaving_group_ability=True,
            coupling_handle_type="aryl_halide",
            rule_id="R-ARX-002",
        )

    boronate = any(
        str(n.GetSymbol()) == "B" for n in atom.GetNeighbors()
    )
    if boronate:
        return RoleSignature(
            role_taxonomy="organoboron_handle",
            attachment_multiplicity=n_attachments,
            anchor_polarity="nucleophilic",
            coupling_handle_type="boronic_acid",
            rule_id="R-BORO-004",
        )

    nucleophilic = any(
        lab in ("Basic amine", "Hydroxyl", "Alcohol", "basic amine", "alcohol")
        for lab in fg
    )
    return RoleSignature(
        role_taxonomy="aryl_scaffold" if atom.GetIsAromatic() else "substituent",
        attachment_multiplicity=n_attachments,
        anchor_polarity="nucleophilic" if nucleophilic else "neutral",
        coupling_handle_type="none",
        rule_id="R-ARYL-003" if atom.GetIsAromatic() else "R-GEN-000",
    )


# --------------------------------------------------------------------------
# Attachment point construction
# --------------------------------------------------------------------------


def _submol(mol, keep: set[int]):
    """Sanitized submol of exactly `keep`, preserving ascending atom order."""
    rw = Chem.RWMol(mol)
    for idx in sorted(set(range(mol.GetNumAtoms())) - keep, reverse=True):
        rw.RemoveAtom(idx)
    sub = rw.GetMol()
    try:
        Chem.SanitizeMol(sub)
    except Exception:  # noqa: BLE001 - partial subgraphs may not sanitize
        pass
    return sub


def _kekulized(mol):
    """Copy with aromatic bonds written explicitly.

    Slicing an aromatic ring leaves aromatic atoms in no ring, which makes
    RDKit emit either a warning or a chemically wrong string. Kekulizing first
    means every sliced bond is an explicit single or double, so the environment
    descriptors stay readable and correct.
    """
    m = Chem.Mol(mol)
    try:
        Chem.Kekulize(m, clearAromaticFlags=True)
    except Exception:  # noqa: BLE001 - non-kekulizable input keeps aromatic flags
        pass
    return m


def _env_smiles(mol, anchor_idx: int, radius: int) -> str:
    """SMILES of the atoms within `radius` bonds, written from the anchor.

    `MolFragmentToSmilesWithAtomLabels` is not present in this RDKit build, so
    the environment is materialised as a submol and written rooted at the
    anchor instead. Rooting makes the string describe the environment *from
    the attachment*, which is what distinguishes a fragment that connects
    through one position from one that connects through another.
    """
    try:
        bond_ids = Chem.FindAtomEnvironmentOfRadiusN(mol, radius, anchor_idx, useHs=False)
    except Exception:  # noqa: BLE001 - radius may exceed the component
        return ""
    atoms = {anchor_idx}
    for bidx in bond_ids:
        b = mol.GetBondWithIdx(int(bidx))
        atoms.add(b.GetBeginAtomIdx())
        atoms.add(b.GetEndAtomIdx())
    if not atoms:
        return ""
    source = _kekulized(mol)
    sub = _submol(source, atoms)
    new_anchor = sorted(atoms).index(anchor_idx)
    return Chem.MolToSmiles(sub, rootedAtAtom=new_anchor, canonical=True)


def _distance_signature(mol, anchor_idx: int) -> tuple[int, ...]:
    if mol.GetNumAtoms() > _MAX_DM_ATOMS:
        return ()
    row = Chem.GetDistanceMatrix(mol)[anchor_idx]
    return tuple(int(round(v)) for v in row)


def _local_environment(piece, dummy_idx: int, anchor_idx: int) -> LocalEnvironment:
    anchor = piece.GetAtomWithIdx(anchor_idx)
    try:
        logp, mr = rdMolDescriptors._CalcCrippenContribs(piece)[anchor_idx]
    except Exception:  # noqa: BLE001 - dummy atoms have no contribution
        logp, mr = 0.0, 0.0
    return LocalEnvironment(
        anchor_props={
            "symbol": anchor.GetSymbol(),
            "aromatic": anchor.GetIsAromatic(),
            "formal_charge": anchor.GetFormalCharge(),
            "degree": anchor.GetDegree(),
            "num_hs": anchor.GetTotalNumHs(),
            "hybridization": str(anchor.GetHybridization()).split(".")[-1],
            "is_in_ring": anchor.IsInRing(),
        },
        env_radius1=_env_smiles(piece, anchor_idx, 1),
        env_radius2=_env_smiles(piece, anchor_idx, 2),
        env_radius3=_env_smiles(piece, anchor_idx, 3),
        distance_matrix_signature=_distance_signature(piece, dummy_idx),
        crippen_logp_contrib=round(float(logp), 4),
        crippen_mr_contrib=round(float(mr), 4),
        fg_within_radius2=_fg_labels(piece, anchor_idx, radius=2),
    )


def _rcad(piece, dummy_idx: int) -> str:
    """Rooted canonical SMILES at the attachment dummy.

    This is the descriptor that separates a phenyl fragment bonded at one
    position from the same fragment bonded at another: rooting at the dummy
    makes the string position-sensitive while keeping it canonical.
    """
    try:
        return Chem.MolToSmiles(piece, rootedAtAtom=dummy_idx, canonical=True)
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# Ring geometry -> ortho / meta / para
# --------------------------------------------------------------------------


def _ring_path_length(piece, a: int, b: int) -> int | None:
    """Shortest bond path between two atoms staying inside rings."""
    if not (piece.GetAtomWithIdx(a).IsInRing() and piece.GetAtomWithIdx(b).IsInRing()):
        return None
    dist = Chem.GetDistanceMatrix(piece)
    try:
        return int(round(dist[a][b]))
    except Exception:  # noqa: BLE001
        return None


_LABEL_BY_DISTANCE = {1: "ortho", 2: "meta", 3: "para"}


def _substitution_pattern(piece, dummy_indices: list[int]) -> PositionRelation:
    anchors = [
        next(
            (n.GetIdx() for n in piece.GetAtomWithIdx(d).GetNeighbors()
             if n.GetAtomicNum() != 0),
            None,
        )
        for d in dummy_indices
    ]
    anchors = [a for a in anchors if a is not None]
    if not anchors:
        return "not_applicable"

    distances = []
    for i in range(len(anchors)):
        for j in range(i + 1, len(anchors)):
            d = _ring_path_length(piece, anchors[i], anchors[j])
            if d is not None:
                distances.append(d)
    if not distances:
        return "not_applicable" if len(anchors) < 2 else "different_anchor"

    nearest = min(distances)
    if nearest in _LABEL_BY_DISTANCE:
        return _LABEL_BY_DISTANCE[nearest]  # type: ignore[return-value]
    return "regio_isomer"


# --------------------------------------------------------------------------
# Splitting a molecule into fragments
# --------------------------------------------------------------------------


def _bond_cut_indices(mol, method: str) -> set[int]:
    if method == "brics":
        # `BreakBRICSBonds` returns a Mol with dummies, not bond indices.
        # `FindBRICSBonds` gives exact (atom_i, atom_j) pairs, which map back to
        # bond indices and therefore to the parent's bond vector.
        cut: set[int] = set()
        for (a_idx, b_idx), _labels in BRICS.FindBRICSBonds(mol):
            bond = mol.GetBondBetweenAtoms(a_idx, b_idx)
            if bond is not None:
                cut.add(bond.GetIdx())
        return cut

    if method in ("ring_linker", "ring_system"):
        cut = set()
        for bond in mol.GetBonds():
            a, b = bond.GetBeginAtom(), bond.GetEndAtom()
            if a.IsInRing() != b.IsInRing():
                cut.add(bond.GetIdx())
        if method == "ring_system":
            # also detach acyclic atoms that never join a ring
            for atom in mol.GetAtoms():
                if atom.IsInRing():
                    continue
                for bond in atom.GetBonds():
                    cut.add(bond.GetIdx())
        return cut

    return set()


def _groups(mol, cut: set[int]) -> list[set[int]]:
    parent = list(range(mol.GetNumAtoms()))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for bond in mol.GetBonds():
        if bond.GetIdx() in cut:
            continue
        ra, rb = find(bond.GetBeginAtomIdx()), find(bond.GetEndAtomIdx())
        if ra != rb:
            parent[rb] = ra

    out: dict[int, set[int]] = {}
    for idx in range(mol.GetNumAtoms()):
        out.setdefault(find(idx), set()).add(idx)
    return [g for g in out.values() if g]


def _build_piece(mol, group: set[int], cut: set[int]):
    """Build a fragment Mol containing `group` plus a dummy per cut bond."""
    em = Chem.RWMol()
    idx_map: dict[int, int] = {}
    for orig in sorted(group):
        idx_map[orig] = em.AddAtom(Chem.Atom(mol.GetAtomWithIdx(orig)))

    for bond in mol.GetBonds():
        if bond.GetIdx() in cut:
            continue
        a, b = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if a in idx_map and b in idx_map:
            em.AddBond(idx_map[a], idx_map[b], Chem.BondType(bond.GetBondType()))
            nb = em.GetBondBetweenAtoms(idx_map[a], idx_map[b])
            if nb is not None:
                nb.SetIsAromatic(bond.GetIsAromatic())
                nb.SetStereo(bond.GetStereo())

    raw_attachments = []
    for bond in mol.GetBonds():
        if bond.GetIdx() not in cut:
            continue
        a, b = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if a in idx_map:
            mine, partner = a, b
        elif b in idx_map:
            mine, partner = b, a
        else:
            continue
        dummy = em.AddAtom(Chem.Atom(0))
        em.AddBond(idx_map[mine], dummy, Chem.BondType.SINGLE)
        raw_attachments.append(
            {
                "dummy_idx": dummy,
                "anchor_idx": idx_map[mine],
                "parent_atom_idx": mine,
                "parent_bond_idx": bond.GetIdx(),
                "partner_idx": partner,
                "bond_order": str(bond.GetBondType()).split(".")[-1],
                "is_ring_bond": bond.IsInRing(),
            }
        )

    piece = em.GetMol()
    try:
        Chem.SanitizeMol(piece)
    except Exception:  # noqa: BLE001 - partial subgraphs may not sanitize
        pass
    return piece, idx_map, raw_attachments


def _make_fragment(
    mol,
    molecule: Molecule,
    group: set[int],
    cut: set[int],
    origin: OriginMethod,
    origin_bonds: list[int],
    seq: int,
) -> Fragment:
    piece, _idx_map, raw_aps = _build_piece(mol, group, cut)
    dummy_indices = [a["dummy_idx"] for a in raw_aps]

    attachments: list[AttachmentPoint] = []
    for i, raw in enumerate(sorted(raw_aps, key=lambda r: r["parent_bond_idx"])):
        anchor_idx = raw["anchor_idx"]
        fg = _fg_labels(piece, anchor_idx, radius=2)
        attachments.append(
            AttachmentPoint(
                ap_id=f"ap{i + 1}",
                dummy_idx=raw["dummy_idx"],
                anchor_idx=anchor_idx,
                parent_atom_idx=raw["parent_atom_idx"],
                parent_bond_idx=raw["parent_bond_idx"],
                parent_side_elements=(
                    mol.GetAtomWithIdx(raw["partner_idx"]).GetSymbol(),
                ),
                bond_order=raw["bond_order"],
                is_ring_bond=raw["is_ring_bond"],
                rcad=_rcad(piece, raw["dummy_idx"]),
                local_env=_local_environment(piece, raw["dummy_idx"], anchor_idx),
            )
        )

    plain = Chem.RWMol(piece)
    for idx in sorted(dummy_indices, reverse=True):
        plain.RemoveAtom(idx)
    plain_mol = plain.GetMol()
    try:
        Chem.SanitizeMol(plain_mol)
    except Exception:  # noqa: BLE001
        pass

    role = None
    if attachments:
        role = _role_signature(
            piece, attachments[0].anchor_idx, len(attachments), attachments[0].local_env.fg_within_radius2
        )

    fragment_smiles = Chem.MolToSmiles(plain_mol)
    with_dummy_smiles = Chem.MolToSmiles(piece)

    return Fragment(
        fragment_id=f"{molecule.mol_id}:{origin}:{seq}",
        fragment_smiles=fragment_smiles,
        with_attachment_smiles=with_dummy_smiles,
        n_attachment_points=len(attachments),
        attachments=attachments,
        rcads=tuple(a.rcad for a in attachments),
        origin_method=origin,
        origin_bonds=sorted(origin_bonds),
        formula=rdMolDescriptors.CalcMolFormula(plain_mol),
        free_valences=len(attachments),
        heavy_atom_count=plain_mol.GetNumHeavyAtoms(),
        n_rings=rdMolDescriptors.CalcNumRings(plain_mol),
        fg_labels=sorted({lab for a in attachments for lab in a.local_env.fg_within_radius2}),
        role=role,
        parent_mol_id=molecule.mol_id,
        position_relation=_substitution_pattern(piece, dummy_indices),
    )


def decompose(mol_a, molecule: Molecule, method: str = "ring_linker") -> list[Fragment]:
    """Decompose `mol_a` into fully annotated fragments."""
    if method not in _METHODS:
        raise FragmentationError(
            f"unknown decomposition method {method!r}; expected one of {sorted(_METHODS)}"
        )

    with timer("crea.chem.fragment.decompose", rdkit_version(), method=method) as t:
        if method == "whole":
            return [
                _make_fragment(
                    mol_a, molecule, set(range(mol_a.GetNumAtoms())), set(), "whole", [], 1
                )
            ]

        cut = _bond_cut_indices(mol_a, method)
        if not cut:
            return [
                _make_fragment(
                    mol_a, molecule, set(range(mol_a.GetNumAtoms())), set(),
                    "whole" if method not in _BRICS_METHODS else "brics", [], 1,
                )
            ]

        groups = _groups(mol_a, cut)
        origin: OriginMethod = method  # type: ignore[assignment]
        fragments = []
        for group in groups:
            touching = sorted(
                b.GetIdx()
                for b in mol_a.GetBonds()
                if b.GetIdx() in cut
                and (b.GetBeginAtomIdx() in group or b.GetEndAtomIdx() in group)
            )
            fragments.append(
                _make_fragment(mol_a, molecule, group, cut, origin, touching, 0)
            )
        # stable output regardless of dict iteration order
        fragments.sort(key=lambda f: (-f.heavy_atom_count, f.fragment_smiles))
        for i, f in enumerate(fragments):
            f.fragment_id = f"{molecule.mol_id}:{origin}:{i + 1}"
        return fragments


def decompose_recap(mol_a, molecule: Molecule) -> list[Fragment]:
    """RECAP decomposition, mapped back onto parent atom indices where possible.

    `RecapDecompose` returns a `RecapHierarchyNode`, not a (fragments, labels)
    tuple; the leaves carry the fragment SMILES. An empty dict means RECAP
    found no cleavable bond, which is normal for e.g. an xylene.
    """
    with timer("crea.chem.fragment.decompose_recap", rdkit_version()):
        leaves = RECAP.RecapDecompose(mol_a).GetLeaves()
        out: list[Fragment] = []
        for i, smi in enumerate(sorted(leaves)):
            piece = Chem.MolFromSmiles(smi)
            if piece is None:
                continue
            core_atoms = [
                a.GetIdx() for a in piece.GetAtoms() if a.GetAtomicNum() != 0
            ]
            matches = mol_a.GetSubstructMatches(piece, uniquify=True) if core_atoms else []
            attachments: list[AttachmentPoint] = []
            parent_atoms: list[int] = []
            if len(matches) == 1:
                mapping = matches[0]
                for new_idx, old_idx in enumerate(mapping):
                    parent_atoms.append(old_idx)
                for new_idx, atom in enumerate(piece.GetAtoms()):
                    if atom.GetAtomicNum() != 0:
                        continue
                    anchor = next(
                        (n.GetIdx() for n in atom.GetNeighbors()), None
                    )
                    if anchor is None:
                        continue
                    old_anchor = mapping[anchor]
                    bond_idx = next(
                        (
                            b.GetIdx()
                            for b in mol_a.GetAtomWithIdx(old_anchor).GetBonds()
                        ),
                        -1,
                    )
                    attachments.append(
                        AttachmentPoint(
                            ap_id=f"ap{len(attachments) + 1}",
                            dummy_idx=new_idx,
                            anchor_idx=anchor,
                            parent_atom_idx=old_anchor,
                            parent_bond_idx=bond_idx,
                            parent_side_elements=(),
                            rcad=_rcad(piece, new_idx),
                            local_env=_local_environment(piece, new_idx, anchor),
                        )
                    )
            plain = Chem.RWMol(piece)
            for idx in sorted(
                [a.GetIdx() for a in piece.GetAtoms() if a.GetAtomicNum() == 0],
                reverse=True,
            ):
                plain.RemoveAtom(idx)
            plain_mol = plain.GetMol()
            try:
                Chem.SanitizeMol(plain_mol)
            except Exception:  # noqa: BLE001
                pass
            role = (
                _role_signature(
                    piece, attachments[0].anchor_idx, len(attachments),
                    attachments[0].local_env.fg_within_radius2,
                )
                if attachments
                else None
            )
            out.append(
                Fragment(
                    fragment_id=f"{molecule.mol_id}:recap:{i + 1}",
                    fragment_smiles=Chem.MolToSmiles(plain_mol),
                    with_attachment_smiles=smi,
                    n_attachment_points=len(attachments),
                    attachments=attachments,
                    rcads=tuple(a.rcad for a in attachments),
                    origin_method="recap",
                    origin_bonds=[],
                    formula=rdMolDescriptors.CalcMolFormula(plain_mol),
                    free_valences=len(attachments),
                    heavy_atom_count=plain_mol.GetNumHeavyAtoms(),
                    n_rings=rdMolDescriptors.CalcNumRings(plain_mol),
                    fg_labels=[],
                    role=role,
                    parent_mol_id=molecule.mol_id,
                    position_relation=_substitution_pattern(
                        piece, [a.dummy_idx for a in attachments]
                    ),
                )
            )
        return out


def decompose_molecule(molecule: Molecule, method: str = "ring_linker") -> list[Fragment]:
    """Public entry point. Takes a `Molecule`, returns annotated fragments."""
    mol = rebuild(molecule)
    if method == "recap":
        return decompose_recap(mol, molecule)
    return decompose(mol, molecule, method=method)

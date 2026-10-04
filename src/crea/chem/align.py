"""Layer 2 -- maximum common substructure and atom-level correspondence.

`rdFMCS` gives us the query-side atom map only. The target side has to be
enumerated from the resulting SMARTS pattern and scored, because taking the
first substructure match is not a correspondence -- it is whichever one
RDKit happened to find first. In symmetric molecules there are genuinely
several valid correspondences, and we keep them rather than pretending one is
the answer.

Differences are reported as *connected components with their boundary bonds*,
not as a set of unmatched atom indices, because "atoms 3, 7, 9 differ" is not
an explanation a chemist can use.
"""

from __future__ import annotations

from rdkit import Chem
from rdkit.Chem import rdFMCS, rdMolDescriptors

from crea.chem.normalize import rebuild
from crea.core.errors import AlignmentError
from crea.core.models.matching import (
    AlignmentCandidate,
    CoreMatch,
    MatchConflicts,
    MCSParams,
    UnmatchedComponent,
)
from crea.core.models.molecule import BondRef, Molecule
from crea.chem.version import rdkit_version
from crea.core.provenance import timer

# Explicit, because RDKit's defaults are chemically unsafe here:
# AtomCompare.CompareAny will happily match carbon to oxygen, and
# BondCompare.CompareAny will match a single bond to a double bond.
#
# `CompareOrder` was also rejected: it treats an aromatic bond and a single bond
# as equivalent, so benzene matches cyclohexane. Verified, not assumed.
_PROFILES: dict[str, dict] = {
    "strict": {
        "atomCompare": rdFMCS.AtomCompare.CompareElements,
        "bondCompare": rdFMCS.BondCompare.CompareOrderExact,
        "ringMatchesRingOnly": True,
        "completeRingsOnly": True,
        "matchValences": True,
        "matchChiralTag": True,
        "maximizeBonds": True,
    },
    "relaxed": {
        "atomCompare": rdFMCS.AtomCompare.CompareElements,
        "bondCompare": rdFMCS.BondCompare.CompareOrderExact,
        "ringMatchesRingOnly": True,
        "completeRingsOnly": True,
        "matchValences": False,
        "matchChiralTag": False,
        "maximizeBonds": True,
    },
}


def _find_mcs(mols, params: dict, timeout: int):
    """Call FindMCS, degrading gracefully if an RDKit build lacks a kwarg."""
    try:
        return rdFMCS.FindMCS(mols, timeout=timeout, verbose=False, **params)
    except TypeError:
        trimmed = {k: v for k, v in params.items() if k != "maximizeBonds"}
        return rdFMCS.FindMCS(mols, timeout=timeout, verbose=False, **trimmed)


def _bond_key(bond) -> tuple[int, int]:
    a, b = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
    return (a, b) if a <= b else (b, a)


def _concordance(mol_a, mol_b, atom_map: dict[int, int]) -> float:
    """How well mapped atoms agree on aromaticity / ring membership / degree."""
    if not atom_map:
        return 0.0
    agree = 0
    for ia, ib in atom_map.items():
        aa = mol_a.GetAtomWithIdx(ia)
        ab = mol_b.GetAtomWithIdx(ib)
        sig_a = (aa.GetIsAromatic(), aa.IsInRing(), aa.GetDegree())
        sig_b = (ab.GetIsAromatic(), ab.IsInRing(), ab.GetDegree())
        if sig_a == sig_b:
            agree += 1
    return agree / len(atom_map)


def _score(atom_map: dict[int, int], n_bonds: int, concordance: float) -> tuple[float, dict]:
    breakdown = {
        "matched_bonds": 3.0 * n_bonds,
        "matched_atoms": 1.0 * len(atom_map),
        "environment_concordance": 2.0 * concordance,
    }
    return sum(breakdown.values()), breakdown


def _candidate(mol_a, mol_b, a_match, b_match) -> AlignmentCandidate:
    """Score one (A-match, B-match) pair of the MCS pattern.

    Both sides are enumerated from the pattern rather than taking the first hit:
    in a symmetric molecule several correspondences are legitimate and RDKit
    gives no reason to prefer one. Note the RDKit argument order -- the molecule
    is the receiver and the query is the argument.
    """
    atom_map = {ia: ib for ia, ib in zip(a_match, b_match)}
    matched_a = set(atom_map)
    matched_b = set(b_match)

    bond_map: list[tuple[int, int]] = []
    n_bonds = 0
    for bond in mol_a.GetBonds():
        a, b = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if a in matched_a and b in matched_a:
            n_bonds += 1
            mapped = mol_b.GetBondBetweenAtoms(atom_map[a], atom_map[b])
            if mapped is not None:
                bond_map.append((bond.GetIdx(), mapped.GetIdx()))

    concordance = _concordance(mol_a, mol_b, atom_map)
    score, breakdown = _score(atom_map, n_bonds, concordance)
    return AlignmentCandidate(
        score=round(score, 4),
        score_breakdown={k: round(v, 4) for k, v in breakdown.items()},
        atom_map=dict(sorted(atom_map.items())),
        bond_map=bond_map,
        matched_atoms_a=sorted(matched_a),
        matched_atoms_b=sorted(matched_b),
        element_concordance=round(concordance, 4),
        degree_concordance=round(concordance, 4),
    )


def _block_with_stars(mol, block: set[int], cut: set[int]) -> str:
    """SMILES of a block with a `*` wherever it was cut from the rest.

    `MolFragmentToSmiles` cannot be used here: in this RDKit build it does not
    add boundary dummies and simply truncates the SMILES mid-string, producing
    a chemically wrong string. The fragment molecule is therefore built
    explicitly, the same way fragments are built in `crea.chem.fragment`.
    """
    em = Chem.RWMol()
    idx_map = {o: em.AddAtom(Chem.Atom(mol.GetAtomWithIdx(o))) for o in sorted(block)}
    for bond in mol.GetBonds():
        a, b = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if a in idx_map and b in idx_map:
            em.AddBond(idx_map[a], idx_map[b], Chem.BondType(bond.GetBondType()))
            em.GetBondBetweenAtoms(idx_map[a], idx_map[b]).SetIsAromatic(
                bond.GetIsAromatic()
            )
    for bond in mol.GetBonds():
        if bond.GetIdx() not in cut:
            continue
        a, b = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if (a in idx_map) != (b in idx_map):
            inside = a if a in idx_map else b
            dummy = em.AddAtom(Chem.Atom(0))
            em.AddBond(idx_map[inside], dummy, Chem.BondType.SINGLE)
    piece = em.GetMol()
    try:
        Chem.SanitizeMol(piece)
    except Exception:  # noqa: BLE001
        pass
    return Chem.MolToSmiles(piece)


def _submol(mol, keep: set[int]):
    """Proper sanitized submol containing exactly `keep`."""
    rw = Chem.RWMol(mol)
    for idx in sorted(set(range(mol.GetNumAtoms())) - keep, reverse=True):
        rw.RemoveAtom(idx)
    sub = rw.GetMol()
    try:
        Chem.SanitizeMol(sub)
    except Exception:  # noqa: BLE001 - a partial subgraph may not sanitize
        pass
    return sub


def _bond_refs_for(molecule: Molecule, bond_indices: set[int], side: str) -> list[BondRef]:
    return [b for b in molecule.bonds if b.bond_idx in bond_indices]


def _unmatched_components(
    mol, molecule: Molecule, matched: set[int], side: str
) -> list[UnmatchedComponent]:
    """Connected blocks of unmatched atoms, with their attachment points."""
    unmatched = set(range(mol.GetNumAtoms())) - matched
    components: list[UnmatchedComponent] = []
    seen: set[int] = set()

    for start in sorted(unmatched - seen):
        if start in seen:
            continue
        block: set[int] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur in seen or cur in matched:
                continue
            seen.add(cur)
            block.add(cur)
            for nbr in mol.GetAtomWithIdx(cur).GetNeighbors():
                n = nbr.GetIdx()
                if n not in matched and n not in seen:
                    stack.append(n)

        if not block:
            continue

        internal_bonds = {
            b.GetIdx()
            for b in mol.GetBonds()
            if b.GetBeginAtomIdx() in block and b.GetEndAtomIdx() in block
        }
        boundary_bonds = {
            b.GetIdx()
            for b in mol.GetBonds()
            if (b.GetBeginAtomIdx() in block) != (b.GetEndAtomIdx() in block)
        }
        boundary_atoms = {
            (b.GetEndAtomIdx() if b.GetBeginAtomIdx() in block else b.GetBeginAtomIdx())
            for b in mol.GetBonds()
            if (b.GetBeginAtomIdx() in block) != (b.GetEndAtomIdx() in block)
        }

        sub = _submol(mol, block)
        components.append(
            UnmatchedComponent(
                component_id=f"{side}-d{len(components) + 1}",
                atoms=[molecule.atoms[i] for i in sorted(block)],
                internal_bonds=_bond_refs_for(molecule, internal_bonds, side),
                boundary_bonds=_bond_refs_for(molecule, boundary_bonds, side),
                boundary_atoms=[molecule.atoms[i] for i in sorted(boundary_atoms)],
                formula=rdMolDescriptors.CalcMolFormula(sub),
                heavy_atom_count=len(block),
                n_rings=rdMolDescriptors.CalcNumRings(sub),
                canonical_smiles_with_star=_block_with_stars(mol, block, boundary_bonds),
                attachment_note=_attachment_note(molecule, boundary_bonds, boundary_atoms, side),
            )
        )
    return components


def _attachment_note(molecule, boundary_bonds, boundary_atoms, side: str) -> str:
    if not boundary_bonds:
        return f"isolated from the matched core on side {side}"
    bonds = [b for b in molecule.bonds if b.bond_idx in boundary_bonds]
    atoms = [molecule.atoms[i] for i in sorted(boundary_atoms)]
    parts = []
    for b in bonds:
        anchor = next(
            (a for a in atoms if a.atom_idx in (b.a_idx, b.b_idx)), None
        )
        if anchor is not None:
            parts.append(
                f"bond#{b.bond_idx}({b.order}) to atom#{anchor.atom_idx}"
                f"[{anchor.symbol}{'arom' if anchor.aromatic else ''}]"
            )
    return "; ".join(parts) if parts else f"{len(boundary_bonds)} boundary bond(s)"


def _conflicts(mol_a, mol_b, atom_map: dict[int, int]) -> MatchConflicts:
    out = MatchConflicts()
    for ia, ib in atom_map.items():
        aa, ab = mol_a.GetAtomWithIdx(ia), mol_b.GetAtomWithIdx(ib)
        tag = f"atom A#{ia} <-> B#{ib}"
        if aa.GetFormalCharge() != ab.GetFormalCharge():
            out.charge.append(f"{tag}: {aa.GetFormalCharge()} vs {ab.GetFormalCharge()}")
        if aa.GetTotalValence() != ab.GetTotalValence():
            out.valence.append(
                f"{tag}: valence {aa.GetTotalValence()} vs {ab.GetTotalValence()}"
            )
        if aa.GetIsAromatic() != ab.GetIsAromatic():
            out.aromaticity.append(
                f"{tag}: aromatic {aa.GetIsAromatic()} vs {ab.GetIsAromatic()}"
            )
        if aa.GetChiralTag() != ab.GetChiralTag():
            out.stereo.append(f"{tag}: {aa.GetChiralTag()} vs {ab.GetChiralTag()}")
    return out


def _dedupe(candidates: list[AlignmentCandidate], best: AlignmentCandidate,
            limit: int = 3) -> list[AlignmentCandidate]:
    """Keep only genuinely different correspondences, best-aligned first.

    A symmetric molecule produces the same atom map many times over; those are
    not alternatives, they are the same answer counted repeatedly.
    """
    seen = {tuple(sorted(best.atom_map.items()))}
    out: list[AlignmentCandidate] = []
    for cand in candidates:
        key = tuple(sorted(cand.atom_map.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
        if len(out) >= limit:
            break
    return out


def substructure_contains(container: Molecule, query: Molecule) -> bool:
    """Is `query` a substructure of `container`? (Phase 3.10, §33)

    The chemistry layer's own answer to a chemistry question: services ask
    this instead of touching RDKit, and the RDKit call stays where every
    other one lives. A parse failure answers False -- an unparseable query
    contains nothing.
    """
    mol_container = rebuild(container)
    mol_query = rebuild(query)
    if mol_container is None or mol_query is None:
        return False
    return bool(mol_container.HasSubstructMatch(mol_query))


def find_core_match(
    mol_a: Molecule,
    mol_b: Molecule,
    profile: str = "relaxed",
    timeout: int = 10,
    max_candidates: int = 64,
) -> CoreMatch:
    """Maximum common substructure between two molecules, with atom mapping."""
    if profile not in _PROFILES:
        raise AlignmentError(f"unknown MCS profile {profile!r}; expected one of {list(_PROFILES)}")

    params = _PROFILES[profile]
    mcs_params = MCSParams(
        profile=profile,
        atom_compare=str(params["atomCompare"]).split(".")[-1],
        bond_compare=str(params["bondCompare"]).split(".")[-1],
        ring_matches_ring_only=params["ringMatchesRingOnly"],
        complete_rings_only=params["completeRingsOnly"],
        match_valences=params["matchValences"],
        match_chiral_tag=params["matchChiralTag"],
        maximize_bonds=params["maximizeBonds"],
        timeout_seconds=timeout,
    )

    rdmol_a, rdmol_b = rebuild(mol_a), rebuild(mol_b)

    with timer("crea.chem.align.find_core_match", rdkit_version(),
               profile=profile, timeout=timeout) as t:
        res = _find_mcs([rdmol_a, rdmol_b], params, timeout)

        if res is None or res.numAtoms == 0:
            return CoreMatch(
                found=False,
                params=mcs_params,
                runtime_ms=t.prov.runtime_ms,
            )

        patt = Chem.MolFromSmarts(res.smartsString)
        if patt is None:
            raise AlignmentError(f"could not build pattern from SMARTS: {res.smartsString!r}")

        # This RDKit build's MCSResult carries no atom map back to the query
        # molecule, so both sides are enumerated from the pattern and paired by
        # score. No undocumented ordering assumption is relied upon.
        a_matches = rdmol_a.GetSubstructMatches(
            patt, uniquify=False, maxMatches=max_candidates
        )
        b_matches = rdmol_b.GetSubstructMatches(
            patt, uniquify=False, maxMatches=max_candidates
        )
        if not a_matches or not b_matches:
            raise AlignmentError("MCS pattern produced no match on one of the molecules")

        candidates = [
            _candidate(rdmol_a, rdmol_b, am, bm)
            for am in a_matches
            for bm in b_matches
        ]
        candidates.sort(key=lambda c: c.score, reverse=True)
        best = candidates[0]
        alternatives = _dedupe(candidates[1:], best)

        matched_a = set(best.matched_atoms_a)
        matched_b = set(best.matched_atoms_b)
        heavy_a = mol_a.descriptors.heavy_atom_count
        heavy_b = mol_b.descriptors.heavy_atom_count
        union = heavy_a + heavy_b - len(matched_a)

        return CoreMatch(
            found=True,
            mcs_smarts=res.smartsString,
            num_atoms=res.numAtoms,
            num_bonds=res.numBonds,
            coverage_a=round(len(matched_a) / heavy_a, 4) if heavy_a else 0.0,
            coverage_b=round(len(matched_b) / heavy_b, 4) if heavy_b else 0.0,
            coverage_jaccard=round(len(matched_a) / union, 4) if union else 0.0,
            best_alignment=best,
            alternative_alignments=alternatives,
            unmatched_components_a=_unmatched_components(
                rdmol_a, mol_a, matched_a, "A"
            ),
            unmatched_components_b=_unmatched_components(
                rdmol_b, mol_b, matched_b, "B"
            ),
            common_substructure_smiles=Chem.MolFragmentToSmiles(
                rdmol_a, atomsToUse=sorted(matched_a), canonical=True
            ),
            params=mcs_params,
            conflicts=_conflicts(rdmol_a, rdmol_b, best.atom_map),
            runtime_ms=t.prov.runtime_ms,
        )

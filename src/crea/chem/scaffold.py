"""Layer 3a -- Murcko scaffold comparison.

Caveat that must survive into the report: a Murcko scaffold keeps ring systems
and the linkers between them, and discards everything else. Benzene, toluene,
benzyl and phenylacetylene therefore all have the scaffold `c1ccccc1`. A
scaffold match is evidence of a shared ring skeleton, never evidence that two
molecules are the same or interchangeable.
"""

from __future__ import annotations

from rdkit import Chem
from rdkit.Chem.Scaffolds import MurckoScaffold

from crea.chem.normalize import rebuild
from crea.core.models.matching import ScaffoldMatch
from crea.core.models.molecule import Molecule
from crea.chem.version import rdkit_version
from crea.core.provenance import timer

_CAVEAT = (
    "Murcko scaffolds retain ring systems and the linkers between them and "
    "discard acyclic substituents. Distinct molecules therefore share a "
    "scaffold; this is not an identity claim."
)


def _ring_systems(scaffold) -> list[str]:
    """Individual ring systems of a scaffold, as SMILES.

    Implemented over SymmSSSR rather than `GetRingSystem` so the behaviour does
    not depend on which RDKit build is installed.
    """
    if scaffold is None or scaffold.GetNumAtoms() == 0:
        return []
    rings = [set(r) for r in Chem.GetSymmSSSR(scaffold)]
    if not rings:
        return []

    parent = {i: i for i in range(scaffold.GetNumAtoms())}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for ring in rings:
        it = iter(ring)
        first = next(it)
        for other in it:
            union(first, other)

    groups: dict[int, set[int]] = {}
    for atom in scaffold.GetAtoms():
        if atom.IsInRing():
            groups.setdefault(find(atom.GetIdx()), set()).add(atom.GetIdx())

    out = []
    for atoms in groups.values():
        out.append(
            Chem.MolFragmentToSmiles(scaffold, atomsToUse=sorted(atoms), canonical=True)
        )
    return sorted(out)


def murcko_scaffold_smiles(molecule) -> str:
    """The Murcko scaffold of a normalized `Molecule`, as a SMILES string.

    Public so callers above the chemistry core can reuse it rather than
    reaching for RDKit themselves.
    """
    scaffold, _generic = _scaffold_bits(rebuild(molecule))
    return Chem.MolToSmiles(scaffold) if scaffold is not None else ""


def _scaffold_bits(mol):
    scaffold = MurckoScaffold.GetScaffoldForMol(mol)
    generic = MurckoScaffold.MakeScaffoldGeneric(scaffold) if scaffold is not None else None
    return scaffold, generic


def compare_scaffolds(mol_a: Molecule, mol_b: Molecule) -> ScaffoldMatch:
    with timer("crea.chem.scaffold.compare_scaffolds", rdkit_version()) as t:
        scaf_a, gen_a = _scaffold_bits(rebuild(mol_a))
        scaf_b, gen_b = _scaffold_bits(rebuild(mol_b))

        smi_a = Chem.MolToSmiles(scaf_a) if scaf_a is not None else ""
        smi_b = Chem.MolToSmiles(scaf_b) if scaf_b is not None else ""
        gsmi_a = Chem.MolToSmiles(gen_a) if gen_a is not None else ""
        gsmi_b = Chem.MolToSmiles(gen_b) if gen_b is not None else ""

        if smi_a and smi_a == smi_b:
            relation = "identical"
        elif gsmi_a and gsmi_a == gsmi_b:
            relation = "generic_match"
        elif (
            scaf_a is not None
            and scaf_b is not None
            and (scaf_a.HasSubstructMatch(scaf_b) or scaf_b.HasSubstructMatch(scaf_a))
        ):
            relation = "substructure"
        else:
            rs_a = _ring_systems(scaf_a)
            rs_b = _ring_systems(scaf_b)
            relation = "ring_system_shared" if set(rs_a) & set(rs_b) else "disjoint"

        return ScaffoldMatch(
            scaffold_a=smi_a,
            scaffold_b=smi_b,
            generic_a=gsmi_a,
            generic_b=gsmi_b,
            relation=relation,
            ring_systems_a=_ring_systems(scaf_a),
            ring_systems_b=_ring_systems(scaf_b),
            caveat=_CAVEAT,
        )

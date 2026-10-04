"""3D conformer generation (CREA Stage 2.1).

The one sanctioned place a molecule becomes three-dimensional coordinates.
RDKit ETKDG embeds a conformer; MMFF94 (with UFF fallback) optionally
optimizes it. Nothing here invents a conformer algorithm (§4) -- ETKDG is
RDKit's. Explicit stereochemistry is preserved because the input is
rebuilt from its canonical SMILES (which carries stereo) and ETKDG honours
the specified stereo centres (§11).

The result is the project's own `MoleculeConformer` -- RDKit's `Conformer`
never leaves this module.
"""

from __future__ import annotations

from rdkit import Chem
from rdkit.Chem import AllChem

from crea.chem.normalize import rebuild
from crea.core.errors import ConformerError
from crea.core.models.conformer import (
    ConformerAtom,
    ConformerBond,
    MoleculeConformer,
)
from crea.core.models.molecule import Molecule


def _bond_order(bond) -> str:
    bond_type = bond.GetBondType()
    if bond_type == Chem.BondType.DOUBLE:
        return "double"
    if bond_type == Chem.BondType.TRIPLE:
        return "triple"
    if bond_type == Chem.BondType.AROMATIC:
        return "aromatic"
    return "single"


def generate_conformer(molecule: Molecule, *,
                       optimize: bool = False) -> MoleculeConformer:
    """Embed one representative conformer for a normalized `Molecule`.

    Pipeline: canonical SMILES -> RDKit Mol -> AddHs -> ETKDG embed ->
    optional MMFF94 (fallback UFF) optimization -> extract coordinates.
    """
    mol = rebuild(molecule)
    if mol is None:
        raise ConformerError(
            "CONFORMER_GENERATION_FAILED",
            "the molecule could not be rebuilt from its canonical SMILES")

    mol_with_h = Chem.AddHs(mol)
    try:
        success = AllChem.EmbedMolecule(mol_with_h, AllChem.ETKDGv3())
    except Exception as exc:  # noqa: BLE001 - a failed embed is a typed result
        raise ConformerError(
            "CONFORMER_GENERATION_FAILED",
            f"ETKDG embedding raised: {type(exc).__name__}: {exc}") from exc
    if success != 0:
        raise ConformerError(
            "CONFORMER_GENERATION_FAILED",
            f"ETKDG could not embed a conformer (status {success})")

    optimization_method = ""
    warnings: list[str] = []
    if optimize:
        optimization_method, warnings = _optimize(mol_with_h)

    conformer = mol_with_h.GetConformer()
    atoms = []
    for atom in mol_with_h.GetAtoms():
        position = conformer.GetAtomPosition(atom.GetIdx())
        atoms.append(ConformerAtom(
            atom_index=atom.GetIdx(), element=atom.GetSymbol(),
            atomic_number=atom.GetAtomicNum(),
            x=round(position.x, 4), y=round(position.y, 4),
            z=round(position.z, 4)))
    bonds = [
        ConformerBond(a=bond.GetBeginAtomIdx(), b=bond.GetEndAtomIdx(),
                      order=_bond_order(bond))
        for bond in mol_with_h.GetBonds()
    ]

    return MoleculeConformer(
        canonical_smiles=molecule.canonical_smiles,
        formula=molecule.descriptors.molecular_formula,
        conformer_id=0, generation_method="ETKDG", optimized=optimize,
        optimization_method=optimization_method,
        charge=molecule.descriptors.net_charge,
        atoms=atoms, bonds=bonds, warnings=warnings)


def _optimize(mol) -> tuple[str, list[str]]:
    """MMFF94 first, UFF fallback. A failed optimization is a warning, not a
    crash -- the embedded conformer is still a valid conformer."""
    try:
        AllChem.MMFFOptimizeMolecule(mol)
        return "MMFF94", []
    except Exception:
        pass
    try:
        AllChem.UFFOptimizeMolecule(mol)
        return "UFF", []
    except Exception:
        return "", ["optimization was requested but no force field could be applied"]


__all__ = ["generate_conformer"]

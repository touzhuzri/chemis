"""Structural notations of a normalized molecule (CREA 4.0 Stage B-1).

RDKit is the only thing that turns a `Molecule` into a notation string --
this module is the only place outside `normalize.py` allowed to hold a
Mol for that purpose (`rebuild` is the sanctioned door). What each string
*means* and which entity it belongs to is the semantic layer's question,
not this one.
"""

from __future__ import annotations

from crea.core.models.molecule import Molecule


def isomeric_smiles(molecule: Molecule) -> str:
    """The isomeric SMILES of the structure. Stereo kept, never invented."""
    from rdkit import Chem

    from crea.chem.normalize import rebuild

    mol = rebuild(molecule)
    return Chem.MolToSmiles(mol, isomericSmiles=True) if mol is not None else ""


def molblock(molecule: Molecule) -> str:
    """The MDL molblock (V2000) of the structure.

    Coordinates are RDKit's plain 2D depiction; the block is a notation
    of the graph, not a geometry claim.
    """
    from rdkit import Chem

    from crea.chem.normalize import rebuild

    mol = rebuild(molecule)
    if mol is None:
        return ""
    mol = Chem.AddHs(mol)  # noqa: F841 - keep graph form consistent
    mol = Chem.RemoveHs(mol)
    try:
        from rdkit.Chem import AllChem

        AllChem.Compute2DCoords(mol)
    except Exception:  # noqa: BLE001 - depiction failure is not a chemistry fact
        pass
    return Chem.MolToMolBlock(mol)


def molecule_from_molblock(text: str) -> Molecule:
    """Parse an MDL molblock into a normalized `Molecule`.

    A notation that is not a molblock fails here, explicitly. No repair,
    no guessing at what the file "meant" (§3).
    """
    from rdkit import Chem

    from crea.chem.normalize import normalize_mol

    mol = Chem.MolFromMolBlock(text or "", sanitize=True)
    if mol is None:
        raise ValueError("not a parseable MDL molblock")
    return normalize_mol(mol, "molblock", source="user_input")


__all__ = ["isomeric_smiles", "molecule_from_molblock", "molblock"]

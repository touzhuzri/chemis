"""2D structure depiction -- RDKit draws, the UI only shows (Phase 4 §18).

The single sanctioned place a molecule becomes a picture. Input is a
SMILES string; output is SVG text. No drawing algorithm lives anywhere
else in CREA, and the web layer never sees RDKit -- it receives SVG.

`None` means the structure could not be parsed. The caller turns that
into a structured `INVALID_STRUCTURE` error (§56); a raw RDKit exception
never reaches a user.
"""

from __future__ import annotations


def molecule_svg(smiles: str, *, width: int = 320, height: int = 240) -> str | None:
    """SVG for one structure, or None when the SMILES is not a structure."""
    from rdkit import Chem
    from rdkit.Chem.Draw import rdMolDraw2D

    mol = Chem.MolFromSmiles(smiles or "")
    if mol is None:
        return None
    try:
        drawer = rdMolDraw2D.MolDraw2DSVG(width, height)
        drawer.drawOptions().clearBackground = False
        rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol)
        drawer.FinishDrawing()
        svg = drawer.GetDrawingText()
    except Exception:  # noqa: BLE001 - depiction failure is not a crash
        return None
    return svg or None


__all__ = ["molecule_svg"]

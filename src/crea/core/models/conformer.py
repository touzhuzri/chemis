"""A generated 3D conformer (CREA Stage 2.1).

A conformer is ONE reasonable three-dimensional arrangement produced by a
specific algorithm (RDKit ETKDG, optionally MMFF/UFF-optimized) -- never the
molecule's unique experimental shape (§10). The coordinates here are the
stable data the API/UI consume; the RDKit `Conformer` object never crosses
that boundary (§5).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ConformerAtom(BaseModel):
    atom_index: int
    element: str
    atomic_number: int
    x: float
    y: float
    z: float


class ConformerBond(BaseModel):
    a: int
    b: int
    order: str = "single"


class MoleculeConformer(BaseModel):
    """One generated conformer, coordinates included, RDKit-free."""

    canonical_smiles: str
    formula: str
    conformer_id: int = 0
    generation_method: str = "ETKDG"
    optimized: bool = False
    optimization_method: str = ""
    charge: int = 0
    atoms: list[ConformerAtom] = Field(default_factory=list)
    bonds: list[ConformerBond] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


__all__ = ["ConformerAtom", "ConformerBond", "MoleculeConformer"]

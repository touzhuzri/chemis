"""Molecule-vs-molecule comparison result (CREA Phase 1 Molecule Explorer).

Reuses the existing screening/scaffold/MCS vocabulary. Similarity here is a
screening number (`SimilarityScreen.screening_only`), never identity, never
synthetic feasibility. The result carries both identities, the scores with
their method, the scaffold relation, and the atom-resolved MCS core.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SimilarityResult(BaseModel):
    smiles_a: str
    smiles_b: str
    identity_a: dict = Field(default_factory=dict)
    identity_b: dict = Field(default_factory=dict)
    tanimoto: float = 0.0
    dice: float = 0.0
    method: str = "ecfp4_fp2048"
    fingerprint_radius: int = 2
    fingerprint_size: int = 2048
    scaffold_relation: str = "disjoint"
    mcs: dict = Field(default_factory=dict)
    provenance: dict = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list)


__all__ = ["SimilarityResult"]

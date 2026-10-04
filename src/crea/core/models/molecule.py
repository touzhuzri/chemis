"""Molecular data model.

The traceable representation of a chemical structure. A Molecule always keeps
the input it came from, its normalized form, and the full atom/bond graph, so
that any later statement about it can be traced back to specific atoms.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from crea.core.provenance import Provenance

NormalizationProfile = Literal["identity", "parent", "canonical_tautomer"]
SourceKind = Literal[
    "user_input", "pubchem", "ocr", "literature", "retrosynthesis", "fragment"
]


class SourceRef(BaseModel):
    kind: SourceKind
    detail: str | None = None
    retrieved_at: str | None = None


class AtomRef(BaseModel):
    """One atom, addressed by its index in the parent's atom vector."""

    mol_id: str
    atom_idx: int
    canonical_rank: int = 0
    symbol: str
    atomic_num: int
    aromatic: bool = False
    formal_charge: int = 0
    degree: int = 0
    num_hs: int = 0
    hybridization: str = "UNSPECIFIED"
    is_in_ring: bool = False
    ring_sizes: tuple[int, ...] = ()
    chirality_tag: str = "CHI_UNSPECIFIED"
    is_dummy: bool = False


class BondRef(BaseModel):
    mol_id: str
    bond_idx: int
    a_idx: int
    b_idx: int
    order: str = "single"
    is_in_ring: bool = False
    stereo: str = "STEREONONE"


class Descriptors(BaseModel):
    molecular_formula: str
    exact_mol_wt: float
    mol_wt: float
    heavy_atom_count: int
    num_rings: int
    num_aromatic_rings: int
    tpsa: float
    logp: float
    hbd: int
    hba: int
    rotatable_bonds: int
    fraction_csp3: float
    net_charge: int = 0


class FingerprintSet(BaseModel):
    """Bit vectors.

    These are CACHING ARTEFACTS ONLY. A fingerprint bit is a hashed structural
    feature, not a human-defined chemical part ID, and must never be shown to a
    user as a chemical label. See `SimilarityScreen.screening_only`.
    """

    ecfp4_fp2048: str = ""  # hex bit string
    fcfp4_fp2048: str = ""
    maccs167: str = ""
    generator: str = ""
    radius: int = 2
    fp_size: int = 2048


class Molecule(BaseModel):
    mol_id: str  # canonical InChIKey -- stable across input notations
    source: SourceRef
    input_repr: str
    canonical_smiles: str
    inchi: str
    inchikey: str
    inchikey_skeleton_block: str = ""  # first two InChIKey blocks
    rdkit_version: str
    normalization_profile: NormalizationProfile = "parent"
    normalization_actions: list[str] = Field(default_factory=list)
    atoms: list[AtomRef] = Field(default_factory=list)
    bonds: list[BondRef] = Field(default_factory=list)
    descriptors: Descriptors
    fingerprints: FingerprintSet = Field(default_factory=FingerprintSet)
    warnings: list[str] = Field(default_factory=list)
    provenance: Provenance | None = None

    @property
    def heavy_atom_count(self) -> int:
        return self.descriptors.heavy_atom_count

    def atom(self, idx: int) -> AtomRef:
        return self.atoms[idx]

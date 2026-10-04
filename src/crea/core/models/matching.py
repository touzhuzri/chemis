"""Matching-layer models: screening, MCS alignment, scaffold comparison.

Note the deliberate asymmetry between `SimilarityScreen` and `CoreMatch`:
the first is cheap and unreliable for fine distinctions, the second is
expensive and atom-resolved. The renderer is forbidden from quoting a
screening number as a conclusion without a `CoreMatch` to back it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from crea.core.models.molecule import AtomRef, BondRef
from crea.core.provenance import Provenance

ScaffoldRelation = Literal[
    "identical", "generic_match", "substructure",
    "ring_system_shared", "disjoint",
]


class SimilarityScreen(BaseModel):
    """Layer 1 -- fast screening.

    `screening_only` is typed `Literal[True]` on purpose: this model cannot be
    constructed claiming to be anything but a screening result, so no consumer
    can accidentally treat a Tanimoto score as a structural conclusion.
    """

    screening_only: Literal[True] = True
    tanimoto_ecfp4: float = 0.0
    dice_ecfp4: float = 0.0
    tanimoto_fcfp4: float = 0.0
    tanimoto_maccs: float = 0.0
    formula_identical: bool = False
    heavy_atom_delta: int = 0
    skeleton_block_equal: bool = False
    # Measured, not assumed: the InChIKey skeleton block is the *connectivity*
    # layer, so it differs for positional isomers (o/m/p-xylene have three
    # different blocks) and is equal only for stereoisomers. Conflating the two
    # was a design error caught during API verification.
    constitutional_isomer_hint: bool = False
    stereo_isomer_hint: bool = False
    caveat: str = ""


class MCSParams(BaseModel):
    profile: Literal["strict", "relaxed"] = "relaxed"
    atom_compare: str = "CompareElements"
    bond_compare: str = "CompareOrder"
    ring_matches_ring_only: bool = True
    complete_rings_only: bool = True
    match_valences: bool = False
    match_chiral_tag: bool = False
    maximize_bonds: bool = True
    timeout_seconds: int = 10


class AlignmentCandidate(BaseModel):
    score: float
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    atom_map: dict[int, int] = Field(default_factory=dict)  # A_idx -> B_idx
    bond_map: list[tuple[int, int]] = Field(default_factory=list)
    matched_atoms_a: list[int] = Field(default_factory=list)
    matched_atoms_b: list[int] = Field(default_factory=list)
    element_concordance: float = 0.0
    degree_concordance: float = 0.0


class UnmatchedComponent(BaseModel):
    """A connected region of atoms that did NOT match.

    Differences are reported as connected blocks with their boundary bonds, not
    as a flat list of indices -- a list of indices is not something a chemist
    can read.
    """

    component_id: str
    atoms: list[AtomRef] = Field(default_factory=list)
    internal_bonds: list[BondRef] = Field(default_factory=list)
    boundary_atoms: list[AtomRef] = Field(default_factory=list)
    boundary_bonds: list[BondRef] = Field(default_factory=list)
    formula: str = ""
    heavy_atom_count: int = 0
    n_rings: int = 0
    canonical_smiles_with_star: str = ""
    attachment_note: str = ""


class MatchConflicts(BaseModel):
    valence: list[str] = Field(default_factory=list)
    stereo: list[str] = Field(default_factory=list)
    charge: list[str] = Field(default_factory=list)
    aromaticity: list[str] = Field(default_factory=list)


class CoreMatch(BaseModel):
    """Layer 2 -- atom-resolved common substructure."""

    found: bool = False
    mcs_smarts: str = ""
    num_atoms: int = 0
    num_bonds: int = 0
    coverage_a: float = 0.0
    coverage_b: float = 0.0
    coverage_jaccard: float = 0.0
    best_alignment: AlignmentCandidate | None = None
    alternative_alignments: list[AlignmentCandidate] = Field(default_factory=list)
    unmatched_components_a: list[UnmatchedComponent] = Field(default_factory=list)
    unmatched_components_b: list[UnmatchedComponent] = Field(default_factory=list)
    common_substructure_smiles: str = ""
    params: MCSParams = Field(default_factory=MCSParams)
    conflicts: MatchConflicts = Field(default_factory=MatchConflicts)
    runtime_ms: int = 0


class ScaffoldMatch(BaseModel):
    """Layer 3a -- Murcko scaffold comparison."""

    scaffold_a: str = ""
    scaffold_b: str = ""
    generic_a: str = ""
    generic_b: str = ""
    relation: ScaffoldRelation = "disjoint"
    ring_systems_a: list[str] = Field(default_factory=list)
    ring_systems_b: list[str] = Field(default_factory=list)
    caveat: str = ""

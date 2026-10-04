"""Fragment, attachment point, local environment and structural role.

This is where the engine earns its keep. Two molecules can share a scaffold,
share a fingerprint, share an MCS, and still be chemically different -- because
the *point at which they connect* differs. A phenyl fragment with one bond
leaving one position is not the same thing as a phenyl fragment with that bond
leaving another position.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

OriginMethod = Literal[
    "brics", "recap", "scaffold", "ring_system", "ring_linker", "manual", "whole"
]

# Known design limitation, carried in the model rather than only in prose.
FRAGMENT_FORMULA_CAVEAT = (
    "Fragment.formula is the formula of the fragment treated as a standalone "
    "molecule, NOT the formula it would have once re-capped. RDKit fills each "
    "broken bond's free valence with an implicit hydrogen, so the CH2-OH "
    "linker of benzyl alcohol reads 'CH4O' rather than the 'CH3O' it would "
    "become on capping, and an isolated methyl reads 'CH4'. Capping atoms are "
    "not modelled. For composition use heavy_atom_count and "
    "n_attachment_points; treat formula as an approximate label only."
)
PositionRelation = Literal[
    "identical", "ortho", "meta", "para", "regio_isomer",
    "different_anchor", "not_applicable",
]
EmbeddedMatch = Literal["standalone", "role_context", "embedded_full", "fragment_divergent"]


class LocalEnvironment(BaseModel):
    """Environment around an attachment anchor, from tight to complete.

    `distance_matrix_signature` is the complete topological description and is
    included as a backstop: local SMILES environments can in principle be
    ambiguous after canonicalization, the distance vector cannot.
    """

    anchor_props: dict = Field(default_factory=dict)
    env_radius1: str = ""
    env_radius2: str = ""
    env_radius3: str = ""
    distance_matrix_signature: tuple[int, ...] = ()
    crippen_logp_contrib: float = 0.0
    crippen_mr_contrib: float = 0.0
    fg_within_radius2: list[str] = Field(default_factory=list)


class RoleSignature(BaseModel):
    """Rule-derived structural role of a fragment in synthesis.

    Every field carries the rule that produced it. This is a heuristic, not a
    chemical fact, and is rendered as such.
    """

    role_taxonomy: str = "unspecified"
    attachment_multiplicity: int = 1
    anchor_polarity: Literal[
        "electrophilic", "nucleophilic", "neutral", "ambident"
    ] = "neutral"
    leaving_group_ability: bool = False
    coupling_handle_type: str = "none"
    is_terminal: bool = True
    rule_id: str = "R-GEN-000"
    role_confidence: Literal["rule_derived", "inferred"] = "rule_derived"


class AttachmentPoint(BaseModel):
    """Where and how a fragment connects to the rest of a molecule."""

    ap_id: str
    dummy_idx: int          # index of the [*] within the fragment
    anchor_idx: int         # anchor atom inside the fragment
    parent_atom_idx: int    # atom index in the parent molecule  <-- traceability
    parent_bond_idx: int    # bond index in the parent molecule <-- traceability
    parent_side_smiles: str = ""  # the substructure on the far side
    parent_side_elements: tuple[str, ...] = ()
    bond_order: str = "single"
    is_ring_bond: bool = False
    rcad: str = ""          # rooted canonical attachment descriptor
    origin_env: str | None = None
    local_env: LocalEnvironment = Field(default_factory=LocalEnvironment)


class Fragment(BaseModel):
    fragment_id: str
    fragment_smiles: str            # canonical, no [*]
    with_attachment_smiles: str     # contains [*] -- the "role SMILES"
    n_attachment_points: int = 0
    attachments: list[AttachmentPoint] = Field(default_factory=list)
    rcads: tuple[str, ...] = ()     # one per attachment point
    origin_method: OriginMethod = "manual"
    origin_bonds: list[int] = Field(default_factory=list)
    formula: str = ""
    # Number of bonds that were cut. Each one leaves an open valence that a
    # real fragment would be capped at; the model does not model the cap.
    free_valences: int = 0
    formula_caveat: str = FRAGMENT_FORMULA_CAVEAT
    heavy_atom_count: int = 0
    n_rings: int = 0
    fg_labels: list[str] = Field(default_factory=list)
    role: RoleSignature | None = None
    parent_mol_id: str = ""
    position_relation: PositionRelation = "not_applicable"


class FragmentMatch(BaseModel):
    """A fragment of A compared to a fragment of B."""

    frag_a_id: str
    frag_b_id: str
    fragment_smiles_equal: bool = False
    rcads_equal: bool = False
    tanimoto_ecfp4: float = 0.0
    local_env_match: dict[str, bool] = Field(default_factory=dict)
    role_match: bool | None = None
    position_relation: PositionRelation = "not_applicable"
    embedded_match: EmbeddedMatch = "fragment_divergent"
    attachment_env_conflicts: list[str] = Field(default_factory=list)


class AttachmentMatch(BaseModel):
    """The attachment-level comparison -- the layer that separates the
    ortho/meta/para isomers that every cheaper layer calls identical."""

    frag_a_id: str
    frag_b_id: str
    rcad_a: str = ""
    rcad_b: str = ""
    rcad_equal: bool = False
    position_relation: PositionRelation = "not_applicable"
    # Each side keeps its own geometry, so a para/ortho mismatch is legible
    # rather than being flattened into a single "differs" verdict.
    position_a: PositionRelation = "not_applicable"
    position_b: PositionRelation = "not_applicable"
    local_env_match: dict[str, bool] = Field(default_factory=dict)
    role_match: bool | None = None
    role_conflicts: list[str] = Field(default_factory=list)
    embedded_match: EmbeddedMatch = "fragment_divergent"
    attachment_env_conflicts: list[str] = Field(default_factory=list)

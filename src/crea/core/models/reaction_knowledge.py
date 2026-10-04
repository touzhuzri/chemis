"""The reaction as the knowledge chain knows it.

Phase 3.9. A `ReactionRecord` is what a document's procedure text said,
validated field by field. This module is the next link: the *reaction* those
facts describe -- every component attached to a `ChemicalMention`, every
resolved mention attached to a `ChemicalEntity`, the whole thing pointing
back at the paper, the document and the evidence it came from.

The chain, end to end:

    computed facts -> structured extraction -> identity resolution
        -> evidence validation -> reaction object -> literature linkage
        -> route linkage -> report

and the division of labour that keeps it honest: an LLM extracts candidate
information; it is never the final source of a fact. Every component here
reached its role through a policy-validated record and its identity through
the identity layer -- never through a model writing a SMILES.

What a `Reaction` must answer (§62): which paper, which document, which
evidence, which mention produced each component, which entity was adopted.
When a layer is missing the reaction is `PARTIAL`, not quietly complete.

`ReactionStatus` is a description of how far the *evidence* got. It is not
a score and there is no confidence: `CANDIDATE` was proposed, `STRUCTURED`
survived deterministic validation, `EVIDENCE_BACKED` has quote-level
evidence behind it, `EXPERIMENTALLY_SUPPORTED` has an experimental record
plus its evidence plus structure, `PARTIAL` exists but has unresolvable
parts. Metadata that mentions a reaction never climbs these rungs (§11).

The reaction key (`crea-reaction-key-v1`) is a deduplication aid and says
so: it is built from identity keys and ordered component sets, never from
`hash(raw_text)`, and it represents neither mechanism nor reaction
equivalence. Two reactions that might differ stay two reactions.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.models.entity import ChemicalMention
from crea.core.models.evidence import Locator
from crea.core.models.reaction import ComponentRole
from crea.core.models.reaction_linking import LinkBasis
from crea.core.models.route import ReactionMatchType
from crea.core.provenance import Provenance, utc_now_iso

#: Bumped when the reaction key derivation changes meaning. A key minted
#: under one version may need re-derivation under the next.
REACTION_KEY_VERSION = "crea-reaction-key-v1"


class ReactionStatus(str, Enum):
    """How far this reaction's support got. Never a confidence (§10)."""

    #: A model or parser proposed it and nothing more.
    CANDIDATE = "candidate"
    #: The structured fields passed deterministic validation.
    STRUCTURED = "structured"
    #: Quote-level evidence stands behind it (a passage, not a title).
    EVIDENCE_BACKED = "evidence_backed"
    #: An experimental record plus its evidence plus established structures.
    EXPERIMENTALLY_SUPPORTED = "experimentally_supported"
    #: It exists, but parts of it could not be resolved. Kept, not dropped.
    PARTIAL = "partial"


class ReactionComponent(BaseModel):
    """One substance in a reaction, with its full provenance chain.

    `role` is what the source said it was doing -- a policy-validated
    `ComponentRole`, `UNKNOWN` when the source was unclear and nothing
    invented a role (§7). `mention` is the occurrence that named it,
    `entity_id` the entity that occurrence resolved to (§13), `identity_key`
    the key of that entity (§24). A component whose mention stayed
    unresolved keeps no entity -- and the reaction stays `PARTIAL`.
    """

    role: ComponentRole = ComponentRole.UNKNOWN
    name: str = ""
    mention: ChemicalMention | None = None
    entity_id: str | None = None
    identity_key: str = ""
    #: The established structure in SMILES form, for deterministic
    #: comparisons (the identity key is InChI-based; matching reads SMILES).
    canonical_smiles: str = ""

    def key_material(self) -> str:
        """What this component contributes to the reaction key.

        An established identity contributes its identity key. An unresolved
        name contributes its normalised spelling, *marked* as name-based --
        a key built from names can never merge two reactions (§22).
        """
        if self.identity_key:
            return f"e:{self.identity_key}"
        # Identity normalization (CREA 4.0 §12): the one canonical spelling
        # rule for key material. Inlined form retired -- one home in
        # `core.naming`, so a future change changes every key at once.
        from crea.core.naming import normalize_for_identity

        return f"name:{normalize_for_identity(self.name)}"


class Reaction(BaseModel):
    """One reaction, as the knowledge chain established it.

    Components are grouped by the role the source gave them; `products` may
    hold a mention that never resolved (`compound 8`), and the reaction still
    exists (§9). Provenance answers the §62 questions: `paper_id`/`doi`/
    `source_id` (which paper), `document_id` (which document),
    `evidence_refs` (which evidence), and per component its `mention` and
    `entity_id` (which mention, which entity).
    """

    reaction_id: str
    reaction_key: str = ""
    reaction_key_version: str = REACTION_KEY_VERSION

    reactants: list[ReactionComponent] = Field(default_factory=list)
    products: list[ReactionComponent] = Field(default_factory=list)
    reagents: list[ReactionComponent] = Field(default_factory=list)
    catalysts: list[ReactionComponent] = Field(default_factory=list)
    solvents: list[ReactionComponent] = Field(default_factory=list)
    #: Substances the source named without saying what they do (§7).
    unassigned: list[ReactionComponent] = Field(default_factory=list)

    status: ReactionStatus = ReactionStatus.CANDIDATE
    record_id: str = ""
    #: What the source called this transformation, when it said (Stage D).
    #: A label from the source or the extraction -- never inferred.
    reaction_class: str = ""
    #: Atom mapping, when a mapper produced one (Stage D §8: absent is
    #: normal; a reaction without mapping is a complete reaction). Keyed
    #: mapping data only -- no algorithm here.
    atom_mapping: dict | None = None

    paper_id: str = ""
    doi: str = ""
    source_id: str = ""
    document_id: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    unresolved_evidence_refs: list[str] = Field(default_factory=list)
    locator: Locator | None = None
    provenance: Provenance | None = None

    notes: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)

    def components(self) -> list[ReactionComponent]:
        return (self.reactants + self.products + self.reagents
                + self.catalysts + self.solvents + self.unassigned)

    def structures(self) -> list[str]:
        """Established structures of the components, in role order."""
        return [c.identity_key for c in self.components() if c.identity_key]

    def paper_scope(self) -> str:
        """The paper-level scope this reaction was reported in (§21).

        Two reports of one chemistry in two papers are two reactions: the
        same words in another paper may mean another experiment.
        """
        return self.doi or self.paper_id or self.source_id


def reaction_key(reactants: list[str], products: list[str],
                 others: list[str] = (), representation: str = "") -> str:
    """The deterministic deduplication key for a reaction (§22).

    Built from identity-key materials (`ReactionComponent.key_material`)
    with every list sorted, so reagent/solvent ordering never forks a
    reaction, plus the reaction representation when a source gave one. The
    name-based materials (`name:...`) make the key *tainted*: a key derived
    from names is a deduplication aid, not identity, and the registry never
    merges on it.

    What this key is not (§22): not `hash(raw_text)`, not a mechanism, not a
    claim of reaction equivalence. It is a database aid, versioned so a
    later derivation says which reactions must be re-keyed.
    """
    def joined(items: list[str]) -> str:
        return ",".join(sorted(items))

    return (f"{REACTION_KEY_VERSION}:"
            f"R={joined(reactants)}|P={joined(products)}"
            f"|O={joined(others)}|S={representation or ''}")


def key_is_identity_based(reaction_key: str) -> bool:
    """True only when every component of the key came from an identity key.

    A `name:` material anywhere in the key means the key was built from
    words; merging on it would weld two reactions together because two
    papers spelled things the same way.
    """
    return "name:" not in reaction_key


class RouteStepReactionLink(BaseModel):
    """This retrosynthesis step may correspond to that literature reaction.

    `match_type` is what the deterministic comparison found; `link_basis`
    what that comparison established; `screening_only` is True whenever the
    match is anything short of exact -- similarity is a discovery/filtering
    tool and never proof of reaction equivalence (§35). A link is a record
    of a comparison, never a promotion: support levels move only through
    `RouteValidationService` and `RouteEvidenceService` (§38).
    """

    link_id: str
    step_id: str
    reaction_id: str
    match_type: ReactionMatchType = ReactionMatchType.NO_MATCH
    link_basis: LinkBasis = LinkBasis.UNVERIFIED_STRUCTURE
    screening_only: bool = True
    source_id: str = ""
    doi: str = ""
    notes: list[str] = Field(default_factory=list)


class ReactionSearchResult(BaseModel):
    """Why this reaction came back (Stage D §13): the reaction itself,
    the entities the query matched and in which role, its evidence and
    its provenance. An agent must never have to guess why a result is a
    result -- and `diagnostics` says what is missing (no mapping, partial
    resolution) instead of hiding it."""

    reaction: Reaction
    matched_entities: list[str] = Field(default_factory=list)
    #: "product" | "reactant" | "component" -- which relation the query hit.
    match_role: str = "component"
    evidence: list[dict] = Field(default_factory=list)
    provenance: dict | None = None
    diagnostics: list[str] = Field(default_factory=list)


__all__ = [
    "REACTION_KEY_VERSION",
    "Reaction",
    "ReactionComponent",
    "ReactionSearchResult",
    "ReactionStatus",
    "RouteStepReactionLink",
    "key_is_identity_based",
    "reaction_key",
]

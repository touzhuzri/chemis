"""Chemical entities: one stable internal id per established identity.

Phase 3.8.1. A paper may name a compound as `phenylacetylene`,
`ethynylbenzene`, a SMILES, a PubChem CID or an InChIKey. Those are strings
until something says what they refer to. This module is the vocabulary for
the thing they refer to: `ChemicalEntity`, an identity the resolution layer
established, carrying its structure, its names, its external identifiers and
where each of those came from.

The hard rule, the one everything else in this file serves:

> A name is never an identity. `CHEM-00000001` is a resolved chemical
> entity -- never "whatever phenylacetylene means to someone".

So a `ChemicalEntity` can only be minted from a `ChemicalIdentityResolution`
that reached `RESOLVED` (see `crea.services.entity_registry`). Ambiguous,
unavailable and unresolved stays as it was: candidates and refusals. There
is no path here from a word to a structure.

`entity_id` is CREA's own stable identifier -- `CHEM-00000001`, `CHEM-00000002`
-- not a structure and not a database record. It is allocated in registration
order, it does not change when the world is re-sorted, and it does not change
when a program restarts and re-registers the same identities in the same
order. A PubChem CID stays an `ExternalIdentifier`; it is never promoted to
an internal id.

The identity key is derived from the standard InChI of the established
structure, with a version stamp (`IDENTITY_KEY_VERSION`) so a later change to
identity policy can say which keys must be re-derived. The derivation
reproduces Phase 3.6's semantics exactly rather than inventing new ones:
`crea.services.identity.measure_facets` compares the InChI layers
(`/c`, `/h`, `/t /m /s /b`, `/i`, `/q /p`), so two candidates with the same
standard InChI are one identity under that policy -- tautomers that InChI's
mobile-H treatment merges, merged; protonation states it separates,
separated. A key that disagrees with the policy would split one entity into
two or weld two into one, and both are the failures this phase exists to
prevent.

This is `core`. No RDKit, no provider, no network: by the time anything
reaches this module, the chemistry layer has already parsed the structure
and the identity service has already decided what it is.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from crea.core.models.evidence import Locator
from crea.core.models.identity import (
    ChemicalIdentityCandidate,
    IdentityBasis,
    IdentityFacets,
    StructureResolutionStatus,
)
from crea.core.provenance import Provenance, utc_now_iso

#: Prefix and width of the internal entity id. Fixed so the id is readable,
#: sortable as text, and visibly *not* a database key or a CID.
ENTITY_ID_PREFIX = "CHEM-"
ENTITY_ID_WIDTH = 8

#: Bumped whenever the identity key derivation changes meaning. An entity
#: minted under one version may need re-resolution under the next; this
#: constant says which derivation produced its key.
IDENTITY_KEY_VERSION = "crea-identity-key-v1"


def format_entity_id(index: int) -> str:
    """`1` -> `CHEM-00000001`. Allocation order is the whole story of the number."""
    if index < 1:
        raise ValueError("entity indices start at 1")
    return f"{ENTITY_ID_PREFIX}{index:0{ENTITY_ID_WIDTH}d}"


def identity_key(inchi: str) -> str:
    """The stable key for an established identity.

    Derived from the standard InChI -- the same string Phase 3.6's facet
    comparison is built on -- and stamped with the version so a later policy
    change is visible in every key it affected. A name or any other string
    yields no key rather than a key built from something weaker: an entity
    whose key came from a word would be exactly the failure this layer
    prevents.
    """
    cleaned = (inchi or "").strip()
    if not cleaned.startswith("InChI="):
        raise ValueError(
            "an identity key needs a standard InChI; a name or a guess is "
            "not an identity substrate")
    return f"{IDENTITY_KEY_VERSION}:{cleaned}"


def identity_key_of(candidate: ChemicalIdentityCandidate) -> str:
    """Key for a resolved candidate. Requires the InChI the policy compares."""
    return identity_key(candidate.inchi)


class EntityRegistrationError(Exception):
    """Refusing to mint an entity is a decision, and it says why.

    `status` is the resolution status that caused the refusal -- `AMBIGUOUS`
    is a different refusal from `UNAVAILABLE`, and both are different from a
    resolution that arrived with no structure to key on (`code=
    "NO_IDENTITY_KEY"`).
    """

    def __init__(self, message: str, *,
                 status: StructureResolutionStatus | None = None,
                 code: str = "NOT_RESOLVED") -> None:
        self.status = status
        self.code = code
        super().__init__(message)


class EntityNotFound(LookupError):
    """Named an entity the registry does not hold. Never a silent new one."""


class ExternalIdentifier(BaseModel):
    """A name some *other* system gave this entity.

    `namespace` is whose scheme it is (`pubchem`), `identifier` is the value
    in that scheme (`733`), `source` is who told us the pairing. An external
    identifier is a pointer outward; it is never an internal id and never a
    structure.
    """

    namespace: str
    identifier: str
    source: str = ""

    def key(self) -> tuple[str, str]:
        return (self.namespace, self.identifier)


#: The structural notations a `ChemicalRepresentation` may carry. CREA 4.0
#: (Stage B-1): the entity owns a *set* of representations, each typed and
#: sourced -- and all of them describe the one entity, never forking it.
REPRESENTATION_KINDS = (
    "canonical_smiles", "isomeric_smiles", "standard_inchi",
    "inchikey", "molblock",
)


class ChemicalRepresentation(BaseModel):
    """One structural notation of this entity, with where it came from.

    Additive (CREA 4.0 §3). Two representations that parse to the same
    identity are two views of ONE entity; a representation that cannot be
    parsed fails explicitly and is never stored as if it were a structure.
    """

    kind: str  # one of REPRESENTATION_KINDS
    value: str
    source: str = ""        # "rdkit", "user_input", "pubchem", "opsin", ...
    source_id: str = ""
    retrieved_at: str | None = None


class ChemicalEntity(BaseModel):
    """One chemical identity, established -- and everything attached to it.

    The fields group as the concept does: `identity` (the key, the basis on
    which it was established, the facets that justify that basis),
    `structure` (the notation itself), `names` (labels), the external
    identifiers other systems use, and provenance for every one of those.
    """

    entity_id: str
    #: The identity key this entity is registered under.
    identity_key: str
    identity_key_version: str = IDENTITY_KEY_VERSION
    identity_basis: IdentityBasis = IdentityBasis.UNKNOWN
    #: Phase 3.6's own facets, reused rather than redefined: connectivity,
    #: stereo, isotope, tautomer mean exactly what the identity layer says.
    facets: IdentityFacets = Field(default_factory=IdentityFacets)

    canonical_smiles: str = ""
    inchi: str = ""
    inchikey: str = ""
    molecular_formula: str | None = None
    molecular_weight: float | None = None

    #: Labels only. Order preserved, duplicates ignored; a name can be added
    #: to an entity and can never make one.
    names: list[str] = Field(default_factory=list)
    external_identifiers: list[ExternalIdentifier] = Field(default_factory=list)
    #: CREA 4.0 (Stage B-1): the structural notations of THIS entity --
    #: canonical/isomeric SMILES, standard InChI, InChIKey, molblock -- each
    #: typed and sourced. Extra representations never mean an extra entity.
    representations: list[ChemicalRepresentation] = Field(default_factory=list)
    #: Per-field provenance (§2 of the migration): which source established
    #: this field. Grown as fields are established; absent means "the
    #: entity's own provenance covers it", never "invented".
    field_provenance: dict[str, Provenance] = Field(default_factory=dict)

    #: Same vocabulary as `Evidence`: which source established the identity,
    #: which record of theirs, where to look it up, when we looked.
    identity_source: str = ""
    source_id: str = ""
    source_url: str | None = None
    retrieved_at: str | None = None
    provenance: Provenance | None = None

    notes: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)

    def add_name(self, name: str | None) -> bool:
        """Attach a label. Returns True when it was new. Names never identify."""
        cleaned = (name or "").strip()
        if not cleaned or cleaned in self.names:
            return False
        self.names.append(cleaned)
        return True

    def add_external_identifier(self, identifier: ExternalIdentifier) -> bool:
        for existing in self.external_identifiers:
            if existing.key() == identifier.key():
                return False
        self.external_identifiers.append(identifier)
        return True


class MentionResolutionStatus(str, Enum):
    """How far a text mention got. A mention is not an entity.

    Future surface (Phase 3.8+): `Paper -> ChemicalMention -> ChemicalEntity`.
    The model exists so the boundary is fixed before any PDF parsing exists.

    Four outcomes, never more: `RESOLVED` (this mention means that entity),
    `AMBIGUOUS` (several entities/structures survive; none chosen),
    `NOT_FOUND` (a trusted source was consulted and holds nothing), and
    `UNRESOLVED` (context or evidence is still missing). Guessing is not an
    outcome.
    """

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    UNRESOLVED = "unresolved"


#: A cap on how much surrounding text a mention may carry. Context is a
#: pointer to where the mention was said, not evidence in itself, and a
#: mention that stored half a paper would pretend otherwise.
MAX_CONTEXT_CHARS = 2000


class ChemicalMention(BaseModel):
    """One occurrence of a chemical name or notation in some source text.

    `raw_text` is what the text said; `normalized_text` is tidy spelling of
    the same string; `entity_id` is set only when resolution actually
    established which entity it means. A mention with no established entity
    keeps `UNRESOLVED` / `AMBIGUOUS` / `NOT_FOUND` -- exactly like the
    identity layer -- and the model refuses to carry both a non-resolved
    status and an entity id, because that pair is a guess wearing an id as a
    disguise.

    Phase 3.8.2 additions, all additive:

    * `explicit_smiles` / `explicit_inchi` -- a structure the *user* attached
      to this mention. Explicit structure outranks name resolution (§23).
    * `context_before` / `context_after` -- a bounded window of surrounding
      text (§14). Context can point at a definition; it is not evidence.
    * `source_id` -- the paper/document this mention lives in. Numbered
      references like "compound 7" are paper-local (§17): the same two words
      in another paper may mean another compound, so scope travels with the
      mention.
    """

    model_config = ConfigDict(populate_by_name=True)

    mention_id: str
    #: What the text said. `text=` is accepted as an alias for it.
    raw_text: str = Field(alias="text")
    normalized_text: str = ""
    entity_id: str | None = None
    resolution_status: MentionResolutionStatus = MentionResolutionStatus.UNRESOLVED
    source: str = ""
    source_id: str = ""
    locator: Locator | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    explicit_smiles: str = ""
    explicit_inchi: str = ""
    context_before: str = ""
    context_after: str = ""

    @model_validator(mode="after")
    def _entity_requires_resolution(self) -> "ChemicalMention":
        if self.entity_id and self.resolution_status is not MentionResolutionStatus.RESOLVED:
            raise ValueError(
                "a mention may name an entity only when its resolution is "
                f"{MentionResolutionStatus.RESOLVED.value}, not "
                f"{self.resolution_status.value}")
        self.context_before = (self.context_before or "")[-MAX_CONTEXT_CHARS:]
        self.context_after = (self.context_after or "")[:MAX_CONTEXT_CHARS]
        return self


__all__ = [
    "ChemicalEntity",
    "ChemicalMention",
    "ChemicalRepresentation",
    "EntityNotFound",
    "EntityRegistrationError",
    "ENTITY_ID_PREFIX",
    "ENTITY_ID_WIDTH",
    "ExternalIdentifier",
    "IDENTITY_KEY_VERSION",
    "MAX_CONTEXT_CHARS",
    "MentionResolutionStatus",
    "REPRESENTATION_KINDS",
    "format_entity_id",
    "identity_key",
    "identity_key_of",
]

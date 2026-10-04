"""What a mention resolution concluded -- and why it concluded it.

Phase 3.8.2. `ChemicalMention` says what a piece of text said; this module
records the answer to the question the knowledge graph will keep asking:

> Why does CREA think *this* mention is *that* ChemicalEntity?

The answer is never "a model said so". It is a chain of deterministic facts:
an explicit structure the user attached and RDKit parsed, a name a trusted
provider matched to one structure, an identity key shared with an entity
already registered, a contextual definition in the same paper, a document
passage the mention sits in. `MentionResolutionEvidence` keeps that chain,
and `MentionResolutionBasis` names its strongest link.

Where the answer is *not* established, the record says so instead of
picking: `AMBIGUOUS` keeps its candidates, `NOT_FOUND` keeps the
consultation honest, `UNRESOLVED` keeps waiting for context. A numbered
reference is paper-local -- "compound 7" in one paper may be another
compound in the next -- so candidates and context quotes travel with the
mention's own source scope.

`core` again: no provider, no network, no RDKit, no model. By the time a
record here exists, the identity layer has already parsed and the registry
has already remembered.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator

from crea.core.models.entity import ChemicalMention, ExternalIdentifier, MentionResolutionStatus
from crea.core.models.evidence import Locator
from crea.core.models.identity import ChemicalIdentityCandidate
from crea.core.provenance import Provenance

#: Bumped when the meaning of a resolution record changes. The basis names
#: what established a link; this names the vocabulary doing the naming.
MENTION_RESOLUTION_VERSION = "crea-mention-resolution-v1"


class MentionResolutionBasis(str, Enum):
    """The strongest link in the chain that connected mention to entity.

    Ordered by how much they establish -- explicit structure outranks a name
    match (§23) -- but never used to *choose* between candidates. The basis
    records what happened; it is not a ranking of ambiguous outcomes.
    """

    #: A structure the user attached to the mention; parsed, not guessed.
    EXPLICIT_STRUCTURE = "explicit_structure"
    #: The mention text matched a name already carried by a registered entity.
    REGISTRY_NAME_MATCH = "registry_name_match"
    #: The mention named an external id (a PubChem CID) already on an entity.
    REGISTRY_EXTERNAL_ID = "registry_external_id"
    #: A trusted provider matched the name to exactly one structure.
    PROVIDER_NAME_MATCH = "provider_name_match"
    #: A trusted provider's own record id (a CID) resolved to one structure.
    PROVIDER_EXTERNAL_ID = "provider_external_id"
    #: The same paper's context defined the reference ("Compound 7
    #: (phenylacetylene)"), and that definition resolved.
    CONTEXTUAL_REFERENCE = "contextual_reference"
    #: Nothing was established. The mention stays what it was.
    UNRESOLVED = "unresolved"


class MentionCandidateInfo(BaseModel):
    """One candidate an ambiguous mention is still choosing among.

    Kept as information, never as a choice: the resolution carries every
    candidate and selects none. `entity_id` is filled only when that
    candidate's identity key is *already* registered -- a lookup, never a
    side-effect registration -- because minting entities for candidates
    would make ambiguity look like three answers.
    """

    name: str | None = None
    canonical_smiles: str = ""
    inchi: str = ""
    inchikey: str = ""
    external_identifiers: list[ExternalIdentifier] = Field(default_factory=list)
    entity_id: str | None = None
    source: str = ""

    @classmethod
    def from_identity_candidate(cls, candidate: ChemicalIdentityCandidate,
                                entity_id: str | None = None):
        external: list[ExternalIdentifier] = []
        if candidate.source.upper() == "PUBCHEM" and candidate.source_id:
            external.append(ExternalIdentifier(
                namespace="pubchem", identifier=candidate.source_id,
                source=candidate.source))
        return cls(
            name=candidate.candidate_name or candidate.input_name,
            canonical_smiles=candidate.canonical_smiles,
            inchi=candidate.inchi,
            inchikey=candidate.inchikey,
            external_identifiers=external,
            entity_id=entity_id,
            source=candidate.source,
        )


class ContextReference(BaseModel):
    """What the surrounding context says a reference means.

    `name` is a name the context gives ("phenylacetylene"); `smiles`/`inchi`
    are a structure the context gives outright. `context_quote` is the text
    that carried the definition -- kept verbatim so a reader can check that
    the context really said this. A `Locator` may point at the page it came
    from; the quote is a pointer, not evidence in itself.
    """

    name: str = ""
    smiles: str = ""
    inchi: str = ""
    context_quote: str = ""
    source: str = ""
    source_id: str = ""
    locator: Locator | None = None


class MentionResolutionEvidence(BaseModel):
    """The chain behind one resolution. See the module docstring.

    Deliberately shaped like `Evidence` provenance -- source, source_id,
    locator, evidence_refs -- because the question it answers ("where did
    this come from?") is the same question, and two vocabularies for one
    question is one too many.
    """

    basis: MentionResolutionBasis = MentionResolutionBasis.UNRESOLVED
    #: The identity key the link was established under, when established.
    identity_key: str = ""
    #: Where the establishing fact came from: "entity_registry", "pubchem",
    #: "context", "user_provided", ...
    source: str = ""
    source_id: str = ""
    #: The context text that defined the reference, verbatim.
    context_quote: str = ""
    locator: Locator | None = None
    #: Document evidence the mention sits in. Only ids of evidence actually
    #: held are kept here; anything else lands in `unresolved_evidence_refs`.
    evidence_refs: list[str] = Field(default_factory=list)
    unresolved_evidence_refs: list[str] = Field(default_factory=list)
    provenance: Provenance | None = None
    #: The human-readable chain, e.g. "explicit SMILES + RDKit validation +
    #: identity key". For audit; never the only record of the basis.
    notes: list[str] = Field(default_factory=list)


class ChemicalMentionResolution(BaseModel):
    """One mention, resolved or refused, with its reasons.

    `mention` is the mention as it now stands (entity_id and status filled
    in when something was established). `entity_id` mirrors it for readers
    that only need the outcome; `candidates` carries what an ambiguous
    mention is still choosing among, unchosen.
    """

    mention: ChemicalMention
    status: MentionResolutionStatus
    entity_id: str | None = None
    basis: MentionResolutionBasis = MentionResolutionBasis.UNRESOLVED
    evidence: MentionResolutionEvidence = Field(
        default_factory=MentionResolutionEvidence)
    candidates: list[MentionCandidateInfo] = Field(default_factory=list)

    @model_validator(mode="after")
    def _entity_requires_resolution(self) -> "ChemicalMentionResolution":
        if self.entity_id and self.status is not MentionResolutionStatus.RESOLVED:
            raise ValueError(
                "a resolution may name an entity only when it is "
                f"{MentionResolutionStatus.RESOLVED.value}, not {self.status.value}")
        return self


__all__ = [
    "ChemicalMentionResolution",
    "ContextReference",
    "MENTION_RESOLUTION_VERSION",
    "MentionCandidateInfo",
    "MentionResolutionBasis",
    "MentionResolutionEvidence",
]

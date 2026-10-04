"""Evidence strength policy.

Strength answers "how close is this to the thing itself", not "how much do we
like it". Three rules follow from that and all three have bitten someone:

1. **An abstract is never primary document evidence.** An abstract saying
   "compound X showed activity against Y" is the authors' summary of a result.
   It is not a method, and a Claim that rests on it must say so.
2. **A reconstruction is never promoted.** An OpenAlex abstract is reassembled
   from a positional index. `AbstractRecord.evidence_strength` already keeps
   that at `DISCOVERY_EVIDENCE`; this module reuses that answer rather than
   inventing a second one.
3. **A database record is secondary, whoever the database is.** Crossref is
   where a publisher deposited its metadata, and that is still metadata. The
   fact that a record has a DOI does not make it a document.
"""

from __future__ import annotations

from crea.core.models.evidence import EvidenceType
from crea.core.models.literature import AbstractRecord, EvidenceStrength

#: Which fields a Paper is routinely asked to vouch for. A minimal set on
#: purpose: every field promoted to Evidence is a field a Claim may lean on, and
#: a citation surface twice the size of the evidence is a liability.
CITED_METADATA_FIELDS: tuple[str, ...] = (
    "title", "doi", "publication_year", "journal",
)

_TYPE_STRENGTH: dict[EvidenceType, EvidenceStrength] = {
    EvidenceType.METADATA: EvidenceStrength.SECONDARY_METADATA_EVIDENCE,
    EvidenceType.STRUCTURED_DATABASE: EvidenceStrength.SECONDARY_METADATA_EVIDENCE,
    EvidenceType.FULLTEXT: EvidenceStrength.PRIMARY_DOCUMENT_EVIDENCE,
    EvidenceType.EXPERIMENTAL: EvidenceStrength.PRIMARY_DOCUMENT_EVIDENCE,
    EvidenceType.USER_PROVIDED_DOCUMENT: EvidenceStrength.USER_PROVIDED_EVIDENCE,
}

#: Types with no producer yet. They are carried so that a document producer, when
#: one exists, fills them in rather than redesigning the model.
UNPRODUCED_TYPES: frozenset[EvidenceType] = frozenset({
    EvidenceType.FULLTEXT, EvidenceType.EXPERIMENTAL,
    EvidenceType.USER_PROVIDED_DOCUMENT, EvidenceType.STRUCTURED_DATABASE,
})


def strength_for(evidence_type: EvidenceType,
                  abstract: AbstractRecord | None = None) -> EvidenceStrength:
    """The strength a piece of evidence may carry.

    An abstract's strength is decided by where the abstract text came from,
    which `AbstractRecord` already knows. Everything else is decided by type.
    """
    if evidence_type is EvidenceType.ABSTRACT:
        if abstract is None:
            # No abstract given: fall back to the weakest reading. Assuming a
            # publisher deposit because nobody said otherwise is the failure
            # mode this whole policy exists to prevent.
            return EvidenceStrength.DISCOVERY_EVIDENCE
        return abstract.evidence_strength
    return _TYPE_STRENGTH.get(evidence_type,
                              EvidenceStrength.SECONDARY_METADATA_EVIDENCE)


def may_support_a_chemical_fact(evidence_type: EvidenceType,
                                 strength: EvidenceStrength) -> bool:
    """May a Claim about an experimental fact rest on this alone?

    Not a chemistry judgement -- a provenance one. "The synthesis used reagent
    A at 80 °C" needs the document, not a summary of it.
    """
    return strength is EvidenceStrength.PRIMARY_DOCUMENT_EVIDENCE

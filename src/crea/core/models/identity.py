"""Chemical identity. What the user *gave*, and what we were able to *prove*.

The whole point of this module is that those two are different objects.
`StructureInput` is the string or image the user handed over; it makes no claim
about what it means. `ChemicalIdentityResolution` is the outcome of an inquiry
into what it means, and its `status` says how far that inquiry got.

The rule the model encodes and that the rest of the phase must not forget:

> `CANDIDATE` is not `RESOLVED`.

A candidate is something a provider, a recogniser or a parser *proposed*. Only a
deterministic parse of a structure the user supplied, or an unambiguous match in
a trusted chemical database, earns `RESOLVED`. A model that let `candidate` be
read as `resolved` would let a hallucinated SMILES walk straight into
`retrosynthesize`, which is the failure this layer exists to prevent.

This is the `core` layer. It must not import RDKit, must not import a provider,
and must not contain any `name -> SMILES` or `image -> SMILES` logic. It
describes; it does not decide. Parsing lives in `crea.chem`, lookup lives in
`crea.providers`, and the decision lives in `crea.services.identity`.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class StructureInputKind(str, Enum):
    """What sort of thing the user handed us. Read from the input, never guessed.

    The kind decides which resolution path is even legal. A SMILES goes to the
    deterministic parser; a name does not, because there is no parser that turns
    a word into a structure without asking a database or inventing one.
    """

    SMILES = "smiles"
    INCHI = "inchi"
    INCHIKEY = "inchikey"
    PUBCHEM_CID = "pubchem_cid"
    NAME = "name"
    IMAGE = "image"


class StructureResolutionStatus(str, Enum):
    """How far the inquiry got. Not a ranking -- a description of a situation.

    `CANDIDATE` sits between `INPUT` and `RESOLVED` deliberately. Something was
    proposed; nothing was established. Reading `candidates[0]` because the list
    is non-empty is the bug this enum is here to make visible.
    """

    INPUT = "input"            # received, not yet examined
    CANDIDATE = "candidate"    # something was proposed; nothing established
    RESOLVED = "resolved"      # one structure, established, no remaining doubt
    AMBIGUOUS = "ambiguous"    # more than one structure survived the rules
    INVALID = "invalid"        # the input could not be a structure at all
    NOT_FOUND = "not_found"    # looked, and the source has no such entry
    UNAVAILABLE = "unavailable"  # could not look; the source was unreachable


class IdentityBasis(str, Enum):
    """In what sense two structures are "the same". Often asked, rarely one answer.

    Two molecules can share a skeleton and differ in stereochemistry, or share
    a formula and differ in where the protons are. Calling any of those "the
    same molecule" is a claim this project will not make without saying which
    sense it meant, so the sense is a field rather than a mood.

    These are the *strongest statement the comparison justifies*, derived from
    the four facets below rather than asserted alongside them. Where the facets
    disagree, the weaker statement wins: same connectivity and different
    stereo is `SAME_CONNECTIVITY`, not `SAME_STEREO` with an asterisk.

    `UNKNOWN` is the honest answer whenever the comparison was not actually
    performed. It is not a synonym for `EXACT_STRUCTURE` with less enthusiasm.
    """

    EXACT_STRUCTURE = "exact_structure"    # identical graph, stereo, isotopes

    # `SAME_STEREO` is deliberately narrow, and the facets are the real answer.
    # "Same stereochemistry" is true of two compounds that share a skeleton and
    # differ only in where the protons are, and it is the strongest of the five
    # labels that is still true of them. Naming the difference is
    # `IdentityFacets.same_tautomer`'s job, not the basis's.
    SAME_STEREO = "same_stereo"            # stereo agrees; isotope/tautomer does not

    SAME_CONNECTIVITY = "same_connectivity"  # graph agrees; stereo does not
    PROVIDER_IDENTITY = "provider_identity"  # a database says so; we did not compare
    UNKNOWN = "unknown"                    # nothing was compared


class IdentityFacets(BaseModel):
    """The four questions that make "the same molecule" mean four different things.

    Each is `bool | None` on purpose. `False` means the comparison ran and the
    answer was no; `None` means it was never asked, which is a different
    statement and one that must survive into the report. Collapsing `None` into
    `False` would report "these differ in tautomerism" for a pair nobody
    checked tautomerism on.

    The four are kept apart because they are genuinely independent. A pair can
    be identical in connectivity and different in stereochemistry -- and that
    pair is not "the same molecule" in any sense a chemist would accept, which
    is the exact case this phase must never round off to yes.
    """

    same_connectivity: bool | None = None
    same_stereo: bool | None = None
    same_isotope: bool | None = None
    #: Same protomer/tautomer class: the protons are in the same places, and
    #: the charge with them. Acetic acid and acetate differ here and nowhere
    #: else, which is why this facet cannot be folded into `same_connectivity`
    #: -- their heavy-atom graphs really are identical.
    same_tautomer: bool | None = None

    def basis(self) -> "IdentityBasis":
        """The strongest claim these facets support. Derived, never asserted."""
        if self.same_connectivity is False:
            return IdentityBasis.UNKNOWN
        if self.same_connectivity is not True:
            return IdentityBasis.UNKNOWN
        if self.same_stereo is False:
            return IdentityBasis.SAME_CONNECTIVITY
        if self.same_stereo is not True:
            return IdentityBasis.SAME_CONNECTIVITY
        # connectivity and stereo both agree; anything else still open keeps
        # this short of EXACT_STRUCTURE
        if self.same_isotope is False or self.same_tautomer is False:
            return IdentityBasis.SAME_STEREO
        if self.same_isotope is not True or self.same_tautomer is not True:
            return IdentityBasis.SAME_STEREO
        return IdentityBasis.EXACT_STRUCTURE


class IdentityEvidenceLevel(str, Enum):
    """How strong the evidence behind an identity claim is (CREA 4.0 §21/§4).

    A second dimension from `StructureResolutionStatus`: that says *how far
    resolution got*; this says *what the claim rests on*. Both are always
    reported together, and neither is a confidence score -- there is no
    number here and never will be.

    EXACT
        The input itself carries a structure notation (SMILES, InChI,
        molblock...) and a deterministic parser established the structure.
        Nothing external was consulted and nothing was inferred.
    SUPPORTED
        A reliable external source (a chemical database record, a
        deterministic nomenclature parser) provided an explicit structure
        and the chain is unbroken from name to that structure.
    PROBABLE
        The chain points one way but identity is not fully pinned: the
        match came through a synonym rather than a primary name, or a
        facet (stereochemistry, isotope, ...) is undetermined.
    AMBIGUOUS
        Several distinct structures survive and no rule this layer will
        apply separates them. The choice is a person's, never
        `candidates[0]`.
    UNRESOLVED
        Not enough information to establish an identity at all.

    The rule the name exists to enforce: a name resolution is never
    labelled to look like a structure resolution. `Phenylacetylene` earning
    SUPPORTED is honest; claiming it earned EXACT would make every future
    reader trust a chain nobody verified.
    """

    EXACT = "exact"
    SUPPORTED = "supported"
    PROBABLE = "probable"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class RecognitionStage(str, Enum):
    """How far an image reading got. Not a resolution status -- see the docstring.

    OCSR is *recognition*, not verification. A recogniser emits a string it
    believes is a structure; whether that string is a structure is a question
    for RDKit, and whether it is *the user's* structure is a question nobody
    here can answer. So the image path records its stages explicitly and the
    resolution status never reaches `RESOLVED` from an image alone.

    `IMAGE_RECOGNIZED_CANDIDATE` is the first layer by requirement: the raw
    output of the recogniser, before anything has been validated.
    `STRUCTURE_CANDIDATE` means that output parsed and sanitised, which is a
    fact about the string and still not a fact about the chemistry.
    """

    IMAGE_RECOGNIZED_CANDIDATE = "image_recognized_candidate"
    STRUCTURE_CANDIDATE = "structure_candidate"
    UNPARSED = "unparsed"


class MatchedBy(str, Enum):
    """How a candidate was found. Recorded because it is not all equal.

    A synonym that happens to be a registered trade name is a weaker link than
    a CAS number that is the entry's own identifier. `matched_by` says which of
    those happened; it is a fact about the lookup, not a grade.
    """

    PRIMARY_NAME = "primary_name"
    SYNONYM = "synonym"
    INCHI = "inchi"
    INCHIKEY = "inchikey"
    SMILES = "smiles"
    CID = "cid"
    IMAGE_RECOGNITION = "image_recognition"
    UNKNOWN = "unknown"


class ImageProvenance(BaseModel):
    """Everything we know about a chemical structure image and its reading.

    An image is the least trustworthy input this phase accepts, because the
    recogniser is a model and a model's output is a guess that happens to be
    well-formed. Every field here exists so that a reader can audit the guess:
    which file, read by which recogniser at which version, producing which raw
    string, normalised how.

    The image bytes themselves are not copied into the database. A hash is
    enough to say "this image", and the user's own file reference is enough to
    find it again.
    """

    image_sha256: str = ""
    source: str = ""              # where the image came from, or "user_provided"
    retrieved_at: str | None = None
    recognizer: str = ""          # e.g. "decimer"
    recognizer_version: str = ""
    model_version: str = ""
    raw_smiles: str = ""          # exactly what the recogniser emitted
    normalized_smiles: str = ""   # what the deterministic parser made of it
    file_reference: str | None = None  # the user's path or id; not a copy


class StructureInput(BaseModel):
    """What the user gave us, recorded before we interpret any of it.

    `kind` and `value` are separated because the same string means different
    things in different roles and because the resolution path branches on the
    kind. Recording the input as given -- rather than only the structure we
    eventually concluded -- is what makes a later disagreement auditable.
    """

    kind: StructureInputKind
    value: str = ""
    image: ImageProvenance | None = None
    given_by_user: bool = True
    note: str = ""

    @field_validator("kind", mode="before")
    @classmethod
    def _accept_kind_by_name_or_value(cls, raw: Any) -> Any:
        """Take `NAME` as happily as `name`.

        The enum's *values* are lowercase, matching the rest of the codebase.
        The spec and the tool schema write the *member names*, uppercase. A
        caller should not have to know which of those two the constructor
        wanted, so both are accepted and neither is privileged.
        """
        if isinstance(raw, str):
            for member in StructureInputKind:
                if raw == member.value or raw.upper() == member.name:
                    return member.value
        return raw

    @classmethod
    def of(cls, kind: str, value: str, **kwargs: Any) -> "StructureInput":
        """Build from a raw kind string, so a tool argument maps cleanly."""
        return cls(kind=kind, value=value, **kwargs)


class ChemicalIdentityCandidate(BaseModel):
    """One structure somebody proposed for the user's input. A proposal, not a finding.

    Every candidate carries where it came from, because a candidate with no
    provenance is a candidate nobody can check and therefore a candidate nobody
    should believe. `source` and `source_id` name the database entry;
    `matched_by` and `matched_synonym` name *how* the entry was reached.

    This model has no field that says "this is the one". That is
    `ChemicalIdentityResolution.selected`'s job, and it is only populated when
    the status is `RESOLVED`.
    """

    candidate_id: str = ""
    source: str = ""              # "PUBCHEM", "RDKit", "DECIMER", "user_input"
    source_id: str = ""           # e.g. the PubChem CID
    source_url: str | None = None
    retrieved_at: str | None = None

    input_name: str | None = None      # what the user called it, if they used a name
    candidate_name: str | None = None  # the source's own name for the entry
    synonyms: list[str] = Field(default_factory=list)
    matched_by: MatchedBy = MatchedBy.UNKNOWN
    matched_synonym: str | None = None

    canonical_smiles: str = ""
    isomeric_smiles: str = ""
    inchi: str = ""
    inchikey: str = ""
    molecular_formula: str | None = None
    molecular_weight: float | None = None

    identity_basis: IdentityBasis = IdentityBasis.UNKNOWN
    facets: IdentityFacets = Field(default_factory=IdentityFacets)
    #: Set for candidates that came out of an image recogniser, which are the
    #: only ones with a stage rather than a basis. Empty for everything else.
    recognition_stage: RecognitionStage | None = None
    notes: list[str] = Field(default_factory=list)
    #: Free-form facts kept verbatim from the source, for audit only. Never read
    #: as a structure and never rendered as a conclusion.
    raw: dict[str, Any] = Field(default_factory=dict)

    def describes_structure(self) -> bool:
        """Whether this candidate carries a structure at all.

        A lookup can legitimately return a record with a name and no structure
        -- a formula hit, or a source that lists synonyms only. Such a record is
        not a candidate for the molecule; it is a candidate for the *name*.
        """
        return bool(self.canonical_smiles or self.inchi or self.inchikey)


class ChemicalIdentityResolution(BaseModel):
    """The outcome of asking "what did the user mean?".

    The status is the answer. Everything else is what the answer is built out
    of. `candidates` is never truncated to one entry to make a decision look
    tidier; an `AMBIGUOUS` resolution with three candidates is a *complete*
    answer, not a partial one waiting to be finished by picking the first.
    """

    status: StructureResolutionStatus = StructureResolutionStatus.INPUT
    input: StructureInput
    candidates: list[ChemicalIdentityCandidate] = Field(default_factory=list)

    #: Set only when `status is RESOLVED`. The one structure that was
    #: established, chosen by a rule rather than by position in a list.
    selected: ChemicalIdentityCandidate | None = None
    selected_basis: IdentityBasis = IdentityBasis.UNKNOWN

    #: Why the status is what it is, in words a reader can act on. "Found 3
    #: candidates that differ in protonation" is useful; "failed" is not.
    reason: str = ""
    #: Machine-readable reason codes, so tests and callers need not parse prose.
    reason_codes: list[str] = Field(default_factory=list)
    #: Everything that had to be true for `RESOLVED`, recorded so a later audit
    #: can see which rule produced the answer.
    rules_applied: list[str] = Field(default_factory=list)

    @property
    def is_resolved(self) -> bool:
        return self.status is StructureResolutionStatus.RESOLVED

    @property
    def is_ambiguous(self) -> bool:
        return self.status is StructureResolutionStatus.AMBIGUOUS

    def structures_distinguished(self) -> bool:
        """Whether the surviving candidates are actually different structures.

        Several candidate *records* can point at one structure -- PubChem has
        one CID and many synonyms, and a name lookup can surface both a primary
        name hit and a synonym hit for the same entry. Those are not an
        ambiguity. An ambiguity is two candidates whose structures differ, and
        that is the only case where asking the user is warranted.
        """
        seen: set[tuple[str, str]] = set()
        for candidate in self.candidates:
            key = (candidate.canonical_smiles, candidate.inchikey)
            if key != ("", ""):
                seen.add(key)
        return len(seen) > 1

    def require_resolved(self) -> ChemicalIdentityCandidate:
        """The selected structure, or a refusal to pretend there is one.

        Callers that need a structure before running chemistry go through this
        rather than reading `candidates[0]`. Reaching it with an unresolved
        status is a programming error in the caller, and it says so.
        """
        if self.status is StructureResolutionStatus.RESOLVED and self.selected is not None:
            return self.selected
        raise IdentityNotResolved(
            f"identity is {self.status.value}, not resolved; "
            f"{self.reason or 'no structure has been established'}",
            status=self.status,
        )


class IdentityNotResolved(Exception):
    """A structure was demanded before one had been established.

    Deliberately not a `CreaError` subclass in spirit -- this is a caller bug,
    not a chemistry failure -- but it carries the status so the caller can say
    something true about *which* unresolved state it hit.
    """

    def __init__(self, message: str, status: StructureResolutionStatus) -> None:
        self.status = status
        super().__init__(message)

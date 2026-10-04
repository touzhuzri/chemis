"""Reaction-level literature linking, and the ladder a step has to climb.

This module exists because `Paper` cannot hold a reaction, and putting one
there would be wrong. A `Paper` is a bibliographic record: title, abstract,
identifiers, access. It has no reactants, no products, no reaction SMILES, and
the temptation to bolt them on is exactly the mistake this phase is about --
"Synthesis of X" is not a reaction, and a paper whose title contains the name
of our product has not thereby reported the route we are asking about.

So structures live here, beside the record they belong to, in three roles:

    LiteratureReactionCandidate  a reaction that a piece of literature may
                                  contain, with its structures or with an
                                  explicit statement that it has none
    ReactionReference             a stable, citable handle on that candidate,
                                  independent of whether structures were found
    ReactionEvidenceLink          the connection from a reference to the
                                  Evidence it rests on

The separation between a candidate and a reference is deliberate. A candidate
gains structures when somebody reads the document; its reference does not
change, because the work did not. A citation that resolved to a different id
depending on how much of the paper we had read would be useless.

Two things are deliberately absent and must stay absent:

* **atom mapping and reaction centres.** We cannot place them, so we do not.
* **any number between the rungs.** `best_similarity` is a measured Tanimoto
  coefficient from RDKit and is named for what it is. There is no confidence,
  no probability, no success rate, because no statistical source for one exists
  in this project and a field with no definition acquires a number in it.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator

from crea.core.ids import content_hash, normalize_doi
from crea.core.models.route import ReactionMatchType, StepStatus

#: Bumped when the shape of a reference changes meaning.
REACTION_REFERENCE_VERSION = "reaction-reference-v1"

#: Bumped when the rule for deriving a candidate id changes.
REACTION_CANDIDATE_ID_VERSION = "reaction-candidate-v1"


class ReactionStructureAvailability(str, Enum):
    """Whether a literature record carries reaction structures at all.

    `NO_REACTION_STRUCTURE_AVAILABLE` is the common case and is a real answer,
    not a gap. The providers return metadata and abstracts; neither contains a
    reactant. Recording the absence explicitly is what lets a reader tell "we
    looked and there was no structure" apart from "we never looked", and it is
    what keeps a title match at `DISCOVERY_ONLY` forever.
    """

    AVAILABLE = "available"
    NO_REACTION_STRUCTURE_AVAILABLE = "no_reaction_structure_available"


class ReactionEvidenceLevel(str, Enum):
    """The reaction-level evidence ladder.

    Ordered by what had to be true for a step to reach it, not by how good the
    chemistry is. Each rung is a distinct claim about the world:

        RETROSYNTHESIS_CANDIDATE
            a search tool proposed this step. Nothing has been consulted.

        LITERATURE_DISCOVERY
            a relevant work exists and we can name it. This is the ceiling for
            anything read from a title, an abstract or a DOI -- the work was
            found, and finding it says nothing about whether it ran this step.

        LITERATURE_REACTION_SUPPORT
            a literature record's reaction structures were compared with this
            step's, both sides matched exactly, and the comparison rests on
            citable text.

        EXPERIMENTAL_REACTION_SUPPORT
            a `ReactionRecord` with real conditions was read out of a
            literature document, and its structures match this step's.

        USER_DOCUMENT_EXPERIMENTAL_SUPPORT
            the same, from a file the operator handed us.

    The last two are **peers, not a pair to merge**. One is a published
    procedure; the other is a file whose provenance this project cannot
    establish -- not whether it is the publisher's PDF, a scan, or somebody's
    notes. A user document does not become general literature support by being
    convenient, and the rung it reaches says so.
    """

    RETROSYNTHESIS_CANDIDATE = "retrosynthesis_candidate"
    LITERATURE_DISCOVERY = "literature_discovery"
    LITERATURE_REACTION_SUPPORT = "literature_reaction_support"
    EXPERIMENTAL_REACTION_SUPPORT = "experimental_reaction_support"
    USER_DOCUMENT_EXPERIMENTAL_SUPPORT = "user_document_experimental_support"


#: Position on the ladder. Support only ever moves up, and a comparison of
#: positions is the only way this module is allowed to decide anything -- which
#: is what makes "never auto-upgrade" a checkable property rather than a
#: promise in a docstring.
EVIDENCE_LEVEL_ORDER: dict[ReactionEvidenceLevel, int] = {
    ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE: 0,
    ReactionEvidenceLevel.LITERATURE_DISCOVERY: 1,
    ReactionEvidenceLevel.LITERATURE_REACTION_SUPPORT: 2,
    ReactionEvidenceLevel.EXPERIMENTAL_REACTION_SUPPORT: 3,
    ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT: 3,
}

#: The two experimental rungs tie deliberately: neither outranks the other,
#: because their difference is provenance rather than strength. A tie would be
#: dishonest for a single number, so this is a set and membership is the test.
_EXPERIMENTAL_RANKS = (
    ReactionEvidenceLevel.EXPERIMENTAL_REACTION_SUPPORT,
    ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT,
)


def at_least(level: ReactionEvidenceLevel, floor: ReactionEvidenceLevel) -> bool:
    """Has `level` reached `floor`, treating the two experimental rungs as equal?"""
    if floor in _EXPERIMENTAL_RANKS:
        return level in _EXPERIMENTAL_RANKS
    return EVIDENCE_LEVEL_ORDER[level] >= EVIDENCE_LEVEL_ORDER[floor]


class LinkBasis(str, Enum):
    """On what a reaction record was attached to a step.

    `UNVERIFIED_STRUCTURE` is the answer a caller gives when it has a
    `ReactionRecord` and no structures to compare against the step. That record
    is real experimental text about *something*; it is not shown to be this
    step. The distinction is kept rather than collapsed, because the only way
    to collapse it is to promote the step on the strength of a coincidence.
    """

    EXACT_STRUCTURE = "exact_structure"
    PARTIAL_STRUCTURE = "partial_structure"
    UNVERIFIED_STRUCTURE = "unverified_structure"


# ---------------------------------------------------------------------------
# literature reaction candidates
# ---------------------------------------------------------------------------


class LiteratureReactionCandidate(BaseModel):
    """A reaction a piece of literature may contain.

    Structures are optional in practice and mandatory in honesty: if there are
    none, `structure_availability` says so and the candidate can only ever be
    `DISCOVERY_ONLY`. The validator below enforces that, so a candidate cannot
    claim structures it does not carry.
    """

    candidate_id: str
    source: str                    # "openalex" | "crossref" | "user_provided" | ...
    source_id: str
    paper_id: str | None = None
    doi: str | None = None
    title: str = ""
    url: str | None = None
    retrieved_at: str = ""

    reactants: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    reaction_smiles: str | None = None

    structure_availability: ReactionStructureAvailability = (
        ReactionStructureAvailability.NO_REACTION_STRUCTURE_AVAILABLE)
    #: What the structures are and where they came from. `"UNVERIFIED"` is the
    #: default and is meant to stay common: a structure asserted by a caller
    #: who read it out of a document is verified only in the sense that
    #: somebody looked.
    structure_origin: str = "UNVERIFIED"
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _availability_follows_the_structures(self) -> "LiteratureReactionCandidate":
        """The availability field is a fact about the structures, not a claim.

        Set here rather than by the caller so that a candidate cannot say
        "structures available" over an empty list -- which is the shape a bug
        would take, and the shape that would silently turn a title match into
        an exact structure match.
        """
        has = bool(self.reactants or self.products or self.reaction_smiles)
        self.structure_availability = (
            ReactionStructureAvailability.AVAILABLE if has
            else ReactionStructureAvailability.NO_REACTION_STRUCTURE_AVAILABLE)
        if not has and not self.warnings:
            self.warnings.append(
                "this record carries no reaction structures, so it cannot be "
                "compared to a step; it is a discovery and nothing more")
        return self

    @classmethod
    def build(cls, *, source: str, source_id: str, paper_id: str | None = None,
              doi: str | None = None, title: str = "", url: str | None = None,
              retrieved_at: str = "",
              reactants: list[str] | None = None,
              products: list[str] | None = None,
              reaction_smiles: str | None = None,
              structure_origin: str = "UNVERIFIED") -> "LiteratureReactionCandidate":
        reactants = [s for s in (reactants or []) if s and s.strip()]
        products = [s for s in (products or []) if s and s.strip()]
        normalized_doi = normalize_doi(doi) if doi else None
        identity = content_hash("|".join([
            REACTION_CANDIDATE_ID_VERSION, source, source_id,
            ",".join(sorted(reactants)), ",".join(sorted(products)),
            reaction_smiles or "",
        ]), 16)
        return cls(
            candidate_id=identity,
            source=source,
            source_id=source_id,
            paper_id=paper_id,
            doi=normalized_doi,
            title=title or "",
            url=url,
            retrieved_at=retrieved_at,
            reactants=reactants,
            products=products,
            reaction_smiles=reaction_smiles,
            structure_origin=structure_origin,
        )

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.source}:{self.source_id}"


class ReactionReference(BaseModel):
    """A stable citation for a literature reaction candidate.

    The id is derived from the work, not from its structures, so re-reading the
    same paper with more of it parsed yields the same reference. A citation
    that changed when the extraction improved would break every report that
    had already used it.
    """

    reference_id: str
    candidate_id: str
    source: str
    source_id: str
    paper_id: str | None = None
    doi: str | None = None
    url: str | None = None
    retrieved_at: str = ""

    @classmethod
    def for_candidate(cls, candidate: LiteratureReactionCandidate) -> "ReactionReference":
        identity = content_hash("|".join([
            REACTION_REFERENCE_VERSION, candidate.source, candidate.source_id,
            candidate.doi or "", candidate.paper_id or "",
        ]), 16)
        return cls(
            reference_id=identity,
            candidate_id=candidate.candidate_id,
            source=candidate.source,
            source_id=candidate.source_id,
            paper_id=candidate.paper_id,
            doi=candidate.doi,
            url=candidate.url,
            retrieved_at=candidate.retrieved_at,
        )


class ReactionEvidenceLink(BaseModel):
    """A reference to the Evidence underneath it.

    Resolved against the `EvidenceRegistry` before it is believed: a link whose
    evidence id does not resolve is reported unresolved, not quietly dropped.
    Dropping it would make the report shorter and less alarming while leaving
    the gap exactly where it was.
    """

    link_id: str
    reference_id: str
    evidence_id: str
    evidence_type: str
    strength: str
    source: str
    resolved: bool = False
    note: str = ""

    @classmethod
    def build(cls, *, reference: ReactionReference, evidence) -> "ReactionEvidenceLink":
        identity = content_hash(
            f"reaction-link-v1|{reference.reference_id}|{evidence.evidence_id}", 16)
        return cls(
            link_id=identity,
            reference_id=reference.reference_id,
            evidence_id=evidence.evidence_id,
            evidence_type=str(evidence.evidence_type),
            strength=str(evidence.strength),
            source=evidence.source,
        )


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------


class ReactionMatchResult(BaseModel):
    """How one literature candidate relates to one route step.

    `evidence_level` is what the match *earned*, which is at most
    `LITERATURE_REACTION_SUPPORT`: matching structures is still a structural
    statement, and no amount of exact matching turns a paper's reaction table
    into an experimental record. Only a `ReactionRecord` read out of a
    document does that.

    There is no confidence field and there will not be one. `best_similarity`
    is RDKit's Tanimoto coefficient for the nearest unmatched pair, it is
    named for what it is, and it is not a probability of anything.
    """

    step_id: str = ""
    reference_id: str = ""
    candidate_id: str = ""
    match_type: ReactionMatchType = ReactionMatchType.DISCOVERY_ONLY
    evidence_level: ReactionEvidenceLevel = (
        ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE)

    #: What the comparison was actually made on, in words a reader can check:
    #: "both sides matched exactly", "the product matched and the reactants
    #: did not", "no reaction structures were available to compare".
    match_basis: str = ""

    source: str = ""
    source_id: str = ""
    paper_id: str | None = None
    doi: str | None = None
    title: str = ""

    evidence_refs: list[str] = Field(default_factory=list)
    matched_reactants: list[str] = Field(default_factory=list)
    matched_products: list[str] = Field(default_factory=list)
    unmatched_reactants: list[str] = Field(default_factory=list)
    unmatched_products: list[str] = Field(default_factory=list)

    #: RDKit's Tanimoto ECFP4 for the nearest unmatched pair. A measurement of
    #: structural resemblance and nothing else.
    best_similarity: float | None = None
    notes: list[str] = Field(default_factory=list)


class ReactionRecordLink(BaseModel):
    """A `ReactionRecord` attached to a route step.

    `basis` is the whole argument. `EXACT_STRUCTURE` means both sides were
    compared and matched. `UNVERIFIED_STRUCTURE` means a caller said so and we
    could not check it -- and in that case the step is **not** promoted, no
    matter how good the record is. An experimental procedure for some other
    reaction is not experimental support for this one.
    """

    step_id: str
    reaction_id: str
    basis: LinkBasis = LinkBasis.UNVERIFIED_STRUCTURE
    match_type: ReactionMatchType = ReactionMatchType.NO_MATCH
    reactant_smiles: list[str] = Field(default_factory=list)
    product_smiles: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    #: Who attached it. "deterministic" is a claim we can back with the
    #: comparison above; anything else is a caller's word, and is recorded as
    #: one rather than being presented as a measurement.
    stated_by: str = "caller"


# ---------------------------------------------------------------------------
# per-step evidence state
# ---------------------------------------------------------------------------


class CandidateRouteStepEvidence(BaseModel):
    """What is actually known about one step of a candidate route.

    `candidate_status` and `evidence_level` are separate fields because they
    answer different questions, and merging them is how a candidate gets
    described as verified:

        candidate_status  the Phase 1-3 status of the step itself -- what the
                          provider proposed and what the structural validator
                          made of it. Never moves on evidence alone.
        evidence_level    how far the *reaction-level* evidence chain climbed.

    A step can be `LITERATURE_SUPPORTED` while its evidence level is only
    `LITERATURE_DISCOVERY`, and that is not a contradiction: the first says a
    structure was matched somewhere, the second says we have no reaction-level
    proof. Both are shown, because a reader who saw only one of them would be
    misled by whichever one flattered the route more.
    """

    step_id: str
    step_index: int = 0
    candidate_status: StepStatus = StepStatus.CANDIDATE
    evidence_level: ReactionEvidenceLevel = (
        ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE)
    match_type: ReactionMatchType = ReactionMatchType.NO_MATCH

    #: References whose structures were compared with this step.
    literature_support: list[str] = Field(default_factory=list)
    #: Reaction records attached to this step, and only this step.
    experimental_support: list[str] = Field(default_factory=list)

    evidence_refs: list[str] = Field(default_factory=list)
    #: Cited ids that did not resolve in the registry. Non-empty means the
    #: report is incomplete, and the report says so rather than rounding the
    #: level down to hide it.
    unresolved_evidence_refs: list[str] = Field(default_factory=list)

    support_source: str = ""
    support_reason: str = ""
    #: Where the top rung came from: a literature source name, or
    #: `user_provided`. Carried separately from `evidence_level` so a user
    #: document is never reported as general literature support.
    support_provenance: str = ""

    doi: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)

    #: Records a caller attached without a structure comparison. Listed so the
    #: procedure is not lost, and explicitly *not* counted as support.
    unlinked_records: list[str] = Field(default_factory=list)
    matches: list[ReactionMatchResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RouteEvidenceReport(BaseModel):
    """One route, and the evidence chain for each of its steps.

    Nothing here ranks, scores or recommends. Two routes are described; which
    to attempt is not a question this project answers, because answering it
    would need a success probability and no source for one exists here.
    """

    route_id: str
    target: str = ""
    route_source: str = "UNKNOWN"
    provider_score: float | None = None
    score_source: str = "none"

    steps: list[CandidateRouteStepEvidence] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    #: Recorded so a reader can see the boundary rather than infer it. There is
    #: deliberately no overall level for the route: a three-step route with two
    #: supported steps is not "mostly supported", and a single word for that
    #: would be an invention.
    levels_present: list[ReactionEvidenceLevel] = Field(default_factory=list)

    def step(self, step_id: str) -> CandidateRouteStepEvidence | None:
        for item in self.steps:
            if item.step_id == step_id:
                return item
        return None
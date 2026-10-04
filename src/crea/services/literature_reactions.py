"""Linking literature records to route steps, and measuring the distance.

The pipeline this module implements is deliberately short, because the data
behind it is short:

    a step's structures
        -> literature records
        -> a comparison, when there are structures to compare
        -> an evidence level

What the literature providers actually return is metadata and abstracts. None
of that contains a reactant. So `candidates_from_papers` produces candidates
with `NO_REACTION_STRUCTURE_AVAILABLE`, and every one of them lands at
`DISCOVERY_ONLY`: the work exists, we can name it, and that is the entire
claim. A title containing our target's name does not become a reaction. There
is no extraction here that could turn "Synthesis of X" into structures,
because the only ways to do that -- reading a reaction table, resolving a name
-- need components this project does not have and has decided not to add.

Structures enter through `candidate_with_structures`, which a caller uses when
it has genuinely read them out of a document or a permitted full text. That is
the only door, and it takes the evidence those structures rest on.
"""

from __future__ import annotations

from crea.core.models.literature import Paper
from crea.core.models.reaction_linking import (
    LiteratureReactionCandidate,
    ReactionEvidenceLevel,
    ReactionMatchResult,
    ReactionReference,
    ReactionStructureAvailability,
)
from crea.core.models.route import ReactionMatchType

#: The source name for anything that came off the operator's disk. Matches
#: `crea.services.document_evidence.DOCUMENT_SOURCE`; restated rather than
#: imported because `core` models must not depend on a service.
USER_SOURCE = "user_provided"


def candidates_from_papers(papers: list[Paper]) -> list[LiteratureReactionCandidate]:
    """One candidate per work, carrying no structures, as the providers leave it.

    Named `candidates_from_papers` rather than `reactions_from_papers` because
    the honest thing these are is *places a reaction might be*. Calling them
    reactions would put the word "reaction" in the type of a record that has
    none.
    """
    out: list[LiteratureReactionCandidate] = []
    for paper in papers:
        source = paper.sources[0] if paper.sources else "unknown"
        source_id = paper.source_ids.get(source) or paper.doi or paper.paper_id
        out.append(LiteratureReactionCandidate.build(
            source=source,
            source_id=source_id or "",
            paper_id=paper.paper_id,
            doi=paper.identifiers.doi,
            title=paper.title or "",
            url=paper.url,
            retrieved_at=paper.retrieved_at or "",
        ))
    return out


def candidate_with_structures(
    *,
    source: str,
    source_id: str,
    paper_id: str | None = None,
    doi: str | None = None,
    title: str = "",
    url: str | None = None,
    retrieved_at: str = "",
    reactants: list[str],
    products: list[str],
    evidence_refs: list[str],
    structure_origin: str,
) -> LiteratureReactionCandidate:
    """A candidate whose structures were read out of something.

    `structure_origin` is required rather than defaulted, and `evidence_refs`
    is required rather than optional, because both change what the candidate
    is entitled to claim. A structure nobody can point at is still a
    structure, but the report has to be able to say where it came from.
    """
    candidate = LiteratureReactionCandidate.build(
        source=source,
        source_id=source_id,
        paper_id=paper_id,
        doi=doi,
        title=title,
        url=url,
        retrieved_at=retrieved_at,
        reactants=reactants,
        products=products,
        structure_origin=structure_origin,
    )
    if not evidence_refs:
        candidate.warnings.append(
            "these structures are not backed by an evidence id; they cannot "
            "raise a step above a discovery on their own")
    return candidate


def reference_for(candidate: LiteratureReactionCandidate) -> ReactionReference:
    return ReactionReference.for_candidate(candidate)


def match_candidate(
    step_id: str,
    candidate: LiteratureReactionCandidate,
    reference: ReactionReference,
    step_reactants: list[str],
    step_products: list[str],
    evidence_refs: list[str] | None = None,
    structures_resolved: bool = True,
) -> ReactionMatchResult:
    """Measure one literature candidate against one step.

    `structures_resolved` is the caller's statement that the evidence ids the
    candidate's structures rest on are all present in the registry. It is a
    parameter rather than an assumption because a match whose structures point
    at evidence nobody holds is not the same finding as one that points at
    evidence we can produce -- and the difference is exactly the one that turns
    a structural claim into a citable one.

    Never consults a model. The only inputs are the two sets of structures and
    the two sets of evidence ids.
    """
    from crea.services.reaction_matching import compare_reactions

    evidence_refs = list(evidence_refs or [])
    result = ReactionMatchResult(
        step_id=step_id,
        reference_id=reference.reference_id,
        candidate_id=candidate.candidate_id,
        source=candidate.source,
        source_id=candidate.source_id,
        paper_id=candidate.paper_id,
        doi=candidate.doi,
        title=candidate.title,
        evidence_refs=evidence_refs,
        notes=list(candidate.warnings),
    )

    if candidate.structure_availability is (
            ReactionStructureAvailability.NO_REACTION_STRUCTURE_AVAILABLE):
        # The most common branch, and the one that must never be mistaken for
        # a near miss. This record exists; it says nothing about the reaction.
        result.match_type = ReactionMatchType.DISCOVERY_ONLY
        result.evidence_level = ReactionEvidenceLevel.LITERATURE_DISCOVERY
        result.match_basis = (
            "this record carries no reaction structures, so no comparison "
            "could be made; the work was found and nothing more is known "
            "about whether it runs this reaction")
        result.notes.append(
            "a title, abstract or DOI is not a reaction record: it cannot "
            "support a step and must not be reported as if it did")
        return result

    comparison = compare_reactions(
        step_reactants, step_products,
        candidate.reactants, candidate.products,
        source_id=candidate.source_id, doi=candidate.doi, title=candidate.title,
    )
    result.matched_reactants = list(comparison.matched_reactants)
    result.matched_products = list(comparison.matched_products)
    result.unmatched_reactants = list(comparison.unmatched_reactants)
    result.unmatched_products = list(comparison.unmatched_products)
    result.best_similarity = comparison.best_similarity
    result.notes.extend(comparison.notes)

    if comparison.match_type is ReactionMatchType.EXACT:
        result.match_type = ReactionMatchType.EXACT
        if not evidence_refs:
            result.match_basis = (
                "both sides matched exactly, but the structures are not backed "
                "by an evidence id, so this cannot be cited")
            result.notes.append(
                "an exact structure match with nothing to cite is a finding "
                "about structures, not evidence about a reaction")
            # Held at LITERATURE_DISCOVERY, not left at RETROSYNTHESIS_CANDIDATE:
            # a work was found and compared against this step, which is already
            # more than "a tool proposed it". Saying otherwise would describe a
            # step that has been through a comparison as one nothing ever touched.
            result.evidence_level = ReactionEvidenceLevel.LITERATURE_DISCOVERY
        elif not structures_resolved:
            result.match_basis = (
                "both sides matched exactly, but the evidence the structures "
                "rest on could not be resolved")
            result.notes.append(
                "the cited evidence is not in the registry; the match is held "
                "at discovery level until it is")
            result.evidence_level = ReactionEvidenceLevel.LITERATURE_DISCOVERY
        else:
            result.match_basis = (
                "both the reactants and the products matched exactly, and the "
                "structures are backed by citable text")
            result.evidence_level = ReactionEvidenceLevel.LITERATURE_REACTION_SUPPORT
        return result

    if comparison.match_type is ReactionMatchType.PARTIAL:
        result.match_type = ReactionMatchType.PARTIAL
        result.match_basis = (
            "one side matched exactly and the other did not; this is a "
            "different substrate combination from the one asked about")
    elif comparison.match_type is ReactionMatchType.TRANSFORMATION:
        result.match_type = ReactionMatchType.TRANSFORMATION
        result.match_basis = (
            "no exact match on either side; the molecules are related and the "
            "transformation may be the same kind, but nothing here shows this "
            "step was run")
    else:
        # The matcher ran and found nothing. Not the same as having had
        # nothing to compare with, and reported as a different value so a
        # reader can tell "we checked" from "we could not check".
        result.match_type = ReactionMatchType.NO_MATCH
        result.match_basis = (
            "structures were compared and none of them matched this step")

    result.evidence_level = ReactionEvidenceLevel.LITERATURE_DISCOVERY
    return result


def is_user_provided(source: str) -> bool:
    """Is this evidence from a file the operator handed us?"""
    return source == USER_SOURCE
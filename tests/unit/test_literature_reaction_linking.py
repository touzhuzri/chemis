"""Literature candidates, and what they are allowed to become.

The literature layer in this project produces **metadata**: works, titles,
abstracts, DOIs. It does not produce reactions -- no provider hands back a
parsed scheme, and building one from a title would mean guessing at chemistry.
So the ordinary case is a record with no reaction structures in it, and the
ordinary answer is `DISCOVERY_ONLY`.

That is the uncomfortable branch this file is mostly about. A paper titled
"Synthesis of 1,4-diphenylbutadiyne" plainly *concerns* our product. It still
does not report our reaction, because nothing in it says which structures were
combined and under what conditions. `test_title_match_never_becomes_reaction_support`
is the test that fails if somebody later teaches this layer to read a reaction
out of a title, and it is written to fail loudly rather than to drift.
"""

import pytest

from crea.core.models.literature import EvidenceStrength
from crea.core.models.evidence import Evidence, EvidenceType, Locator
from crea.core.models.reaction_linking import (
    LiteratureReactionCandidate,
    ReactionEvidenceLevel,
    ReactionStructureAvailability,
)
from crea.core.models.route import ReactionMatchType
from crea.services.literature_reactions import (
    USER_SOURCE,
    candidate_with_structures,
    candidates_from_papers,
    is_user_provided,
    match_candidate,
    reference_for,
)
from tests.fakes.fixtures import parse_crossref_message

PA = "C#Cc1ccccc1"                       # phenylacetylene
DIYNE = "c1ccccc1C#CC#Cc2ccccc2"         # diphenyl butadiyne
TOLUENE = "Cc1ccccc1"
BENZENE = "c1ccccc1"

RETRIEVED = "2026-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def a_paper(title: str, doi: str = "10.1234/synthesis-of-x"):
    payload = {"message": {
        "DOI": doi,
        "title": [title],
        "container-title": ["J. Org. Chem."],
        "issued": {"date-parts": [[2020, 1, 1]]},
        "URL": f"https://doi.org/{doi}",
        "type": "journal-article",
    }}
    return parse_crossref_message(payload, retrieved_at=RETRIEVED)


def evidence(quote: str = "Scheme 1: 2a + 1a, 80 C, 12 h") -> Evidence:
    return Evidence.build(
        evidence_type=EvidenceType.EXPERIMENTAL,
        strength=EvidenceStrength.PRIMARY_DOCUMENT_EVIDENCE,
        source="crossref",
        source_id="10.1234/synthesis-of-x",
        raw_quote=quote,
        locator=Locator(locator_type="document_region", document_id="doc-1",
                        page_start=5, section="Experimental"),
        retrieved_at=RETRIEVED,
        doi="10.1234/synthesis-of-x",
    )


def structured_candidate(reactants: list[str], products: list[str],
                         refs: list[str], source: str = "crossref"):
    return candidate_with_structures(
        source=source, source_id="10.1234/synthesis-of-x",
        paper_id="10.1234/synthesis-of-x", doi="10.1234/synthesis-of-x",
        title="Synthesis of 1,4-diphenylbutadiyne", retrieved_at=RETRIEVED,
        reactants=reactants, products=products,
        evidence_refs=refs, structure_origin="scheme read out of a supplied PDF",
    )


def match(candidate, step_reactants, step_products, refs=None, resolved=True):
    return match_candidate(
        step_id="step-1", candidate=candidate,
        reference=reference_for(candidate),
        step_reactants=step_reactants, step_products=step_products,
        evidence_refs=refs, structures_resolved=resolved)


# ===========================================================================
# what a provider actually hands back
# ===========================================================================


def test_a_search_result_becomes_a_candidate_with_no_structures():
    papers = [a_paper("Synthesis of 1,4-diphenylbutadiyne"),
              a_paper("Alkyne couplings in the solid state", doi="10.1234/other")]

    candidates = candidates_from_papers(papers)

    assert len(candidates) == 2
    for candidate in candidates:
        assert candidate.structure_availability is (
            ReactionStructureAvailability.NO_REACTION_STRUCTURE_AVAILABLE)
        assert candidate.reactants == [] and candidate.products == []
        assert candidate.warnings, "a structureless record must say so on its face"


def test_structure_availability_is_a_fact_about_the_record_not_a_choice():
    """The validator derives it from the structures; it is not a flag to set.

    A candidate built without structures says so on its own face, whatever
    anybody would have preferred it to say.
    """
    bare = LiteratureReactionCandidate.build(
        source="crossref", source_id="10.1/x", paper_id="10.1/x",
        title="t", retrieved_at=RETRIEVED)
    with_structures = structured_candidate([PA], [DIYNE], ["ev-1"])

    assert bare.structure_availability is (
        ReactionStructureAvailability.NO_REACTION_STRUCTURE_AVAILABLE)
    assert with_structures.structure_availability is (
        ReactionStructureAvailability.AVAILABLE)
    # naming it does not make it so
    assert "structure_availability" not in {
        k for k, v in bare.__dict__.items() if v != getattr(bare, k)}


def test_the_citation_identifies_the_work_not_the_structures():
    """Two readings of one paper are one reference.

    `reference_id` is what an evidence id will be attached to. If it moved when
    somebody re-read the paper and typed the structures slightly differently,
    every downstream citation would silently come loose. The `candidate_id`
    *does* include the structures, and should: a candidate is a proposed
    reaction, and re-reading the same work may propose a different one.
    """
    first = LiteratureReactionCandidate.build(
        source="crossref", source_id="10.1234/synthesis-of-x",
        paper_id="10.1234/synthesis-of-x", doi="10.1234/synthesis-of-x",
        title="Synthesis of 1,4-diphenylbutadiyne", retrieved_at=RETRIEVED)
    second = structured_candidate([PA], [DIYNE], ["ev-1"])
    third = structured_candidate([TOLUENE], [BENZENE], ["ev-1"])

    assert reference_for(first).reference_id == reference_for(second).reference_id
    assert reference_for(second).reference_id == reference_for(third).reference_id
    assert first.candidate_id != second.candidate_id


def test_two_works_get_two_candidates():
    a = structured_candidate([PA], [DIYNE], ["ev-1"])
    b = structured_candidate([PA], [DIYNE], ["ev-1"], source="openalex")
    assert a.candidate_id != b.candidate_id


# ===========================================================================
# the refusal this file is about
# ===========================================================================


def test_title_match_never_becomes_reaction_support():
    """A paper about our product is not a paper reporting our reaction.

    The title names the product. It does not name the reactants, does not say
    whether the reported chemistry is the transformation our step proposes,
    and does not give a single condition. Every field that would let a reader
    believe this work supports the step must come back empty.
    """
    paper = a_paper("Synthesis of 1,4-diphenylbutadiyne by Glaser coupling")
    assert "diphenylbutadiyne" in paper.title.lower()

    (candidate,) = candidates_from_papers([paper])
    result = match(candidate, [PA], [DIYNE])

    assert result.match_type is ReactionMatchType.DISCOVERY_ONLY
    assert result.evidence_level is ReactionEvidenceLevel.LITERATURE_DISCOVERY
    assert result.best_similarity is None
    assert result.matched_reactants == [] and result.matched_products == []
    assert result.evidence_refs == []
    assert "no reaction structures" in result.match_basis
    # and it says, in as many words, that metadata is not a reaction record
    assert any("title, abstract or DOI is not a reaction record" in n
               for n in result.notes)


def test_a_title_matching_the_reactants_too_is_still_only_a_discovery():
    """Naming every molecule in the title does not make it a scheme.

    This is the stronger form of the same refusal: a title that names the
    reactants *and* the products reads like a reaction to a human, and it is
    still not one, because it says nothing about what was done to what.
    """
    (candidate,) = candidates_from_papers(
        [a_paper("Phenylacetylene plus diphenylbutadiyne: a study")])
    result = match(candidate, [PA], [DIYNE])

    assert result.match_type is ReactionMatchType.DISCOVERY_ONLY
    assert result.evidence_level is ReactionEvidenceLevel.LITERATURE_DISCOVERY


def test_an_abstract_is_not_a_scheme_either():
    """Whatever the provider calls it, the branch is decided by structures."""
    paper = a_paper("Synthesis of 1,4-diphenylbutadiyne")
    paper = paper.model_copy(update={"abstract": (
        "Phenylacetylene was coupled to give the title compound in 82% yield "
        "at 80 C for 12 h.")})
    (candidate,) = candidates_from_papers([paper])

    result = match(candidate, [PA], [DIYNE])

    assert result.match_type is ReactionMatchType.DISCOVERY_ONLY
    assert result.evidence_refs == []


def test_discovery_only_is_not_a_near_miss():
    """`DISCOVERY_ONLY` says nothing was compared; `NO_MATCH` says it did not fit.

    Collapsing them would tell a reader that we looked at this reaction and it
    was wrong, when in fact we never had it to look at.
    """
    paper = a_paper("Synthesis of 1,4-diphenylbutadiyne")
    (discovery,) = candidates_from_papers([paper])
    unrelated = structured_candidate([TOLUENE], [BENZENE], ["ev-1"])

    no_structures = match(discovery, [PA], [DIYNE])
    with_structures = match(unrelated, [PA], [DIYNE])

    assert no_structures.match_type is ReactionMatchType.DISCOVERY_ONLY
    assert with_structures.match_type is ReactionMatchType.NO_MATCH
    assert with_structures.best_similarity is not None


# ===========================================================================
# structures that were actually read
# ===========================================================================


def test_exact_structures_with_citable_evidence_reach_literature_reaction_support():
    item = evidence()
    candidate = structured_candidate([PA], [DIYNE], [item.evidence_id])

    result = match(candidate, [PA], [DIYNE], [item.evidence_id])

    assert result.match_type is ReactionMatchType.EXACT
    assert result.evidence_level is ReactionEvidenceLevel.LITERATURE_REACTION_SUPPORT
    assert "matched exactly" in result.match_basis
    assert "citable text" in result.match_basis
    assert result.evidence_refs == [item.evidence_id]


def test_exact_structures_with_no_evidence_stay_at_discovery():
    """The structures match; nothing citable says they are that work's."""
    candidate = structured_candidate([PA], [DIYNE], [])

    result = match(candidate, [PA], [DIYNE], [])

    assert result.match_type is ReactionMatchType.EXACT
    assert result.evidence_level is ReactionEvidenceLevel.LITERATURE_DISCOVERY
    assert "evidence" in result.match_basis.lower()


def test_exact_structures_whose_evidence_does_not_resolve_stay_at_discovery():
    """A match resting on an id nobody holds is not yet a citable match."""
    candidate = structured_candidate([PA], [DIYNE], ["ev-does-not-exist"])

    result = match(candidate, [PA], [DIYNE], ["ev-does-not-exist"],
                   resolved=False)

    assert result.match_type is ReactionMatchType.EXACT
    assert result.evidence_level is ReactionEvidenceLevel.LITERATURE_DISCOVERY
    assert "evidence" in result.match_basis.lower()


def test_partial_and_transformation_matches_never_reach_reaction_support():
    """Three real substrate combinations: one right, one half-right, one related.

    The middle one has the right product and the wrong reactant; the last one
    is a genuinely related coupling that did not use our alkyne. Neither is the
    step, and neither may be reported as if it were.
    """
    item = evidence()
    half_right = structured_candidate([TOLUENE], [DIYNE], [item.evidence_id])
    related = structured_candidate([TOLUENE], ["c1ccccc1C#Cc1ccccc1"],
                                   [item.evidence_id])

    a = match(half_right, [PA], [DIYNE], [item.evidence_id])
    b = match(related, [PA], [DIYNE], [item.evidence_id])

    assert a.match_type is ReactionMatchType.PARTIAL
    assert b.match_type is ReactionMatchType.TRANSFORMATION
    for result in (a, b):
        assert result.evidence_level is ReactionEvidenceLevel.LITERATURE_DISCOVERY
        assert "different substrate combination" in result.match_basis or (
            "may be the same kind" in result.match_basis)


def test_a_similar_reaction_is_reported_under_its_own_name():
    """Toluene and diphenylmethane are not the same compound."""
    item = evidence()
    candidate = structured_candidate([TOLUENE], ["c1ccccc1Cc1ccccc1"],
                                     [item.evidence_id])

    result = match(candidate, [PA], [DIYNE], [item.evidence_id])

    assert result.match_type is not ReactionMatchType.EXACT
    assert result.evidence_level is ReactionEvidenceLevel.LITERATURE_DISCOVERY
    # the resemblance is visible, but it is labelled a resemblance
    assert result.best_similarity is not None


# ===========================================================================
# provenance
# ===========================================================================


def test_user_provided_is_a_distinguishable_source():
    assert is_user_provided(USER_SOURCE)
    assert not is_user_provided("crossref")
    assert not is_user_provided("openalex")


def test_a_candidate_from_a_local_file_still_says_where_it_came_from():
    candidate = structured_candidate([PA], [DIYNE], ["ev-1"],
                                     source=USER_SOURCE)
    assert candidate.source == USER_SOURCE
    assert candidate.structure_origin
    assert reference_for(candidate).source == USER_SOURCE


def test_structures_with_no_evidence_are_flagged_on_the_candidate():
    candidate = structured_candidate([PA], [DIYNE], [])
    assert any("evidence id" in w for w in candidate.warnings)


def test_the_match_result_carries_no_probability():
    """A similarity is a measurement; a probability would be a claim."""
    item = evidence()
    result = match(structured_candidate([PA], [DIYNE], [item.evidence_id]),
                   [PA], [DIYNE], [item.evidence_id])

    fields = " ".join(result.model_dump_json().lower().split('"'))
    identifiers = " ".join(f for f in fields.split() if f.strip(",:{}").isidentifier())
    for forbidden in ("confidence", "probability", "success_rate"):
        assert forbidden not in identifiers


@pytest.mark.parametrize("source", ["crossref", "openalex", "unpaywall"])
def test_no_provider_is_treated_as_a_reaction_source(source):
    """Every provider is a metadata provider. None of them is special-cased."""
    (candidate,) = candidates_from_papers(
        [a_paper(f"A study by {source}", doi=f"10.1234/{source}")])
    candidate = candidate.model_copy(update={"source": source})
    # the source is a label; it must not open a door
    assert candidate.structure_availability is (
        ReactionStructureAvailability.NO_REACTION_STRUCTURE_AVAILABLE)
    result = match_candidate(step_id="step-1", candidate=candidate,
                             reference=reference_for(candidate),
                             step_reactants=[PA], step_products=[DIYNE])
    assert result.evidence_level is ReactionEvidenceLevel.LITERATURE_DISCOVERY
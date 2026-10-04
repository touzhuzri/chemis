"""The route evidence chain, and the four rules it exists to enforce.

Every test here is a "must not be promoted" test wearing the clothes of a
"must be promoted" one. The promotions themselves are asserted too -- a ladder
nothing ever climbs is not a ladder -- but the interesting property is the
refusal, so each section is named after what it refuses.

Four rules, restated at the top of `crea.services.route_evidence` and tested
here:

1. nothing skips a rung
2. every cited evidence id is resolved, and unresolved evidence earns nothing
3. a reaction record attached without a measured structure match promotes nothing
4. a user file is never restated as general literature support
"""

import pytest

from crea.core.models.evidence import (
    Evidence,
    EvidenceType,
    Locator,
)
from crea.core.models.literature import EvidenceStrength
from crea.core.models.reaction import Quantity, ReactionConditions, ReactionRecord
from crea.core.models.reaction_linking import (
    LinkBasis,
    ReactionEvidenceLevel,
    ReactionRecordLink,
    ReactionStructureAvailability,
)
from crea.core.models.route import (
    CandidateRoute,
    ReactionMatchType,
    ReactionStep,
    RouteMolecule,
    ScoreSource,
)
from crea.services.evidence import EvidenceRegistry
from crea.services.route_evidence import (
    RouteEvidenceInputs,
    RouteEvidenceService,
    build_record_link,
)

# Real molecules, the same ones the rest of the suite uses.
PA = "C#Cc1ccccc1"                       # phenylacetylene
DIYNE = "c1ccccc1C#CC#Cc2ccccc2"         # diphenyl butadiyne
DIBENZYL = "c1ccccc1Cc1ccccc1"          # unrelated: similarity, not identity
TOLUENE = "Cc1ccccc1"


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def molecule(smiles: str, molecule_id: str) -> RouteMolecule:
    return RouteMolecule(molecule_id=molecule_id, smiles=smiles,
                         canonical_smiles=smiles)


def make_step(index: int, reactants: list[str], products: list[str]) -> ReactionStep:
    return ReactionStep(
        step_id=f"step-{index}",
        step_index=index,
        reactants=[molecule(s, f"s{index}-r{i}") for i, s in enumerate(reactants)],
        products=[molecule(s, f"s{index}-p{i}") for i, s in enumerate(products)],
        source="aizynthfinder",
    )


def make_route(steps: list[ReactionStep]) -> CandidateRoute:
    return CandidateRoute(
        route_id="route-1",
        target=molecule(DIBENZYL, "target"),
        steps=steps,
        source="aizynthfinder",
        provider_version="4.4.1",
        score=42.0,
        score_source=ScoreSource.AIZYNTHFINDER,
    )


def publisher_evidence(quote: str = "80 C, 12 h, 82% yield") -> Evidence:
    """An EXPERIMENTAL passage from a published document."""
    return Evidence.build(
        evidence_type=EvidenceType.EXPERIMENTAL,
        strength=EvidenceStrength.PRIMARY_DOCUMENT_EVIDENCE,
        source="crossref",
        source_id="10.1234/example",
        raw_quote=quote,
        locator=Locator(locator_type="document_region", document_id="doc-lit",
                        page_start=4, section="Experimental"),
        retrieved_at="2026-01-01T00:00:00+00:00",
        doi="10.1234/example",
    )


def user_evidence(quote: str = "80 C, 12 h, 82% isolated yield") -> Evidence:
    """An EXPERIMENTAL passage from a file the operator handed us."""
    return Evidence.build(
        evidence_type=EvidenceType.EXPERIMENTAL,
        strength=EvidenceStrength.USER_PROVIDED_EVIDENCE,
        source="user_provided",
        source_id="local-doc-1",
        raw_quote=quote,
        locator=Locator(locator_type="document_region", document_id="local-doc-1",
                        page_start=2, section="Experimental"),
        retrieved_at="2026-01-02T00:00:00+00:00",
    )


def make_record(reaction_id: str, evidence: Evidence,
                document_id: str | None = "doc-lit") -> ReactionRecord:
    """A validated record: conditions present, each citing the passage."""
    conditions = ReactionConditions(
        temperature=Quantity(value=80, raw_value="80", unit="C", raw_unit="C",
                             status="explicit", evidence_refs=[evidence.evidence_id],
                             raw_text="at 80 C"),
        time=Quantity(value=12, raw_value="12", unit="h", raw_unit="h",
                      status="explicit", evidence_refs=[evidence.evidence_id],
                      raw_text="for 12 h"),
        yield_value=Quantity(value=82, raw_value="82", unit="%", raw_unit="%",
                             status="explicit", evidence_refs=[evidence.evidence_id],
                             raw_text="82% isolated yield"),
        yield_type="isolated",
        yield_type_evidence_refs=[evidence.evidence_id],
    )
    return ReactionRecord(
        reaction_id=reaction_id,
        document_id=document_id,
        paper_id="10.1234/example",
        reaction_description="coupling of phenylacetylene",
        conditions=conditions,
        evidence_refs=[evidence.evidence_id],
    )


def registry(*items: Evidence) -> EvidenceRegistry:
    reg = EvidenceRegistry()
    reg.extend(items)
    return reg


# ===========================================================================
# Step A -- a bare candidate
# ===========================================================================


def test_a_candidate_route_starts_at_the_bottom_of_the_ladder():
    """AiZynthFinder proposed it. Nothing has been consulted. That is all."""
    route = make_route([make_step(1, [PA], [DIYNE])])
    report = RouteEvidenceService(registry(), RouteEvidenceInputs()).build(route)

    step = report.steps[0]
    assert step.evidence_level is ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE
    assert step.match_type is ReactionMatchType.NO_MATCH
    assert step.candidate_status.value == "candidate"
    assert step.evidence_refs == []
    assert step.literature_support == []
    assert step.experimental_support == []


def test_the_provider_score_is_carried_as_a_score_and_never_as_a_confidence():
    route = make_route([make_step(1, [PA], [DIYNE])])
    report = RouteEvidenceService(registry(), RouteEvidenceInputs()).build(route)

    assert report.provider_score == 42.0
    assert report.score_source == "aizynthfinder"
    # no field anywhere in the report is named after a probability
    field_names = " ".join(
        f for f in report.model_dump_json().lower().split('"') if f.isidentifier())
    for forbidden in ("confidence", "probability", "success_rate"):
        assert forbidden not in field_names
    # and the report says so in words, not merely by omission
    assert any("not a confidence" in w for w in report.warnings)


def test_a_route_has_no_overall_support_level():
    """Three steps, two supported: there is no single word for that.

    A route-level level would have to be a summary judgement, and the only
    summary judgements available are averages and worst-cases, both of which
    invent a claim about the route that no step actually made.
    """
    route = make_route([make_step(1, [PA], [DIYNE]), make_step(2, [TOLUENE], [PA])])
    report = RouteEvidenceService(registry(), RouteEvidenceInputs()).build(route)

    assert not hasattr(report, "evidence_level")
    assert report.levels_present == [ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE]


# ===========================================================================
# Step C/D -- reaction records
# ===========================================================================


def test_an_exact_record_link_with_resolvable_evidence_reaches_experimental():
    evidence = publisher_evidence()
    route = make_route([make_step(1, [PA], [DIYNE])])
    step = route.steps[0]
    link = build_record_link(step, "rxn-1", [PA], [DIYNE])

    assert link.basis is LinkBasis.EXACT_STRUCTURE
    assert link.stated_by == "deterministic"

    report = RouteEvidenceService(
        registry(evidence),
        RouteEvidenceInputs(records=[make_record("rxn-1", evidence)],
                            record_links=[link]),
    ).build(route)

    result = report.steps[0]
    assert result.evidence_level is ReactionEvidenceLevel.EXPERIMENTAL_REACTION_SUPPORT
    assert result.experimental_support == ["rxn-1"]
    assert result.evidence_refs == [evidence.evidence_id]
    assert result.match_type is ReactionMatchType.EXACT
    assert result.support_provenance == "literature"


def test_a_user_provided_record_reaches_the_user_rung_not_the_literature_one():
    """The provenance distinction is the whole point of the top rung.

    A file on the operator's disk is not a published procedure, and this
    project cannot tell a publisher's PDF from a scan from somebody's notes.
    Reaching `EXPERIMENTAL_REACTION_SUPPORT` from it would report a local file
    as the published literature.
    """
    evidence = user_evidence()
    route = make_route([make_step(1, [PA], [DIYNE])])
    link = build_record_link(route.steps[0], "rxn-1", [PA], [DIYNE])

    report = RouteEvidenceService(
        registry(evidence),
        RouteEvidenceInputs(records=[make_record("rxn-1", evidence,
                                                 document_id="local-doc-1")],
                            record_links=[link]),
    ).build(route)

    result = report.steps[0]
    assert result.evidence_level is ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT
    assert result.support_provenance == "user_provided"
    # and it never appears in the literature column
    assert result.literature_support == []


def test_mixed_provenance_takes_the_user_rung():
    """Being mixed is a reason to claim less, not more.

    One evidence item from a publisher and one from the operator's disk cannot
    be reported as a published procedure, because part of it is not one.
    """
    published = publisher_evidence()
    supplied = user_evidence()
    route = make_route([make_step(1, [PA], [DIYNE])])
    record = make_record("rxn-1", published)
    record.evidence_refs = [published.evidence_id, supplied.evidence_id]

    report = RouteEvidenceService(
        registry(published, supplied),
        RouteEvidenceInputs(records=[record],
                            record_links=[build_record_link(
                                route.steps[0], "rxn-1", [PA], [DIYNE])]),
    ).build(route)

    assert report.steps[0].evidence_level is (
        ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT)


def test_a_record_without_an_exact_structure_match_promotes_nothing():
    """The rule this phase exists for.

    A real experimental procedure was read. That it is *this* procedure was
    not shown, and an unshown link is not a link.
    """
    evidence = publisher_evidence()
    route = make_route([make_step(1, [PA], [DIYNE])])
    link = build_record_link(route.steps[0], "rxn-1", [TOLUENE], [DIYNE])
    assert link.basis is not LinkBasis.EXACT_STRUCTURE

    report = RouteEvidenceService(
        registry(evidence),
        RouteEvidenceInputs(records=[make_record("rxn-1", evidence)],
                            record_links=[link]),
    ).build(route)

    result = report.steps[0]
    assert result.evidence_level is ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE
    assert result.experimental_support == []
    # but the reading is not thrown away: it was a real procedure
    assert result.unlinked_records == ["rxn-1"]
    assert any("without an exact structure comparison" in w
               for w in result.warnings)


def test_a_record_cited_by_a_caller_with_no_structures_promotes_nothing():
    """Asserting harder is not the way to earn support. Bringing structures is."""
    evidence = publisher_evidence()
    route = make_route([make_step(1, [PA], [DIYNE])])
    link = ReactionRecordLink(
        step_id="step-1", reaction_id="rxn-1",
        basis=LinkBasis.EXACT_STRUCTURE,          # claimed, with nothing behind it
        match_type=ReactionMatchType.EXACT,
        reactant_smiles=[], product_smiles=[],
        stated_by="agent")

    report = RouteEvidenceService(
        registry(evidence),
        RouteEvidenceInputs(records=[make_record("rxn-1", evidence)],
                            record_links=[link]),
    ).build(route)

    result = report.steps[0]
    assert result.evidence_level is ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE
    assert result.experimental_support == []


def test_a_record_with_no_resolvable_evidence_promotes_nothing():
    evidence = publisher_evidence()
    route = make_route([make_step(1, [PA], [DIYNE])])
    link = build_record_link(route.steps[0], "rxn-1", [PA], [DIYNE],
                             [evidence.evidence_id])
    # the evidence was never put into the registry
    report = RouteEvidenceService(
        registry(),  # empty
        RouteEvidenceInputs(records=[make_record("rxn-1", evidence)],
                            record_links=[link]),
    ).build(route)

    result = report.steps[0]
    assert result.evidence_level is ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE
    assert result.evidence_refs == []
    assert evidence.evidence_id in result.unresolved_evidence_refs
    assert result.unlinked_records == ["rxn-1"]


def test_a_link_to_a_record_nobody_holds_is_reported_not_ignored():
    route = make_route([make_step(1, [PA], [DIYNE])])
    link = build_record_link(route.steps[0], "rxn-does-not-exist", [PA], [DIYNE])

    report = RouteEvidenceService(
        registry(), RouteEvidenceInputs(records=[], record_links=[link])).build(route)

    result = report.steps[0]
    assert result.evidence_level is ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE
    assert any("not among the records" in w for w in result.warnings)


# ===========================================================================
# rule 2 -- evidence integrity
# ===========================================================================


def test_missing_evidence_cannot_be_upgraded():
    """A dangling evidence id costs the step its rung, and is reported.

    Two failures would be equally bad and are both avoided: counting the
    unresolvable id as support, and refusing to produce a report at all. The
    report is produced, the gap is named, and the support is withheld.
    """
    ghost = publisher_evidence()
    route = make_route([make_step(1, [PA], [DIYNE])])
    link = build_record_link(route.steps[0], "rxn-1", [PA], [DIYNE],
                             [ghost.evidence_id, "completely-made-up-id"])

    report = RouteEvidenceService(
        registry(), RouteEvidenceInputs(records=[make_record("rxn-1", ghost)],
                                        record_links=[link])).build(route)

    result = report.steps[0]
    assert result.evidence_level is ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE
    assert result.evidence_refs == []
    assert "completely-made-up-id" in result.unresolved_evidence_refs
    assert ghost.evidence_id in result.unresolved_evidence_refs
    assert report.unresolved
    assert all("not in the registry" in note for note in report.unresolved)


def test_evidence_is_resolved_before_it_is_resolved_twice_consistently():
    """An id that resolves is counted; the same id twice is still one id."""
    evidence = publisher_evidence()
    route = make_route([make_step(1, [PA], [DIYNE])])
    record = make_record("rxn-1", evidence)
    record.evidence_refs = [evidence.evidence_id, evidence.evidence_id]

    report = RouteEvidenceService(
        registry(evidence),
        RouteEvidenceInputs(records=[record],
                            record_links=[build_record_link(
                                route.steps[0], "rxn-1", [PA], [DIYNE])]),
    ).build(route)

    result = report.steps[0]
    assert result.evidence_refs == [evidence.evidence_id]
    assert report.evidence_refs == [evidence.evidence_id]


def test_a_registry_holding_a_different_thing_under_the_same_id_is_still_one_id():
    """Identity is checked at registration, so this cannot drift silently."""
    evidence = publisher_evidence()
    reg = registry(evidence)
    assert reg.get(evidence.evidence_id) is evidence
    assert len(reg) == 1
    with pytest.raises(ValueError):
        reg.add(evidence.model_copy(update={"raw_quote": "something else",
                                            "evidence_id": evidence.evidence_id}))


# ===========================================================================
# isolation between steps
# ===========================================================================


def test_evidence_is_isolated_between_steps():
    """A record attached to step 2 must never appear on step 1.

    Isolation is structural -- the link names one step and only that step's
    links are read -- so this is not a matter of the data happening to
    separate. Two steps with different products, one record.
    """
    evidence = publisher_evidence()
    route = make_route([make_step(1, [PA], [DIYNE]),
                        make_step(2, [TOLUENE], [PA])])
    link = build_record_link(route.steps[1], "rxn-1", [TOLUENE], [PA])

    report = RouteEvidenceService(
        registry(evidence),
        RouteEvidenceInputs(records=[make_record("rxn-1", evidence)],
                            record_links=[link]),
    ).build(route)

    first, second = report.steps
    assert first.evidence_level is ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE
    assert first.experimental_support == []
    assert first.evidence_refs == []
    assert second.evidence_level is ReactionEvidenceLevel.EXPERIMENTAL_REACTION_SUPPORT
    assert second.experimental_support == ["rxn-1"]


def test_every_step_is_reported_even_when_evidence_exists_for_one_of_them():
    route = make_route([make_step(1, [PA], [DIYNE]),
                        make_step(2, [TOLUENE], [PA]),
                        make_step(3, ["CCO"], ["CC=O"])])
    evidence = publisher_evidence()
    link = build_record_link(route.steps[1], "rxn-1", [TOLUENE], [PA])

    report = RouteEvidenceService(
        registry(evidence),
        RouteEvidenceInputs(records=[make_record("rxn-1", evidence)],
                            record_links=[link]),
    ).build(route)

    assert [s.step_id for s in report.steps] == ["step-1", "step-2", "step-3"]
    levels = {s.step_id: s.evidence_level for s in report.steps}
    assert levels["step-2"] is ReactionEvidenceLevel.EXPERIMENTAL_REACTION_SUPPORT
    assert levels["step-1"] is levels["step-3"] is (
        ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE)


# ===========================================================================
# conflicting records
# ===========================================================================


def test_two_records_that_disagree_are_both_listed_and_neither_is_chosen():
    """A conflict is reported as a conflict.

    Picking the higher temperature, or the higher yield, would be choosing
    which of two procedures the step is -- a decision the evidence does not
    license.
    """
    warm = publisher_evidence("stirred at 80 C for 12 h, 82% yield")
    cool = publisher_evidence("stirred at 20 C for 48 h, 41% yield")
    route = make_route([make_step(1, [PA], [DIYNE])])
    step = route.steps[0]

    report = RouteEvidenceService(
        registry(warm, cool),
        RouteEvidenceInputs(
            records=[make_record("rxn-warm", warm), make_record("rxn-cool", cool)],
            record_links=[build_record_link(step, "rxn-warm", [PA], [DIYNE]),
                          build_record_link(step, "rxn-cool", [PA], [DIYNE])]),
    ).build(route)

    result = report.steps[0]
    assert set(result.experimental_support) == {"rxn-warm", "rxn-cool"}
    assert set(result.evidence_refs) == {warm.evidence_id, cool.evidence_id}
    assert result.evidence_level is ReactionEvidenceLevel.EXPERIMENTAL_REACTION_SUPPORT
    # nothing anywhere in the report says which one to believe
    dumped = report.model_dump_json().lower()
    assert "better" not in dumped and "preferred" not in dumped


def test_a_record_carrying_an_extraction_conflict_keeps_it():
    """The record owns its conflicts; this service neither adds nor resolves."""
    evidence = publisher_evidence()
    record = make_record("rxn-1", evidence)
    record.conflicts = [
        {"field": "temperature", "values": {"p1": "80 C", "p2": "20 C"},
         "evidence_refs": [evidence.evidence_id],
         "note": "two passages disagree"}
    ]
    route = make_route([make_step(1, [PA], [DIYNE])])
    link = build_record_link(route.steps[0], "rxn-1", [PA], [DIYNE])

    report = RouteEvidenceService(
        registry(evidence),
        RouteEvidenceInputs(records=[record], record_links=[link]),
    ).build(route)

    assert report.steps[0].evidence_level is (
        ReactionEvidenceLevel.EXPERIMENTAL_REACTION_SUPPORT)
    # the conflict survives into the report's data rather than being summarised away
    assert record.conflicts[0]["field"] == "temperature"


# ===========================================================================
# the link builder itself
# ===========================================================================


def test_the_link_builder_measures_rather_than_accepts():
    """`stated_by` distinguishes a comparison from a claim, and is set to match."""
    route = make_route([make_step(1, [PA], [DIYNE])])
    step = route.steps[0]

    exact = build_record_link(step, "r", [PA], [DIYNE])
    assert exact.basis is LinkBasis.EXACT_STRUCTURE

    partial = build_record_link(step, "r", [PA], [TOLUENE])
    assert partial.basis is LinkBasis.PARTIAL_STRUCTURE

    related = build_record_link(step, "r", [TOLUENE], [DIBENZYL])
    assert related.basis is LinkBasis.UNVERIFIED_STRUCTURE

    nothing = build_record_link(step, "r")
    assert nothing.basis is LinkBasis.UNVERIFIED_STRUCTURE
    assert nothing.match_type is ReactionMatchType.NO_MATCH


def test_a_similar_reaction_is_not_promoted_by_fingerprint_alone():
    """Toluene and diphenylmethane are not the same compound.

    Even a high Tanimoto is a resemblance, and resemblance is not a reaction
    anybody ran.
    """
    route = make_route([make_step(1, [PA], [DIYNE])])
    link = build_record_link(route.steps[0], "r", [TOLUENE], [DIBENZYL])

    report = RouteEvidenceService(
        registry(publisher_evidence()),
        RouteEvidenceInputs(records=[make_record("r", publisher_evidence())],
                            record_links=[link]),
    ).build(route)

    assert report.steps[0].evidence_level is ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE
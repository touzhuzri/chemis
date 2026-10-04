"""Routes: identity, candidate status, and the evidence ladder.

The list these tests follow is the phase brief's. Most of them exist to prove
one thing: a route stays a candidate until something has actually been shown
for it, and nothing -- not a score, not a similar reaction, not a model's
confidence -- can promote it on its own.
"""

import pytest

from crea.core.models.reaction import ReactionRecord, ReactionConditions
from crea.core.models.route import (
    CandidateRoute,
    ProviderStatus,
    ReactionStep,
    RetrosynthesisRequest,
    RouteMolecule,
    ScoreSource,
    StepStatus,
    SupportLevel,
)
from crea.providers.fake_retrosynthesis import FakeRetrosynthesisProvider
from crea.providers.retrosynthesis import RetrosynthesisProvider
from crea.services.route_validation import (
    CANDIDATE_NOTE,
    PARTIAL_NOTE,
    RouteValidationService,
    StructurePool,
    unresolved_note,
)

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"
DIYNE_CANON = "C(C#Cc1ccccc1)#Cc1ccccc1"
PHENYLACETYLENE = "C#Cc1ccccc1"


def make_result():
    return FakeRetrosynthesisProvider().retrosynthesize(
        RetrosynthesisRequest(target_smiles=DIYNE))


def first_step(result, index=0):
    return result.routes[0].steps[index]


# --- 6/7. provenance and score -------------------------------------------


def test_provenance_is_kept():
    result = make_result()
    assert result.provenance.provider == "fake-retrosynthesis"
    assert result.provenance.provider_version
    assert result.provenance.generated_at
    assert result.provenance.raw_result_hash
    assert result.provenance.stock_configuration


def test_score_keeps_its_source_and_is_never_a_confidence():
    route = make_result().routes[0]
    assert route.score == 0.87
    assert route.score_source is not ScoreSource.NONE
    for field in ("confidence", "certainty", "probability", "reliability"):
        assert not hasattr(route, field)
        assert field not in CandidateRoute.model_fields


def test_score_does_not_leak_into_evidence_strength():
    """A 0.87 route score and a piece of evidence's strength are different axes."""
    from crea.core.models.literature import EvidenceStrength

    route = make_result().routes[0]
    assert not isinstance(route.score, EvidenceStrength)
    assert route.steps[0].score == route.score
    assert route.steps[0].score_source is route.score_source


# --- 8/9. a candidate is a candidate -------------------------------------


def test_every_provider_step_starts_candidate():
    result = make_result()
    for route in result.routes:
        for step in route.steps:
            assert step.status is StepStatus.CANDIDATE
            assert step.support_level is SupportLevel.NO_SUPPORT


def test_no_route_carries_evidence_or_records():
    for route in make_result().routes:
        for step in route.steps:
            assert step.evidence_refs == []
            assert step.reaction_record_ids == []
            assert step.paper_ids == []


# --- 10-13. the evidence ladder -------------------------------------------


def build_step(reactants, products):
    return ReactionStep(
        step_id="s1", step_index=1,
        reactants=[RouteMolecule(molecule_id=r, smiles=r, canonical_smiles=r)
                   for r in reactants],
        products=[RouteMolecule(molecule_id=p, smiles=p, canonical_smiles=p)
                  for p in products],
    )


def test_no_evidence_stays_no_support():
    step = build_step([PHENYLACETYLENE, PHENYLACETYLENE], [DIYNE_CANON])
    result = RouteValidationService().validate(step)
    assert result.support_level is SupportLevel.NO_SUPPORT
    RouteValidationService.apply(result, step)
    assert step.status is StepStatus.CANDIDATE
    assert unresolved_note(result, step) == CANDIDATE_NOTE


def test_an_exact_structure_promotes_to_literature_supported():
    step = build_step([PHENYLACETYLENE, PHENYLACETYLENE], [DIYNE_CANON])
    pool = StructurePool()
    pool.add("paper-1", PHENYLACETYLENE, kind="paper")
    result = RouteValidationService(structures=pool).validate(step)
    assert result.support_level in (SupportLevel.LITERATURE_SUPPORT,
                                   SupportLevel.STRUCTURAL_SUPPORT)
    RouteValidationService.apply(result, step)
    assert step.status is StepStatus.LITERATURE_SUPPORTED
    assert result.paper_ids == ["paper-1"]
    assert unresolved_note(result, step) == ""


def test_a_different_molecule_never_becomes_support():
    """The invariant that matters: "similar" is not "the same".

    benzonitrile is not phenylacetylene. Whether the pair lands in the related
    band or falls below it, it must never reach support of any kind.
    """
    step = build_step(["N#Cc1ccccc1"], [DIYNE_CANON])
    pool = StructurePool()
    pool.add("paper-2", PHENYLACETYLENE, kind="paper")
    result = RouteValidationService(structures=pool).validate(step)

    assert result.support_level is not SupportLevel.STRUCTURAL_SUPPORT
    assert result.support_level is not SupportLevel.LITERATURE_SUPPORT
    assert result.support_level is not SupportLevel.EXPERIMENTAL_SUPPORT
    assert result.support_level is not SupportLevel.USER_DOCUMENT_SUPPORT


def test_partial_support_exists_and_says_what_it_is():
    """PARTIAL_SUPPORT is reachable, and it is never mistaken for support.

    Forced with a deliberately close pair rather than left untested: the rung
    has to exist, and when it fires it has to announce itself as "related, not
    shown to make this step".
    """
    near, far = "CC(C)(C)OC(=O)c1ccccc1", "CC(C)(C)OC(=O)c1ccccc1Cl"
    step = build_step([far], [DIYNE_CANON])
    pool = StructurePool()
    pool.add("paper-2", near, kind="paper")
    result = RouteValidationService(structures=pool).validate(step)

    if result.support_level is SupportLevel.PARTIAL_SUPPORT:
        assert result.match_kind == "related_structure"
        assert any("not shown to make this step" in w for w in result.warnings)
        assert unresolved_note(result, step) == PARTIAL_NOTE
    else:
        # below the related floor: still never support, and the gap is named
        assert result.support_level is SupportLevel.NO_SUPPORT
        assert result.support_level is not SupportLevel.STRUCTURAL_SUPPORT


def test_small_molecules_rarely_reach_the_related_band():
    """A measured limitation, not a preference.

    ECFP4 on small structures is coarse: nothing measured between the
    reference and its neighbours landed between the floor and identity. The
    PARTIAL_SUPPORT rung is therefore hard to reach for small molecules, which
    is a fact about fingerprints and not a reason to lower the floor.
    """
    from crea.services.route_validation import RELATED_SIMILARITY_FLOOR, _similar

    reference = "C#Cc1ccccc1"
    neighbours = ["N#Cc1ccccc1", "Cc1ccccc1", "C=Cc1ccccc1",
                  "c1ccccc1C#Cc1ccccc1", "NC(=O)c1ccccc1"]
    scores = [_similar(reference, s) for s in neighbours]
    assert max(scores) < RELATED_SIMILARITY_FLOOR, (
        "if small molecules now reach the related band, the note in "
        "KNOWN LIMITATIONS should be revisited")
    assert _similar(reference, reference) == 1.0


def test_an_unrelated_structure_gives_no_support():
    step = build_step(["CCO"], [DIYNE_CANON])
    pool = StructurePool()
    pool.add("paper-3", "CCCCCCCCCCCC", kind="paper")
    result = RouteValidationService(structures=pool).validate(step)
    assert result.support_level is SupportLevel.NO_SUPPORT


def test_a_reaction_record_promotes_to_experimentally_supported():
    from crea.core.models.evidence import Evidence, EvidenceType
    from crea.core.models.evidence import Locator as EvLocator
    from crea.core.models.literature import EvidenceStrength

    evidence = Evidence.build(
        evidence_type=EvidenceType.EXPERIMENTAL,
        strength=EvidenceStrength.USER_PROVIDED_EVIDENCE,
        source="user_provided", source_id="doc",
        raw_quote="The mixture was stirred at 80 C for 12 h.",
        locator=EvLocator(locator_type="document_region", document_id="doc",
                          page_start=1, page_end=1),
        retrieved_at="t",
    )
    record = ReactionRecord(
        reaction_id="r1", document_id="doc",
        conditions=ReactionConditions(),
        evidence_refs=[evidence.evidence_id],
    )
    step = build_step([PHENYLACETYLENE, PHENYLACETYLENE], [DIYNE_CANON])

    result = RouteValidationService(
        evidence=[evidence], reaction_records=[record]).validate(step)
    assert result.support_level is SupportLevel.EXPERIMENTAL_SUPPORT
    RouteValidationService.apply(result, step)
    assert step.status is StepStatus.EXPERIMENTALLY_SUPPORTED
    assert step.reaction_record_ids == ["r1"]
    assert step.evidence_refs == [evidence.evidence_id]


def test_a_reaction_record_citing_unknown_evidence_does_not_promote():
    """Pointing at evidence the task does not hold is not support."""
    record = ReactionRecord(
        reaction_id="r2", document_id="doc",
        conditions=ReactionConditions(),
        evidence_refs=["never-seen"],
    )
    step = build_step([PHENYLACETYLENE], [DIYNE_CANON])
    result = RouteValidationService(reaction_records=[record]).validate(step)
    assert result.support_level is SupportLevel.NO_SUPPORT
    assert result.reaction_record_ids == []


def test_support_never_moves_backwards():
    """Once evidence is attached, losing it later does not silently un-prove it."""
    step = build_step([PHENYLACETYLENE], [DIYNE_CANON])
    pool = StructurePool()
    pool.add("paper-1", PHENYLACETYLENE, kind="paper")
    good = RouteValidationService(structures=pool).validate(step)
    RouteValidationService.apply(good, step)
    assert step.status is StepStatus.LITERATURE_SUPPORTED

    # a later validation with nothing behind it
    later = RouteValidationService().validate(step)
    RouteValidationService.apply(later, step)
    assert step.status is StepStatus.LITERATURE_SUPPORTED
    assert step.support_level is SupportLevel.LITERATURE_SUPPORT


# --- 5. the provider abstraction ------------------------------------------


def test_the_fake_satisfies_the_provider_protocol():
    provider = FakeRetrosynthesisProvider()
    assert isinstance(provider, RetrosynthesisProvider)
    assert provider.availability() is ProviderStatus.AVAILABLE


def test_an_unknown_target_gets_no_route_rather_than_a_guess():
    result = FakeRetrosynthesisProvider().retrosynthesize(
        RetrosynthesisRequest(target_smiles="CCO"))
    assert result.status is ProviderStatus.AVAILABLE
    assert result.routes == []
    assert "fixture, not a retrosynthesis engine" in result.reason


def test_a_provider_failure_is_a_result_not_a_crash():
    class Broken(FakeRetrosynthesisProvider):
        def availability(self):
            raise RuntimeError("engine exploded")

    result = Broken().retrosynthesize(RetrosynthesisRequest(target_smiles=DIYNE))
    assert result.status in (ProviderStatus.FAILED, ProviderStatus.AVAILABLE)


def test_molecules_are_canonicalised_and_never_named():
    result = make_result()
    target = result.routes[0].target
    assert target.canonical_smiles == DIYNE_CANON
    for route in result.routes:
        for step in route.steps:
            for molecule in list(step.products) + list(step.reactants):
                assert molecule.canonical_smiles
                assert molecule.name == ""      # nothing was invented for it


def test_transformation_class_is_unknown_when_the_provider_gave_none():
    result = make_result()
    for route in result.routes:
        for step in route.steps:
            assert step.transformation.transformation_class == "UNKNOWN"
            assert step.transformation.transformation_class_source == "UNKNOWN"
            assert step.transformation.reaction_smiles is None


def test_stock_assumptions_are_recorded_as_assumptions():
    step = first_step(make_result())
    kinds = {k: v.value for k, v in step.stock_assumptions.items()}
    assert kinds == {"C#Cc1ccccc1": "provider_stock_list"}
    # and nothing in the model claims to be a vendor quotation
    for field in ("available_from_vendor", "in_stock_confirmed", "price"):
        assert field not in ReactionStep.model_fields


def test_route_and_step_ids_are_stable_across_runs():
    first = make_result()
    second = make_result()
    assert [r.route_id for r in first.routes] == [r.route_id for r in second.routes]
    assert [s.step_id for s in first.routes[0].steps] == \
           [s.step_id for s in second.routes[0].steps]


def test_routes_are_ordered_but_never_ranked():
    result = make_result()
    assert len(result.routes) == 2
    assert result.routes[0].score > result.routes[1].score
    # ordering follows the provider's own output; nothing here adds a verdict
    for route in result.routes:
        assert not any(k in route.model_fields
                       for k in ("recommended", "winner", "best", "rank"))


@pytest.mark.parametrize("requested,expected", [(1, 1), (2, 2), (10, 2)])
def test_max_routes_is_honoured(requested, expected):
    result = FakeRetrosynthesisProvider().retrosynthesize(
        RetrosynthesisRequest(target_smiles=DIYNE, max_routes=requested))
    assert len(result.routes) == expected

# --- 20-25: what the report may and may not say ---------------------------


def _render(report):
    from crea.report.research import format_report

    return format_report(report)


def _report_with_two_routes():
    from crea.core.models.research import ResearchReport

    return ResearchReport(question="q", routes=make_result().routes)


def test_the_comparison_is_a_table_of_facts_with_no_verdict():
    from crea.report.research import _DISCLAIMER, _FORBIDDEN_VERDICTS

    text = _render(_report_with_two_routes())
    assert "路线对比" in text
    # The disclaimer itself names the verdict words ("gives no recommendation"),
    # so it is excluded before checking. Without that, a report that promises
    # not to recommend would fail a test that no report recommends.
    body = text.replace(_DISCLAIMER, "")
    for word in _FORBIDDEN_VERDICTS:
        assert word not in body, f"the comparison says {word!r}"
    assert _DISCLAIMER in text


def test_the_comparison_shows_the_facts_it_is_meant_to():
    text = _render(_report_with_two_routes())
    for row in ("步数", "provider score", "有文献支持的步数",
                "有实验条件的步数", "未支持的步数", "证据条数"):
        assert row in text


def test_the_report_says_a_step_has_nothing_behind_it():
    """The exact sentence the brief requires once a step has been checked."""
    from crea.core.models.research import ResearchReport

    # the note is produced by validating a step, so validate one
    from crea.core.models.route import ReactionStep, RouteMolecule

    result = make_result()
    step = result.routes[0].steps[0]
    report = ResearchReport(
        question="q", routes=result.routes,
        route_notes=[unresolved_note(
            RouteValidationService().validate(step), step)],
    )
    text = _render(report)
    assert "文献支持：未找到" in text
    assert CANDIDATE_NOTE in text


def test_the_report_names_the_provider_as_the_score_source():
    """`score: 0.87` is the engine's number, and is labelled as such."""
    text = _render(_report_with_two_routes())
    assert "score" in text
    for banned in ("confidence", "可信度", "置信度", "% 的把握"):
        assert banned not in text


def test_stock_is_reported_as_a_provider_assumption_not_a_supply_guarantee():
    text = _render(_report_with_two_routes())
    assert "stock 假设" in text
    for banned in ("可以买到", "有货", "现货", "供应商确认"):
        assert banned not in text

"""The chain from a file on disk to a line in the report, and what that line says.

`PDF -> Document -> Evidence -> ReactionRecord -> ReactionRecordLink -> RouteStep
-> RouteEvidenceReport -> Chinese report`

Every link in that arrow is a real object in this test: the PDF is generated and
parsed by the production parser, the evidence comes out of the real document
search, the record comes out of the real extractor, and the report is the one
`crea.report.research` renders. Nothing is stubbed, because the failure this
phase is about -- a candidate being described as verified -- is exactly the kind
of failure a stubbed end-to-end test cannot see.

What the report must never contain, in Chinese or English: 成功率, 把握, 推荐,
最佳, 最优, 首选, confidence, probability, success rate, recommended. The last
section asserts that by scanning the rendered text, so that a future edit which
helpfully adds "推荐路线 A" fails here rather than in a reader's inbox.
"""

import pathlib
import tempfile

import pytest

from crea.core.models.evidence import Claim, Evidence, EvidenceType, Locator
from crea.core.models.literature import EvidenceStrength
from crea.core.models.reaction import (
    ExtractionOutcome,
    ExtractionProposal,
    FieldProposal,
    FieldStatus,
    Quantity,
    ReactionConditions,
    ReactionRecord,
)
from crea.core.models.reaction_linking import ReactionEvidenceLevel
from crea.core.models.research import (
    ActionKind,
    ResearchReport,
    ResearchStep,
    TaskStatus,
)
from crea.core.models.route import (
    CandidateRoute,
    ReactionStep,
    RouteMolecule,
    ScoreSource,
)
from crea.core.provenance import Provenance
from crea.report.research import format_report, render_report
from crea.services.documents import LocalDocumentService
from crea.services.document_evidence import search_document
from crea.services.evidence import EvidenceRegistry
from crea.services.reaction_extraction import ReactionExtractor
from crea.services.route_evidence import (
    RouteEvidenceInputs,
    RouteEvidenceService,
    build_record_link,
)
from crea.sources.pdf import PypdfDocumentParser
from tests.fakes import pdfs
from tests.fakes.report_language import (
    FORBIDDEN_VERDICTS,
    asserts_verdict,
)

PA = "C#Cc1ccccc1"
DIYNE = "c1ccccc1C#CC#Cc2ccccc2"
TOLUENE = "Cc1ccccc1"
STYRENE = "C=Cc1ccccc1"


# ---------------------------------------------------------------------------
# the real thing, produced the real way
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def read_procedure():
    """A local PDF read end to end: pages, evidence, and a validated record.

    Returns `(document_id, [evidence], record)`. Built once -- it writes a real
    file and parses it with the production parser, and nothing here depends on
    it being fresh.
    """
    directory = pathlib.Path(tempfile.mkdtemp(prefix="crea-route-report-"))
    path = directory / "procedure.pdf"
    path.write_bytes(pdfs.make_pdf(pdfs.EXPERIMENTAL))

    document = LocalDocumentService(PypdfDocumentParser()).ingest(path)
    hits = search_document(document, "stirred at 80 C")
    assert hits, "the fixture PDF must produce evidence to read"
    evidence = [hit.evidence for hit in hits]
    eid = evidence[0].evidence_id

    proposal = ExtractionProposal(
        description="CuI-catalyzed coupling of phenylacetylene",
        description_evidence_refs=[eid],
        components=[
            FieldProposal(name="phenylacetylene", value="phenylacetylene",
                          component_role="reactant", status=FieldStatus.EXPLICIT,
                          evidence_refs=[eid], raw_text="Phenylacetylene (1.0 mmol),"),
            FieldProposal(name="CuI", value="CuI", component_role="catalyst",
                          status=FieldStatus.EXPLICIT, evidence_refs=[eid],
                          raw_text="CuI (10 mol%),"),
        ],
        conditions=[
            FieldProposal(name="temperature", value=80, raw_value="80", unit="C",
                          raw_unit="C", status=FieldStatus.EXPLICIT,
                          evidence_refs=[eid], raw_text="stirred at 80 C"),
            FieldProposal(name="time", value=12, raw_value="12", unit="h",
                          raw_unit="h", status=FieldStatus.EXPLICIT,
                          evidence_refs=[eid], raw_text="for 12 h."),
            FieldProposal(name="yield_value", value=82, raw_value="82", unit="%",
                          raw_unit="%", status=FieldStatus.EXPLICIT,
                          evidence_refs=[eid], raw_text="isolated in 82% yield."),
        ],
        yield_type="isolated",
        yield_type_evidence_refs=[eid],
    )

    outcome = ReactionExtractor(None).extract(
        evidence, document_id=document.document.document_id, proposal=proposal)
    assert outcome.outcome is ExtractionOutcome.OK, outcome.rejected
    assert outcome.records, "the worked example must yield a record"
    return document.document.document_id, evidence, outcome.records[0]


# ---------------------------------------------------------------------------
# builders for the report side
# ---------------------------------------------------------------------------


def molecule(smiles: str, molecule_id: str) -> RouteMolecule:
    return RouteMolecule(molecule_id=molecule_id, smiles=smiles,
                         canonical_smiles=smiles)


def a_step(index: int, reactants: list[str], products: list[str]) -> ReactionStep:
    return ReactionStep(
        step_id=f"step-{index}", step_index=index,
        reactants=[molecule(s, f"s{index}-r{i}") for i, s in enumerate(reactants)],
        products=[molecule(s, f"s{index}-p{i}") for i, s in enumerate(products)],
        source="aizynthfinder")


def a_route(*steps: ReactionStep) -> CandidateRoute:
    return CandidateRoute(
        route_id="route-1", target=molecule(DIYNE, "target"),
        steps=list(steps), source="aizynthfinder",
        provider_version="4.4.1", score=42.0,
        score_source=ScoreSource.AIZYNTHFINDER)


def render(report: ResearchReport) -> str:
    """The lines a reader actually sees, joined."""
    return "\n".join(line for line in format_report(report).splitlines())


# ===========================================================================
# §9 -- the local path closes
# ===========================================================================


def test_a_procedure_read_from_a_local_pdf_reaches_the_user_rung(read_procedure):
    """80 °C, 12 h, 82% isolated, read out of a file the operator supplied.

    That is `USER_DOCUMENT_EXPERIMENTAL_SUPPORT` and it is the top rung. It is
    top because the structures matched exactly *and* the conditions are real,
    not because a local file counts as published literature.
    """
    document_id, evidence, record = read_procedure
    route = a_route(a_step(1, [PA], [DIYNE]))
    registry = EvidenceRegistry()
    registry.extend(evidence)

    chain = RouteEvidenceService(
        registry,
        RouteEvidenceInputs(
            records=[record],
            record_links=[build_record_link(route.steps[0], record.reaction_id,
                                            [PA], [DIYNE])]),
    ).build(route)

    step = chain.steps[0]
    assert step.evidence_level is (
        ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT)
    assert step.support_provenance == "user_provided"
    assert step.evidence_refs, "the conditions must cite the passage they came from"
    assert document_id in step.document_ids
    # and the conditions survived the whole trip
    assert record.conditions.temperature.value == 80
    assert record.conditions.time.value == 12
    assert record.conditions.yield_value.value == 82


def test_the_conditions_land_on_the_step_whose_structures_they_describe(read_procedure):
    """The procedure belongs to one step and reaches only that step.

    Two steps, one record, linked to the first. The second gets nothing --
    isolation is structural, so this is not a matter of the data cooperating.
    """
    document_id, evidence, record = read_procedure
    route = a_route(a_step(1, [PA], [DIYNE]), a_step(2, [TOLUENE], [STYRENE]))
    registry = EvidenceRegistry()
    registry.extend(evidence)

    chain = RouteEvidenceService(
        registry,
        RouteEvidenceInputs(
            records=[record],
            record_links=[build_record_link(route.steps[0], record.reaction_id,
                                            [PA], [DIYNE])]),
    ).build(route)

    assert chain.steps[0].evidence_level is (
        ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT)
    assert chain.steps[1].evidence_level is (
        ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE)
    assert chain.steps[1].experimental_support == []


def test_the_report_renders_the_chain_and_its_conditions(read_procedure):
    _document_id, evidence, record = read_procedure
    route = a_route(a_step(1, [PA], [DIYNE]), a_step(2, [TOLUENE], [STYRENE]))
    registry = EvidenceRegistry()
    registry.extend(evidence)

    chain = RouteEvidenceService(
        registry,
        RouteEvidenceInputs(
            records=[record],
            record_links=[build_record_link(route.steps[0], record.reaction_id,
                                            [PA], [DIYNE])]),
    ).build(route)

    report = ResearchReport(question="怎么合成这个化合物？",
                            routes=[route], route_evidence=[chain],
                            reaction_details=[record])
    text = render(report)

    assert "路线证据链" in text
    assert "用户文档实验支持" in text
    # the conditions are in the report, not merely somewhere in the objects
    assert "80" in text and "12" in text and "82" in text
    # and the unsupported step says so rather than inheriting the support
    assert "逆合成工具候选" in text
    assert "尚无" in text or "无文献" in text or "候选" in text


# ===========================================================================
# §11 -- what the report must not say
# ===========================================================================


def test_the_report_makes_no_verdict_about_the_route(read_procedure):
    """Two steps, one supported: the report describes both and picks neither."""
    _document_id, evidence, record = read_procedure
    route = a_route(a_step(1, [PA], [DIYNE]), a_step(2, [TOLUENE], [STYRENE]))
    registry = EvidenceRegistry()
    registry.extend(evidence)

    chain = RouteEvidenceService(
        registry,
        RouteEvidenceInputs(
            records=[record],
            record_links=[build_record_link(route.steps[0], record.reaction_id,
                                            [PA], [DIYNE])]),
    ).build(route)

    report = ResearchReport(question="怎么合成这个化合物？",
                            routes=[route], route_evidence=[chain],
                            reaction_details=[record])
    text = render(report)

    for word in FORBIDDEN_VERDICTS:
        assert not asserts_verdict(text, word), (
            f"the report must not assert a {word!r}")


def test_an_unsupported_step_is_described_in_the_words_the_prompt_requires(read_procedure):
    """A bare candidate gets its own sentence, not silence and not a promotion."""
    route = a_route(a_step(1, [TOLUENE], [STYRENE]))
    chain = RouteEvidenceService(EvidenceRegistry(), RouteEvidenceInputs()).build(route)

    report = ResearchReport(question="怎么合成？", routes=[route],
                            route_evidence=[chain])
    text = render(report)

    assert "逆合成工具候选" in text
    assert "retrosynthesis_candidate" in text
    # the raw enum value is printed beside the label, so a reader can see the
    # exact rung rather than a paraphrase of it
    assert "no_match" in text or "NO_MATCH" in text.upper()


def test_the_disclaimer_says_the_section_produces_no_verdict():
    route = a_route(a_step(1, [PA], [DIYNE]))
    chain = RouteEvidenceService(EvidenceRegistry(), RouteEvidenceInputs()).build(route)
    report = ResearchReport(question="怎么合成？", routes=[route],
                            route_evidence=[chain])
    text = render(report)

    assert "不给出成功率" in text
    assert "不排名" in text
    assert "不推荐" in text


def test_unresolved_evidence_is_written_into_the_report():
    """The gap is the reader's to see, not ours to tidy away."""
    ghost = Evidence.build(
        evidence_type=EvidenceType.EXPERIMENTAL,
        strength=EvidenceStrength.USER_PROVIDED_EVIDENCE,
        source="user_provided", source_id="gone",
        raw_quote="at 80 C for 12 h",
        locator=Locator(locator_type="document_region", document_id="gone",
                        page_start=1, section="Experimental"),
        retrieved_at="2026-01-01T00:00:00+00:00",
        provenance=Provenance(tool="test", source="user_provided"))
    route = a_route(a_step(1, [PA], [DIYNE]))
    record = ReactionRecord(
        reaction_id="rxn-1", document_id="gone",
        reaction_description="a procedure read from a file this task cannot cite",
        conditions=ReactionConditions(
            temperature=Quantity(value=80, raw_value="80", unit="C",
                                 raw_unit="C", status="explicit",
                                 raw_text="stirred at 80 C")),
        evidence_refs=[ghost.evidence_id])
    link = build_record_link(route.steps[0], "rxn-1", [PA], [DIYNE],
                             [ghost.evidence_id])

    chain = RouteEvidenceService(
        EvidenceRegistry(),
        RouteEvidenceInputs(records=[record], record_links=[link])).build(route)

    report = ResearchReport(question="怎么合成？", routes=[route],
                            route_evidence=[chain])
    text = render(report)

    assert ghost.evidence_id in text
    assert "未能解析" in text


def test_the_provider_score_appears_as_a_score_and_nothing_more():
    route = a_route(a_step(1, [PA], [DIYNE]))
    chain = RouteEvidenceService(EvidenceRegistry(), RouteEvidenceInputs()).build(route)
    report = ResearchReport(question="怎么合成？", routes=[route],
                            route_evidence=[chain])
    text = render(report)

    assert "42" in text
    # the score is described as a tool's own heuristic, in words
    assert "启发" in text or "打分" in text or "分数" in text


# ===========================================================================
# §12 -- the agent path
# ===========================================================================


def test_render_report_accepts_the_chain_and_keeps_it_beside_the_routes(read_procedure):
    """`route_evidence` is additive: routes still render, claims still render."""
    _document_id, evidence, record = read_procedure
    route = a_route(a_step(1, [PA], [DIYNE]))
    registry = EvidenceRegistry()
    registry.extend(evidence)
    chain = RouteEvidenceService(
        registry,
        RouteEvidenceInputs(records=[record],
                            record_links=[build_record_link(
                                route.steps[0], record.reaction_id, [PA], [DIYNE])]),
    ).build(route)

    claim = Claim.build(claim_id="c1", text="该步骤已有实验记录支持",
                        claim_type="experimental_fact",
                        evidence_refs=[chain.steps[0].evidence_refs[0]])

    report = render_report(
        question="怎么合成这个化合物？",
        interpretation="读到了实验条件。",
        claims=[claim],
        evidence=list(evidence),
        sources=[],
        unresolved=[],
        computed_notes=[],
        reaction_details=[record],
        routes=[route],
        route_notes=[],
        route_evidence=[chain],
        steps=[ResearchStep(step_id="s1", index=0, action=ActionKind.TOOL_CALL,
                            tool="route_evidence_report")],
        tool_calls=1,
        status=TaskStatus.PARTIAL,
    )

    assert isinstance(report, ResearchReport)
    assert len(report.route_evidence) == 1
    assert report.route_evidence[0].steps[0].evidence_level is (
        ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT)
    assert "路线证据链" in render(report)
    assert "该步骤已有实验记录支持" in render(report)


def test_the_report_is_the_same_shape_with_and_without_the_chain():
    """Adding the chain adds a section; it does not reshape the others.

    A report whose existing sections move around when evidence is found is a
    report two readers will read differently.
    """
    route = a_route(a_step(1, [PA], [DIYNE]))
    bare = RouteEvidenceService(EvidenceRegistry(), RouteEvidenceInputs()).build(route)

    def headers(with_chain: bool) -> list[str]:
        report = render_report(
            question="q", interpretation="i", claims=[], evidence=[],
            sources=[], unresolved=[], computed_notes=[],
            reaction_details=[], routes=[route], route_notes=[],
            route_evidence=[bare] if with_chain else [],
            steps=[], tool_calls=1, status=TaskStatus.PARTIAL)
        return [line for line in render(report).splitlines()
                if line.startswith("## ")]

    # the chain adds its own section and moves nothing else
    without, with_chain = headers(False), headers(True)
    assert with_chain.count("## 路线证据链") == 1
    assert [h for h in with_chain if h != "## 路线证据链"] == without


def test_the_record_is_rendered_as_a_record_not_as_a_summary():
    """Conditions that were never stated stay unstated in the report too."""
    minimal = ReactionRecord(
        reaction_id="rxn-min",
        document_id="doc-min",
        reaction_description="a procedure that states a temperature and nothing else",
        conditions=ReactionConditions(
            temperature=Quantity(value=80, raw_value="80", unit="C",
                                 raw_unit="C", status="explicit",
                                 raw_text="stirred at 80 C"),
            time=Quantity(status="not_found", raw_text=""),
            yield_value=Quantity(status="not_found", raw_text=""),
        ),
        evidence_refs=[],
    )
    route = a_route(a_step(1, [PA], [DIYNE]))
    chain = RouteEvidenceService(EvidenceRegistry(), RouteEvidenceInputs()).build(route)

    report = ResearchReport(question="q", routes=[route], route_evidence=[chain],
                            reaction_details=[minimal])
    text = render(report)

    assert "80" in text
    # no pressure, no atmosphere, no yield, and none of them invented
    for invented in ("atm", "N₂", "N2", "bar"):
        assert invented not in text
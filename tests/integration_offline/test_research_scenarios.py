"""Research scenario acceptance: two runs, one honest shape each.

Phase 3.7 §7, Scenarios A and B. Not unit tests of a component -- two
research tasks driven the way a user drives them, through the real agent
loop in workflow mode, the real tools, and the offline fixture stack.

**Scenario A** is the complete chain: an explicit structure, identity
resolved from it, computed analysis, literature discovery, evidence from a
user document, candidate routes, validation, report. It must end COMPLETED
-- and nothing in it may be stronger than what produced it.

**Scenario B** is the shape this project exists for: a candidate route with
nothing behind it. The route is real -- a tool proposed it -- but no
literature and no experimental record supports it. It must end PARTIAL with
the route kept as candidates, never promoted, never described as
recommended.

The rule both scenarios protect:

    candidate route is not evidence

Each scenario builds its own stack (registry, evidence sink, document
library, workbench) and its own session. Nothing mutable is shared between
them: no common EvidenceRegistry, no common findings, no common route state.

Everything is offline. No network, no API key, no model download.
"""

from __future__ import annotations

from crea.agent.loop import ResearchAgent
from crea.agent.offline import build_offline_workflow_stack
from crea.core.models.evidence import EvidenceStrength
from crea.core.models.reaction_linking import (
    EVIDENCE_LEVEL_ORDER,
    ReactionEvidenceLevel,
    ReactionMatchType,
)
from crea.core.models.reaction import FieldStatus
from crea.core.models.research import (
    ActionKind,
    AgentAction,
    ResearchTask,
    TaskStatus,
    ToolStatus,
)
from crea.core.models.route import StepStatus
from crea.core.models.session import WorkflowStatus
from crea.llm.fake import FakeLLMProvider
from crea.report.research import render_research_report
from tests.fakes import pdfs
from tests.fakes.report_language import FORBIDDEN_VERDICTS, asserts_verdict

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"
QUESTION = "分析 1,4-diphenyl-1,3-diyne 的性质、文献、合成方法和实验依据"


# ---------------------------------------------------------------------------
# harness: one independent stack per scenario
# ---------------------------------------------------------------------------


def _stack(tmp_path, *, with_pdf: bool):
    """An offline stack of its own: registry, library, evidence sink.

    Built per test from `build_offline_workflow_stack` -- the same fixture
    literature, fake retrosynthesis and real tools the rest of the phase
    uses. Nothing here is shared with another test's stack.
    """
    _workflow, library, evidence_sink, registry = build_offline_workflow_stack()
    document_id = None
    if with_pdf:
        path = tmp_path / "procedure.pdf"
        path.write_bytes(pdfs.make_pdf(pdfs.EXPERIMENTAL))
        document_id = library.ingest(path).document.document_id
    return registry, library, evidence_sink, document_id


def _route_step_ids(registry) -> list[str]:
    """The step ids the fake provider proposes for the demo target.

    Read from the provider's own answer, never written by hand -- a
    fabricated step id would make the script validate a step that does not
    exist, and the error would teach the wrong lesson.
    """
    result = registry.execute("retrosynthesize", {"target_smiles": DIYNE})
    assert result.status is ToolStatus.OK
    return [s["step_id"] for r in result.data["routes"] for s in r["steps"]]


def _assert_no_verdict_language(markdown: str) -> None:
    """No line of the report may assert a verdict. Refusals are allowed:
    the report may name the concepts in order to decline them."""
    for word in FORBIDDEN_VERDICTS:
        assert not asserts_verdict(markdown, word), (
            f"report asserts the verdict {word!r}")


def _assert_refs_resolve(report) -> None:
    """Every evidence reference in a finding points at evidence we hold."""
    held = {e.evidence_id for e in report.evidence}
    for finding in report.findings:
        for ref in finding.evidence_refs:
            assert ref in held, f"finding cites unheld evidence {ref}"
    assert report.unsupported_findings() == []


# ---------------------------------------------------------------------------
# Scenario A -- the complete offline research chain
# ---------------------------------------------------------------------------


def test_scenario_A_complete_offline_research_chain(tmp_path):
    """explicit structure → identity → analysis → literature → evidence →
    routes → validation → report → COMPLETED.

    Every step of the chain is driven by a tool; the scripted model only
    names which tool to call. What the report asserts is what the tools
    produced.
    """
    registry, library, sink, document_id = _stack(tmp_path, with_pdf=True)
    step_ids = _route_step_ids(registry)
    assert step_ids, "the fixture must propose routes for the demo target"

    script = [
        # explicit structure: the user's SMILES, not a name guessed into one
        AgentAction(action=ActionKind.TOOL_CALL, tool="resolve_chemical_identity",
                    arguments={"kind": "SMILES", "value": DIYNE}),
        AgentAction(action=ActionKind.TOOL_CALL, tool="analyze_molecule",
                    arguments={"smiles": DIYNE}),
        AgentAction(action=ActionKind.TOOL_CALL, tool="search_literature",
                    arguments={"query": "1,4-diphenyl-1,3-diyne synthesis",
                               "limit": 5}),
        AgentAction(action=ActionKind.TOOL_CALL, tool="get_document_evidence",
                    arguments={"document_id": document_id, "query": "CuI",
                               "max_evidence": 3}),
        AgentAction(action=ActionKind.TOOL_CALL, tool="retrosynthesize",
                    arguments={"target_smiles": DIYNE}),
    ]
    script += [
        AgentAction(action=ActionKind.TOOL_CALL, tool="validate_route_step",
                    arguments={"step_id": sid}) for sid in step_ids
    ]
    script += [
        AgentAction(action=ActionKind.TOOL_CALL, tool="route_evidence_report",
                    arguments={}),
        AgentAction(
            action=ActionKind.FINAL,
            summary="结构、计算、文献检索、文档证据与候选路线均已收集。",
            claim_specs=[{
                "text": "本地文档实验部分记录该反应用 CuI 催化，"
                        "80 C 搅拌 12 小时，分离收率 82%。",
                "claim_type": "experimental_fact",
                "from_tool": "get_document_evidence",
            }],
        ),
    ]

    agent = ResearchAgent(llm=FakeLLMProvider(script), registry=registry,
                          documents=library, evidence=sink)
    task = agent.run(QUESTION, workflow=True)

    # -- the run completed, and the session agrees -------------------------
    assert task.status is TaskStatus.COMPLETED
    assert agent.session is not None
    assert agent.session.status is WorkflowStatus.COMPLETED

    # -- identity resolved from the explicit structure ---------------------
    resolution = registry.identity.resolution
    assert resolution is not None
    assert resolution.is_resolved
    assert resolution.input.value == DIYNE
    assert resolution.selected is not None
    assert resolution.selected.canonical_smiles

    # -- computed findings are labelled computed ---------------------------
    report = task.report
    computed = [f for f in report.findings if f.kind == "computed"]
    assert computed
    assert all(f.kind == "computed" for f in computed)

    # -- literature discovery and evidence both present -------------------
    assert report.sources, "the fixture literature search must find the demo paper"
    assert report.evidence, "the user document must yield evidence"
    backed = [f for f in report.findings if f.kind == "evidence_backed"]
    assert backed, "the claim spec must resolve to an evidence-backed finding"

    # -- evidence references are real -------------------------------------
    _assert_refs_resolve(report)
    for item in report.evidence:
        assert item.evidence_id
        assert item.raw_quote

    # -- routes exist and are honestly labelled ---------------------------
    assert report.routes
    assert all(step.status in (StepStatus.CANDIDATE,
                               StepStatus.LITERATURE_SUPPORTED,
                               StepStatus.EXPERIMENTALLY_SUPPORTED)
               for r in report.routes for step in r.steps)

    # -- the model layer carries no verdict fields ------------------------
    # `CandidateRoute.score` is deliberately kept: it is the provider's own
    # search heuristic, named as one (Phase 3.5). What is forbidden is a
    # confidence, a probability or a ranking -- a score is none of those.
    from crea.core.models.evidence import Evidence, EvidenceType  # noqa: F401
    from crea.core.models.research import Finding, ResearchReport
    from crea.core.models.route import CandidateRoute

    for model in (Finding, Evidence, CandidateRoute, ResearchReport):
        fields = set(model.model_fields)
        for forbidden in ("confidence", "probability", "certainty", "ranking",
                          "success_rate", "recommendation"):
            assert forbidden not in fields, f"{model.__name__} grew {forbidden}"

    # -- the report renders and speaks without verdicts -------------------
    markdown = render_research_report(report, session=agent.session,
                                      identity=resolution)
    assert "# Research Report" in markdown
    assert "## 1. Research Task" in markdown
    assert "## 2. Chemical Identity" in markdown
    assert "## 3. Molecular Analysis" in markdown
    assert "resolution status: resolved" in markdown
    _assert_no_verdict_language(markdown)


# ---------------------------------------------------------------------------
# Scenario B -- candidate routes with nothing behind them
# ---------------------------------------------------------------------------


def test_scenario_B_candidate_route_is_not_evidence(tmp_path):
    """target → candidate route → no supporting evidence → PARTIAL.

    The fixture retrosynthesis proposes real candidate routes for the demo
    target, and nothing else: no document evidence, no reaction record, no
    literature match establishes those steps. The run must keep the routes,
    keep every step at CANDIDATE, and say PARTIAL -- a route that exists is
    not a route that is supported.
    """
    registry, library, sink, _document_id = _stack(tmp_path, with_pdf=False)
    step_ids = _route_step_ids(registry)
    assert step_ids, "the fixture must propose routes for the demo target"

    script = [
        AgentAction(action=ActionKind.TOOL_CALL, tool="retrosynthesize",
                    arguments={"target_smiles": DIYNE}),
    ]
    script += [
        AgentAction(action=ActionKind.TOOL_CALL, tool="validate_route_step",
                    arguments={"step_id": sid}) for sid in step_ids
    ]
    script += [
        AgentAction(action=ActionKind.TOOL_CALL, tool="route_evidence_report",
                    arguments={}),
        # Final with no claims: there is nothing the evidence establishes.
        AgentAction(action=ActionKind.FINAL,
                    summary="候选路线已逐步检查，未找到支持证据。"),
    ]

    agent = ResearchAgent(llm=FakeLLMProvider(script), registry=registry,
                          documents=library, evidence=sink)
    task = agent.run(QUESTION, workflow=True)

    # -- the run is PARTIAL: routes were proposed, nothing was established -
    assert task.status is TaskStatus.PARTIAL
    assert agent.session is not None
    assert agent.session.status is WorkflowStatus.PARTIAL

    report = task.report

    # -- the routes are kept ----------------------------------------------
    assert report.routes
    step_ids_seen = [s.step_id for r in report.routes for s in r.steps]
    assert step_ids_seen

    # -- every step is still a candidate ---------------------------------
    for route in report.routes:
        for step in route.steps:
            assert step.status is StepStatus.CANDIDATE, (
                f"step {step.step_id} was promoted to {step.status}")
            assert step.status is not StepStatus.EXPERIMENTALLY_SUPPORTED
            assert step.reaction_record_ids == []

    # -- the evidence chain did not climb ---------------------------------
    EXPERIMENTAL_RUNGS = (
        ReactionEvidenceLevel.EXPERIMENTAL_REACTION_SUPPORT,
        ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT,
    )
    assert report.route_evidence, "the chain report must be generated"
    for chain in report.route_evidence:
        for chain_step in chain.steps:
            assert chain_step.candidate_status is StepStatus.CANDIDATE
            assert chain_step.evidence_level not in EXPERIMENTAL_RUNGS
            assert chain_step.experimental_support == []
            assert EVIDENCE_LEVEL_ORDER[chain_step.evidence_level] <= (
                EVIDENCE_LEVEL_ORDER[ReactionEvidenceLevel.LITERATURE_DISCOVERY])

    # -- no claims were invented ------------------------------------------
    _assert_refs_resolve(report)
    assert report.findings == [] or all(
        f.kind in ("computed", "insufficient") for f in report.findings)

    # -- the report keeps the candidate route and refuses verdicts --------
    markdown = render_research_report(report, session=agent.session,
                                      identity=registry.identity.resolution)
    assert "# Research Report" in markdown
    assert "Candidate only" in markdown
    assert "Status: PARTIAL" in markdown
    _assert_no_verdict_language(markdown)


# ---------------------------------------------------------------------------
# isolation between the two scenarios
# ---------------------------------------------------------------------------


def test_the_two_scenarios_share_no_state(tmp_path):
    """A scenario must not inherit evidence, routes or a session from the
    other. Two stacks, two runs, disjoint stores."""
    registry_a, library_a, sink_a, _doc_a = _stack(tmp_path, with_pdf=True)
    registry_b, library_b, sink_b, _doc_b = _stack(tmp_path, with_pdf=False)

    # distinct registries, sinks and workbenches
    assert registry_a is not registry_b
    assert sink_a is not sink_b
    assert registry_a.routes is not registry_b.routes
    assert registry_a.identity is not registry_b.identity

    # running one must not populate the other
    doc_a = library_a.ids()[0]
    agent_a = ResearchAgent(llm=FakeLLMProvider([
        AgentAction(action=ActionKind.TOOL_CALL, tool="resolve_chemical_identity",
                    arguments={"kind": "SMILES", "value": DIYNE}),
        AgentAction(action=ActionKind.TOOL_CALL, tool="get_document_evidence",
                    arguments={"document_id": doc_a, "query": "CuI",
                               "max_evidence": 2}),
        AgentAction(action=ActionKind.FINAL, summary="a", claim_specs=[{
            "text": "本地文档实验部分记录该反应用 CuI 催化。",
            "claim_type": "experimental_fact",
            "from_tool": "get_document_evidence",
        }]),
    ]), registry=registry_a, documents=library_a, evidence=sink_a)
    task_a = agent_a.run(QUESTION, workflow=True)

    assert task_a.status is TaskStatus.COMPLETED
    assert agent_a.session is not None
    assert registry_a.identity.resolution is not None  # A resolved its own
    assert sink_b == [], "B's evidence sink must stay empty of A's evidence"
    assert registry_b.routes.routes == [], "B's route state must stay empty"
    assert registry_b.identity.resolution is None


# ---------------------------------------------------------------------------
# Scenario C -- a question with no structure is never guessed into one
# ---------------------------------------------------------------------------


def test_scenario_C_no_structure_input_never_guesses_a_structure():
    """question "Analyze phenylacetylene", structure_input=None →
    Identity unresolved → no guessed structure → PARTIAL, and **no**
    literature discovery.

    The user handed over a sentence, not a structure. Every path that could
    invent one -- a name guessed into a SMILES, a SMILES guessed into an
    InChI, analysis run on whichever structure looked plausible -- has to
    stay shut. And (CREA V1.1 fix A/B) discovery is anchored to a chemical
    object, so with none established the run does not push the question
    sentence into a provider: it records "input required" and performs no
    search. The run ends PARTIAL because the research did not happen, and
    the report says exactly that.
    """
    # The workflow is handed its own stack, sink included: a second sink
    # would render a report disagreeing with what the tools produced.
    workflow, _library, _sink, registry = build_offline_workflow_stack()
    task = ResearchTask(task_id="scenario-c", question="Analyze phenylacetylene")

    # No StructureInput is built. The question goes in as a question.
    report = workflow.run(task, structure_input=None)

    # -- 4. the run is PARTIAL, and did not crash --------------------------
    assert task.status is TaskStatus.PARTIAL
    assert report.status is TaskStatus.PARTIAL

    # -- 1-2. identity unresolved, and no structure was derived -----------
    # The strongest form of the no-guess check: no resolution object exists
    # at all -- no candidate, no selected structure, no source that could
    # pass off a guess as an establishment. (Banning the string
    # "C#Cc1ccccc1" would only prove that *this* guess is absent.)
    resolution = registry.identity.resolution
    assert resolution is None, "nothing was looked up; no resolution may be held"
    assert not any(f.kind == "computed" for f in report.findings), (
        "no computed finding may come from a structure nobody supplied")
    # tool trace: the structure-dependent tools were skipped, not run
    by_tool = {s.tool: s for s in task.steps}
    for tool in ("analyze_molecule", "retrosynthesize",
                 "validate_route_step", "route_evidence_report"):
        step = by_tool.get(tool)
        assert step is not None, f"{tool} must appear in the trace as a skip"
        assert step.result_status is None, f"{tool} must not have run"
        assert step.note.startswith("skipped"), step.note

    # -- 3. chemical analysis never ran ------------------------------------
    assert "analyze_molecule" not in [
        s.tool for s in task.steps if s.result_status is not None]
    assert report.reaction_details == []
    assert report.routes == []

    # -- 6. discovery is NOT run without a chemical object (V1.1 fix A/B) --
    # The question sentence is never used as a search term, so no discovery
    # lands -- and no paper that merely shares words with the question can be
    # dressed up as a relevant result.
    assert report.sources == [], (
        "literature discovery must not run without a resolved chemical object")
    assert report.outcome == "input_required"
    lit = [s for s in task.steps if s.tool == "search_literature"]
    assert lit and lit[0].result_status is None, "search_literature was skipped, not run"
    assert "identity unresolved" in lit[0].note

    # -- 5. the report says all of this in the mandated shape --------------
    markdown = render_research_report(report, identity=resolution)
    assert "# Research Report" in markdown
    identity_section = markdown.split("## 2. Chemical Identity")[1].split("## 3.")[0]
    assert "Chemical Identity" in markdown
    assert "Identity unresolved" in identity_section
    assert "Status: PARTIAL" in markdown
    # no structure is rendered anywhere in the identity section
    assert "SMILES:" not in identity_section
    assert "InChI:" not in identity_section
    assert "InChIKey:" not in identity_section
    assert "resolution status: resolved" not in markdown
    _assert_no_verdict_language(markdown)


def test_scenario_C_agent_workflow_mode_also_refuses_to_guess(tmp_path):
    """The same rule through the agent's workflow mode: a scripted model
    that only searches the literature -- it is never handed a structure and
    never invents one -- ends with a PARTIAL session and an unresolved
    identity.

    The scenario is the realistic classroom case: "研究一下苯乙炔的合成方法"
    and no SMILES. The honest answer is a request for structure confirmation,
    not a plausible molecule.
    """
    registry, library, sink, _doc = _stack(tmp_path, with_pdf=False)

    script = [
        AgentAction(action=ActionKind.TOOL_CALL, tool="search_literature",
                    arguments={"query": "phenylacetylene synthesis", "limit": 5}),
        AgentAction(action=ActionKind.FINAL,
                    summary="未提供结构输入，仅完成文献检索；需要结构确认。"),
    ]
    agent = ResearchAgent(llm=FakeLLMProvider(script), registry=registry,
                          documents=library, evidence=sink)
    task = agent.run("Analyze phenylacetylene", workflow=True)

    # -- 4. PARTIAL, no exception -----------------------------------------
    assert task.status is TaskStatus.PARTIAL
    assert agent.session is not None
    assert agent.session.status is WorkflowStatus.PARTIAL

    # -- 1-3. nothing resolved, nothing computed --------------------------
    # No resolution object exists, so no candidate or selected structure can
    # pass off a guess as an establishment. And the structure-dependent
    # tools never ran -- which is the direct guarantee that no analysis of a
    # guessed structure was produced. (The loop may still record a
    # literature-search observation among its findings; that note is about
    # the search, not about any molecule.)
    assert registry.identity.resolution is None
    ran = [s.tool for s in task.steps if s.result_status is not None]
    assert "resolve_chemical_identity" not in ran
    assert "analyze_molecule" not in ran
    assert "retrosynthesize" not in ran

    # -- 5. the report carries the honest verdict -------------------------
    markdown = render_research_report(
        task.report, session=agent.session,
        identity=registry.identity.resolution)
    assert "# Research Report" in markdown
    assert "Identity unresolved" in markdown
    assert "Status: PARTIAL" in markdown
    assert "resolution status: resolved" not in markdown
    _assert_no_verdict_language(markdown)


# ---------------------------------------------------------------------------
# Scenario D -- a user PDF becomes evidence, not literature, and not a route
# ---------------------------------------------------------------------------


def _d_stack(tmp_path):
    """An offline stack whose registry can extract a ReactionRecord.

    Same fixture literature and fake retrosynthesis as the other scenarios,
    plus the extraction tool wired with a scripted model -- the pattern
    `tests/integration_offline/test_reaction_tool.py` already establishes:
    the model is scripted, everything else (quote checks, quantity policy,
    the record) is real. Returns `(registry, library, sink, document_id,
    evidence_id)` where `evidence_id` is the passage the payload must cite.
    """
    from crea.agent.offline import (
        FixtureTransport,
        _crossref_payload,
        _openalex_payload,
    )
    from crea.agent.toolsets import build_registry
    from crea.agent.workspace import DocumentLibrary, IdentityState, RouteWorkbench
    from crea.providers.fake_retrosynthesis import FakeRetrosynthesisProvider
    from crea.services.document_evidence import search_document
    from crea.services.documents import LocalDocumentService
    from crea.services.identity import IdentityResolutionService
    from crea.services.literature import LiteratureService
    from crea.services.reaction_extraction import ReactionExtractor
    from crea.services.route_validation import RouteValidationService
    from crea.sources.crossref import CrossrefProvider, WORKS_ENDPOINT as CR
    from crea.sources.openalex import OpenAlexProvider, WORKS_ENDPOINT as OA
    from crea.sources.pdf import PypdfDocumentParser
    from tests.integration_offline.test_reaction_tool import (
        ScriptedModel,
        scripted_payload,
    )

    transport = FixtureTransport({OA: _openalex_payload(), CR: _crossref_payload()})
    literature = LiteratureService({
        "openalex": OpenAlexProvider(transport=transport, endpoint=OA),
        "crossref": CrossrefProvider(transport=transport, endpoint=CR),
    })
    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    path = tmp_path / "procedure.pdf"
    path.write_bytes(pdfs.make_pdf(pdfs.EXPERIMENTAL))
    parsed = library.ingest(path)
    document_id = parsed.document.document_id

    # the passage the model will cite; the id is a content hash, so the
    # payload built from it resolves against the evidence the tools hold
    evidence_id = search_document(parsed, "CuI")[0].evidence.evidence_id

    sink: list = []
    routes = RouteWorkbench(provider=FakeRetrosynthesisProvider(),
                            validator=RouteValidationService())
    registry = build_registry(
        literature, library, sink,
        extractor=ReactionExtractor(ScriptedModel(scripted_payload(evidence_id))),
        routes=routes,
        identity=IdentityState(service=IdentityResolutionService()),
    )
    return registry, library, sink, document_id, evidence_id


def test_scenario_D_user_pdf_yields_evidence_and_record_not_literature(tmp_path):
    """User PDF → DocumentLibrary → Evidence → ReactionRecord → Report.

    The PDF is a procedure the operator handed us. Its evidence is
    user-provided, its numbers stay in the units the text wrote, fields the
    text never mentions stay NOT_FOUND, every reference resolves, and the
    report keeps the whole thing apart from literature discovery.
    """
    from crea.core.models.identity import StructureInput
    from crea.services.research_workflow import ResearchWorkflow

    registry, library, sink, document_id, evidence_id = _d_stack(tmp_path)
    workflow = ResearchWorkflow(registry, evidence_sink=sink)
    task = ResearchTask(task_id="scenario-d1",
                        question="分析用户提供的实验 PDF")

    report = workflow.run(
        task,
        structure_input=StructureInput.of(kind="SMILES", value=DIYNE),
        document_id=document_id,
        document_query="CuI",
    )

    # -- 1. provenance: user_provided, never publisher/literature ----------
    assert report.evidence, "the PDF must yield evidence"
    for item in report.evidence:
        assert item.source == "user_provided"
        assert item.strength is EvidenceStrength.USER_PROVIDED_EVIDENCE
        assert item.source not in ("publisher", "crossref", "openalex")

    # -- 6. experimental values keep their raw form -----------------------
    assert report.reaction_details, "the PDF must yield a ReactionRecord"
    record = report.reaction_details[0]
    conditions = record.conditions

    temperature = conditions.temperature
    assert temperature.value == 80
    assert temperature.raw_value == "80"
    assert temperature.raw_unit == "C"   # as written; no Kelvin conversion
    time = conditions.time
    assert time.value == 12
    assert time.raw_value == "12"
    assert time.raw_unit == "h"          # no conversion to minutes
    yield_value = conditions.yield_value
    assert yield_value.value == 82
    assert yield_value.raw_value == "82"
    assert yield_value.raw_unit == "%"

    # -- 7. absent parameters stay absent ---------------------------------
    assert conditions.pressure.status is FieldStatus.NOT_FOUND
    assert conditions.pressure.value is None
    assert conditions.atmosphere == ""   # the text never states one
    assert conditions.workup == ""

    # -- 8. every evidence reference resolves ------------------------------
    held = {e.evidence_id for e in report.evidence}
    assert evidence_id in held, "the cited passage must be held evidence"
    for ref in record.evidence_refs:
        assert ref in held, f"record cites unheld evidence {ref}"
    for field in (temperature, time, yield_value):
        for ref in field.evidence_refs:
            assert ref in held

    # -- 10. the report separates this from literature --------------------
    markdown = render_research_report(report, identity=registry.identity.resolution)
    exp_section = markdown.split("## 5. Experimental Evidence")[1].split("## 6.")[0]
    assert "| Temperature | 80 C |" in exp_section
    assert "| Time | 12 h |" in exp_section
    assert "| Yield | 82 %" in exp_section
    # the user PDF's evidence is experimental evidence, not literature
    backed_literature = markdown.split(
        "### Evidence-backed Literature")[1].split("## 5.")[0]
    assert evidence_id not in backed_literature, (
        "a user PDF must not be rendered as published literature evidence")
    assert "user_provided" not in backed_literature
    _assert_no_verdict_language(markdown)


def test_scenario_D_one_record_does_not_upgrade_a_whole_route(tmp_path):
    """ReactionRecord → RouteEvidence: one measured link lifts one step.

    The fixture procedure is a phenylacetylene coupling, and the fake
    retrosynthesis proposes exactly that transformation as one step -- and
    other steps it does not touch. Attaching the record where its structures
    match may earn that step user-document experimental support. The other
    steps stay candidates: a procedure is evidence for the reaction it
    describes, not for a whole synthesis. An unmeasured attachment is
    reported as read but refuses to promote.
    """
    from crea.core.models.identity import StructureInput
    from crea.services.research_workflow import ResearchWorkflow

    registry, library, sink, document_id, evidence_id = _d_stack(tmp_path)
    workflow = ResearchWorkflow(registry, evidence_sink=sink)
    task = ResearchTask(task_id="scenario-d2",
                        question="用户 PDF 能否支持候选路线的某一步")
    report = workflow.run(
        task,
        structure_input=StructureInput.of(kind="SMILES", value=DIYNE),
        document_id=document_id,
        document_query="CuI",
    )
    assert report.reaction_details
    record_id = report.reaction_details[0].reaction_id

    # structures read for the record: phenylacetylene coupling
    phenylacetylene = "C#Cc1ccccc1"
    diyne = registry.execute(
        "analyze_molecule", {"smiles": DIYNE}).data["canonical_smiles"]
    coupling_reactants = [phenylacetylene, phenylacetylene]
    coupling_products = [diyne]

    all_steps = [(route, step) for route in report.routes for step in route.steps]
    assert all_steps, "the fixture must propose candidate routes"

    # attach the record once per step:
    #  - the coupling step gets the structures the record was read from
    #  - every other step gets no structures: recorded, not measured
    record_links = []
    coupling_step_id = None
    for _route, step in all_steps:
        structures = ([m.canonical_smiles or m.smiles for m in step.reactants]
                      == coupling_reactants)
        if structures:
            coupling_step_id = step.step_id
        record_links.append({
            "step_id": step.step_id,
            "reaction_id": record_id,
            "reactant_smiles": coupling_reactants if structures else [],
            "product_smiles": coupling_products if structures else [],
        })
    assert coupling_step_id is not None, (
        "the fixture must propose the coupling step the PDF describes")

    # The linking runs through the service the tool itself wraps -- the same
    # public interface `tests/unit/test_route_report_evidence.py` exercises.
    # The equivalent path through `ToolRegistry.execute("route_evidence_report",
    # {"record_links": ...})` is regression-tested in tests/unit/test_toolsets.py.
    from crea.services.evidence import EvidenceRegistry
    from crea.services.route_evidence import (
        RouteEvidenceInputs,
        RouteEvidenceService,
        build_record_link,
    )

    record = report.reaction_details[0]
    step_by_id = {step.step_id: step for _route, step in all_steps}
    links = [
        build_record_link(step_by_id[link["step_id"]], record_id,
                          link["reactant_smiles"] or [],
                          link["product_smiles"] or [],
                          list(record.evidence_refs))
        for link in record_links
    ]
    held = EvidenceRegistry(items={e.evidence_id: e for e in sink})
    service = RouteEvidenceService(held, RouteEvidenceInputs(
        candidates=[], records=list(registry.routes.records), record_links=links))
    chains = [service.build(route) for route in report.routes]
    assert chains

    EXPERIMENTAL_RUNGS = (
        ReactionEvidenceLevel.EXPERIMENTAL_REACTION_SUPPORT,
        ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT,
    )
    promoted, unpromoted = [], []
    for chain in chains:
        for chain_step in chain.steps:
            if chain_step.evidence_level in EXPERIMENTAL_RUNGS:
                promoted.append(chain_step)
            else:
                unpromoted.append(chain_step)

    # -- exactly one step is supported: the measured one -------------------
    assert len(promoted) == 1, (
        "one record must lift one matched step, not the whole route")
    supported = promoted[0]
    assert supported.step_id == coupling_step_id
    assert supported.evidence_level is (
        ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT)
    assert supported.match_type is ReactionMatchType.EXACT
    assert record_id in supported.experimental_support
    assert supported.support_provenance == "user_provided"
    assert evidence_id in supported.evidence_refs

    # -- the rest stay candidates -----------------------------------------
    assert unpromoted, "the fixture must propose steps the record does not cover"
    for chain_step in unpromoted:
        assert chain_step.evidence_level not in EXPERIMENTAL_RUNGS
        assert chain_step.experimental_support == []
        assert chain_step.candidate_status is StepStatus.CANDIDATE
        # an unmeasured attachment is listed as read-but-not-supporting,
        # never silently dropped and never counted as support
        assert record_id in chain_step.unlinked_records
        assert any("without an exact structure comparison" in w
                   for w in chain_step.warnings)

    # -- the report keeps both facts visible ------------------------------
    markdown = render_research_report(
        report.model_copy(update={"route_evidence": chains}),
        identity=registry.identity.resolution)
    assert "# Research Report" in markdown
    assert "Candidate only" in markdown  # the unpromoted steps say so
    assert "user_document_experimental_support" in markdown  # the promoted one
    _assert_no_verdict_language(markdown)

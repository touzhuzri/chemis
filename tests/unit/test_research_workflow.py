"""The ResearchWorkflow service: one task through the existing tools.

§2 of Phase 3.7, and only that: the workflow orchestrates, the tools
answer, the report renders what came back. Three paths are covered here --
a run where everything the environment supports succeeds, an identity that
cannot be resolved (which must end the structure-dependent steps rather
than guess a structure), and a retrosynthesis capability that is not
configured (which must be recorded as unavailable, not papered over with a
placeholder route).

The harness is the same offline wiring the agent tests use: fixture
literature providers, the real tool registry, the real identity service,
the fake retrosynthesis provider. Nothing here reaches the network.
"""

from __future__ import annotations

from crea.agent.offline import (
    FixtureTransport,
    _crossref_payload,
    _openalex_payload,
)
from crea.agent.toolsets import build_registry
from crea.agent.workspace import DocumentLibrary, IdentityState, RouteWorkbench
from crea.core.models.identity import StructureInput
from crea.core.models.research import ResearchTask, TaskStatus, ToolStatus
from crea.providers.fake_retrosynthesis import FakeRetrosynthesisProvider
from crea.services.documents import LocalDocumentService
from crea.services.identity import IdentityResolutionService
from crea.services.literature import LiteratureService
from crea.services.research_workflow import ResearchWorkflow
from crea.services.route_validation import RouteValidationService
from crea.sources.crossref import CrossrefProvider, WORKS_ENDPOINT as CR
from crea.sources.openalex import OpenAlexProvider, WORKS_ENDPOINT as OA
from crea.sources.pdf import PypdfDocumentParser

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"
QUESTION = "分析 1,4-diphenyl-1,3-diyne 的性质、文献与合成方法"


def _workflow(*, provider=None, with_routes=True, identity=None):
    """A workflow over the offline stack. Returns (workflow, sink, library)."""
    transport = FixtureTransport({OA: _openalex_payload(), CR: _crossref_payload()})
    literature = LiteratureService({
        "openalex": OpenAlexProvider(transport=transport, endpoint=OA),
        "crossref": CrossrefProvider(transport=transport, endpoint=CR),
    })
    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    sink: list = []
    workbench = (
        RouteWorkbench(provider=FakeRetrosynthesisProvider(),
                       validator=RouteValidationService())
        if with_routes else None
    )
    if identity is None:
        identity = IdentityState(
            service=IdentityResolutionService(compound_provider=provider))
    registry = build_registry(literature, library, sink, routes=workbench,
                              identity=identity)
    return ResearchWorkflow(registry, evidence_sink=sink), sink, library


def _task(task_id: str = "wf-1") -> ResearchTask:
    return ResearchTask(task_id=task_id, question=QUESTION)


# ---------------------------------------------------------------------------
# success
# ---------------------------------------------------------------------------


def test_a_user_supplied_smiles_runs_every_supported_step():
    workflow, sink, _library = _workflow()
    task = _task()

    report = workflow.run(
        task, structure_input=StructureInput.of("SMILES", DIYNE))

    assert task.status is TaskStatus.COMPLETED
    assert report.status is TaskStatus.COMPLETED
    assert task.errors == []

    # step 2 -> computed findings, labelled computed, from analyze_molecule
    computed = [f for f in report.findings if f.kind == "computed"]
    assert computed
    assert any("C16H10" in f.text for f in computed)

    # step 3 -> discovery lands in sources, not in evidence
    assert report.sources
    assert report.evidence == []

    # step 5/6 -> candidate routes, validated and chained
    assert report.routes
    assert report.route_evidence
    step_statuses = {s.status.value for r in report.routes for s in r.steps}
    assert step_statuses <= {"candidate", "literature_supported",
                             "experimentally_supported"}

    # step 4 -> no document supplied, reported as such and not fabricated
    assert "No user document provided" in report.unresolved_items


def test_the_session_trace_records_each_workflow_step_in_order():
    workflow, _sink, _library = _workflow()
    task = _task("wf-trace")

    workflow.run(task, structure_input=StructureInput.of("SMILES", DIYNE))

    tools = [s.tool for s in task.steps]
    # ordered: identity, analysis, literature, routes; no document step
    assert tools[:3] == ["resolve_chemical_identity", "analyze_molecule",
                         "search_literature"]
    assert tools[3] == "retrosynthesize"
    assert tools.count("retrosynthesize") == 1
    assert "get_document_evidence" not in tools
    assert "route_evidence_report" in tools
    assert all(s.result_status is not None for s in task.steps)
    assert task.finished_at is not None


# ---------------------------------------------------------------------------
# identity unresolved
# ---------------------------------------------------------------------------


def test_an_unresolvable_name_ends_the_structure_steps_without_guessing():
    """No provider behind the name lookup: identity is unresolved, and the
    analysis / retrosynthesis / validation steps are skipped and recorded.

    The one thing that must not happen is a plausible structure appearing
    anyway -- so the report carries no computed findings and no routes.
    """
    workflow, _sink, _library = _workflow(provider=None)
    task = _task("wf-no-identity")

    report = workflow.run(
        task, structure_input=StructureInput.of("NAME", "no-such-compound"))

    assert task.status is TaskStatus.PARTIAL
    assert report.status is TaskStatus.PARTIAL

    # unresolved, stated in the report
    assert any("identity unresolved" in item for item in report.unresolved_items)

    # no guessed structure anywhere
    assert not [f for f in report.findings if f.kind == "computed"]
    assert report.routes == []

    # the structure-dependent steps are visible as skips, not silent
    by_tool = {s.tool: s for s in task.steps}
    assert by_tool["resolve_chemical_identity"].result_status is ToolStatus.OK
    for tool in ("analyze_molecule", "retrosynthesize", "validate_route_step"):
        assert by_tool[tool].result_status is None
        assert "identity unresolved" in by_tool[tool].note

    # steps that do not need a structure still ran
    assert any(s.tool == "search_literature" for s in task.steps)


def test_no_structure_input_at_all_is_recorded_not_invented():
    workflow, _sink, _library = _workflow()
    task = _task("wf-no-input")

    report = workflow.run(task)

    assert task.status is TaskStatus.PARTIAL
    assert any("identity unresolved" in item for item in report.unresolved_items)
    assert report.routes == []
    assert not [f for f in report.findings if f.kind == "computed"]
    assert any(s.tool == "resolve_chemical_identity" and
               "未提供 structure_input" in s.note for s in task.steps)


# ---------------------------------------------------------------------------
# unavailable capability
# ---------------------------------------------------------------------------


def test_an_unconfigured_retrosynthesis_provider_is_recorded_not_faked():
    """No routes workbench -> the tool answers not_configured with zero
    routes. The workflow must mark the run partial, keep the steps that did
    succeed, and render no route at all."""
    workflow, _sink, _library = _workflow(with_routes=False)
    task = _task("wf-no-retro")

    report = workflow.run(
        task, structure_input=StructureInput.of("SMILES", DIYNE))

    assert task.status is TaskStatus.PARTIAL
    assert report.status is TaskStatus.PARTIAL
    assert report.routes == []
    assert any("逆合成未返回候选路线" in item for item in report.unresolved_items)

    # the earlier steps still happened -- one failure does not abort the run
    assert any(f.kind == "computed" for f in report.findings)
    assert report.sources
    by_tool = {s.tool: s for s in task.steps}
    assert by_tool["retrosynthesize"].result_status is ToolStatus.OK
    assert by_tool["validate_route_step"].result_status is None
    assert "没有候选路线" in by_tool["validate_route_step"].note

    # and the report still renders, labelled partial
    from crea.report.research import format_report

    text = format_report(report)
    assert "部分完成" in text


def test_a_tool_failure_is_recorded_as_an_error_not_swallowed():
    """A document id that is not in the library makes get_document_evidence
    fail; the failure lands in task.errors and the report, and the later
    steps still run."""
    workflow, _sink, _library = _workflow()
    task = _task("wf-bad-doc")

    report = workflow.run(
        task,
        structure_input=StructureInput.of("SMILES", DIYNE),
        document_id="no-such-document",
    )

    assert task.status is TaskStatus.PARTIAL
    assert any("get_document_evidence 未成功" in e for e in task.errors)
    assert any("get_document_evidence" in item for item in report.unresolved_items)
    # the chain stopped at the document step but retrosynthesis still ran
    assert report.routes

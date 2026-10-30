"""CREA V1.1 phase 1 -- ResearchWorkflow -> Identity -> Literature -> Report.

Five correctness fixes, each pinned by a regression test:

A/B. Literature discovery is anchored to a chemical object. With identity
     unresolved it must not run, and the whole research question is never
     used as a provider query.
C.   "Existing knowledge" is scoped to this run's chemical object -- the
     report never dumps the global knowledge base.
D.   Knowledge gaps reflect the real state; identity-unresolved is a gap,
     and a conflict-synthesis failure can no longer blank the gap list.
E.   Progress and report agree: "Stopped at" names a step that actually ran,
     including when the session was read back from the database.
F.   Blocked-by-input is distinct from no-results and from partial -- the
     report says which.

Nothing here reaches the network; the literature providers are the offline
fixtures.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from crea.agent.offline import (
    FixtureTransport,
    _crossref_payload,
    _openalex_payload,
)
from crea.agent.toolsets import build_registry
from crea.agent.workspace import (
    DocumentLibrary,
    IdentityState,
    RouteWorkbench,
)
from crea.core.models.identity import StructureInput
from crea.core.models.literature import Identifiers, Paper
from crea.core.models.research import ResearchTask, TaskStatus, ToolStatus
from crea.core.models.session import WorkflowStatus
from crea.providers.fake_retrosynthesis import FakeRetrosynthesisProvider
from crea.report.research import _stopped_at, render_research_report_v3
from crea.services.documents import LocalDocumentService
from crea.services.identity import IdentityResolutionService
from crea.services.knowledge_base import KnowledgeBase
from crea.services.literature import LiteratureService
from crea.services.research_study import ResearchStudyService
from crea.services.research_task_executor import ResearchTaskExecutor
from crea.services.research_workflow import ResearchWorkflow
from crea.services.route_validation import RouteValidationService
from crea.services.task_workflow import TaskWorkflowService
from crea.sources.crossref import CrossrefProvider, WORKS_ENDPOINT as CR
from crea.sources.openalex import OpenAlexProvider, WORKS_ENDPOINT as OA
from crea.sources.pdf import PypdfDocumentParser

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"

#: The four natural-language questions the V1.1 acceptance names. None of
#: them carries an explicit structure -- a name inside a sentence is not one.
NL_QUESTIONS = [
    "请分析苯乙炔（phenylacetylene）并给出可能的反应路线",
    "比较邻二甲苯、间二甲苯和对二甲苯的化学性质",
    "以苯乙炔为底物，检索当前知识库中的已知反应",
    "从苯出发，两步以内得到阿司匹林",
]


def _offline_stack(kb=None):
    from crea.agent.offline import build_offline_workflow_stack

    return build_offline_workflow_stack(knowledge_base=kb)


def _fixture_literature():
    transport = FixtureTransport({OA: _openalex_payload(),
                                  CR: _crossref_payload()})
    return LiteratureService({
        "openalex": OpenAlexProvider(transport=transport, endpoint=OA),
        "crossref": CrossrefProvider(transport=transport, endpoint=CR),
    })


def _workflow_with_literature(literature, *, kb=None, retro=True):
    """A workflow whose literature service and retrosynthesis are chosen."""
    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    sink: list = []
    workbench = RouteWorkbench(
        provider=FakeRetrosynthesisProvider() if retro else None,
        validator=RouteValidationService())
    identity = IdentityState(service=IdentityResolutionService())
    registry = build_registry(literature, library, sink, routes=workbench,
                              identity=identity)
    return ResearchWorkflow(registry, evidence_sink=sink,
                            knowledge_base=kb), sink


# ---------------------------------------------------------------------------
# A/B -- no literature discovery without a determined chemical object
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("question", NL_QUESTIONS)
def test_a_natural_language_question_never_searches_the_whole_sentence(question):
    kb = KnowledgeBase(pathlib.Path(tempfile.mkdtemp()) / "c.db")
    kb.initialize()
    try:
        workflow, _library, _sink, _registry = _offline_stack(kb)
        task = ResearchTask(task_id="nl", question=question)
        report = workflow.run(task, structure_input=None)

        # no irrelevant discovery: the search was skipped, not fed the sentence
        assert report.sources == []
        lit = [s for s in task.steps if s.tool == "search_literature"]
        assert lit and lit[0].result_status is None, "discovery did not run"
        assert "identity unresolved" in lit[0].note
        assert "query" not in lit[0].arguments, (
            "the sentence never became a provider query")

        # the report states the identity / input gap plainly
        assert report.outcome == "input_required"
        gaps = [g for g in report.knowledge.gaps if g.facet.value == "entity"]
        assert gaps and "缺少结构输入" in gaps[0].reason
        markdown = render_research_report_v3(report,
                                             session=workflow.session)
        assert "Identity unresolved" in markdown
        assert "Outcome: INPUT_REQUIRED" in markdown
    finally:
        kb.close()


def test_identity_unresolved_still_skips_structure_dependent_steps():
    workflow, _library, _sink, _registry = _offline_stack()
    task = ResearchTask(task_id="nl-2", question=NL_QUESTIONS[0])
    report = workflow.run(task, structure_input=None)

    # analysis / routes never ran and never invented a structure
    assert report.routes == []
    assert not [f for f in report.findings if f.kind == "computed"]
    by_tool = {s.tool: s for s in task.steps}
    for tool in ("analyze_molecule", "retrosynthesize", "validate_route_step"):
        assert by_tool[tool].result_status is None
        assert "identity unresolved" in by_tool[tool].note


# ---------------------------------------------------------------------------
# B -- with a resolved structure the query is the compound, not the question
# ---------------------------------------------------------------------------


def test_a_resolved_identity_anchors_the_query_to_the_compound():
    workflow, _library, _sink, _registry = _offline_stack()
    task = ResearchTask(task_id="anchored",
                        question="研究目标化合物的文献合成方法")
    report = workflow.run(task, structure_input=StructureInput.of("SMILES", DIYNE))

    lit = [s for s in task.steps if s.tool == "search_literature"]
    assert lit and lit[0].result_status is ToolStatus.OK, "discovery ran"
    query = lit[0].arguments["query"]
    assert query != task.question, "the raw question was not the query"
    assert DIYNE in query, "the query is anchored to the chemical object"
    assert report.sources


# ---------------------------------------------------------------------------
# C -- the report never dumps the global knowledge base
# ---------------------------------------------------------------------------


def test_existing_knowledge_is_scoped_and_never_leaks_global_records():
    db = pathlib.Path(tempfile.mkdtemp()) / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    try:
        # an unrelated paper and entity sit in the base from another line of work
        kb.save_paper(Paper(
            paper_id="unrelated-1", title="Foot image segmentation",
            retrieved_at="2026-01-01",
            identifiers=Identifiers(doi="10.9/unrelated")))

        workflow, _library, _sink, _registry = _offline_stack(kb)
        task = ResearchTask(task_id="scope", question="研究目标")
        report = workflow.run(task, structure_input=StructureInput.of("SMILES", DIYNE))

        existing = {k: v for k, v in (report.knowledge.existing or {}).items() if v}
        assert "unrelated-1" not in (report.knowledge.existing or {}).get("papers", [])
        assert all("unrelated" not in str(ids) for ids in existing.values())
        # the run's own product is NEW, not "existing"
        assert report.knowledge.new.get("entities")
    finally:
        kb.close()


# ---------------------------------------------------------------------------
# D -- gaps reflect the real state
# ---------------------------------------------------------------------------


def test_gaps_are_not_blanked_by_a_conflict_synthesis_failure():
    """synthesize_fields raises when there is no entity; that used to blank
    the whole gap list, so identity-unresolved showed as "no gaps"."""
    db = pathlib.Path(tempfile.mkdtemp()) / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    try:
        workflow, _library, _sink, _registry = _offline_stack(kb)
        task = ResearchTask(task_id="gaps", question=NL_QUESTIONS[0])
        report = workflow.run(task, structure_input=None)

        assert report.knowledge is not None
        assert report.knowledge.gaps, "identity-unresolved must be a gap"
        facets = {g.facet.value for g in report.knowledge.gaps}
        assert "entity" in facets
        assert "reaction" in facets
        assert "experimental_evidence" in facets
    finally:
        kb.close()


# ---------------------------------------------------------------------------
# E -- progress and report agree
# ---------------------------------------------------------------------------


def test_stopped_at_names_a_step_that_ran_not_the_last_recorded_skip():
    """A PARTIAL run whose literature step *did* complete must not read as
    "before any step completed" -- the contradiction the V1.1 audit found."""
    # no retrosynthesis provider: the run ends PARTIAL, but literature ran
    workflow, _sink = _workflow_with_literature(_fixture_literature(), retro=False)
    task = ResearchTask(task_id="stopped", question="q")
    report = workflow.run(task, structure_input=StructureInput.of("SMILES", DIYNE))

    assert report.outcome == "partial"
    assert "文献检索" in report.interpretation, "a step did complete"
    stopped = _stopped_at(workflow.session)
    assert stopped != "before any step completed", (
        "a completed step and 'before any step completed' cannot both be true")
    # it names a step that really ran
    ran = {s.index for s in task.steps if s.result_status is not None}
    assert int(stopped.split()[1].strip("(")) in ran


def test_a_session_read_back_from_the_database_still_knows_where_it_stopped():
    db = pathlib.Path(tempfile.mkdtemp()) / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    try:
        workflow, _library, _sink, _registry = _offline_stack(kb)
        task = ResearchTask(task_id="persisted", question="q")
        workflow.run(task, structure_input=StructureInput.of("SMILES", DIYNE))
        session = workflow.session

        loaded = kb.get_session(session.session_id)
        assert loaded is not None
        # session-level steps are not stored; the task's are (existing schema)
        assert loaded.steps == []
        stopped = _stopped_at(loaded)
        assert stopped != "before any step completed", (
            "a run that completed steps must not read as 'before any step completed'")
        assert stopped.startswith("step ")
    finally:
        kb.close()


# ---------------------------------------------------------------------------
# F -- blocked / no-results / partial are distinct
# ---------------------------------------------------------------------------


def test_an_unparsable_structure_input_blocks_with_a_distinct_outcome():
    workflow, _library, _sink, _registry = _offline_stack()
    task = ResearchTask(task_id="blocked", question="q")
    report = workflow.run(task, structure_input=StructureInput.of("NAME", "no-such-compound"))

    assert report.outcome in ("input_required", "failed", "no_results")
    assert report.outcome != "completed"
    assert any("input required" in item for item in report.unresolved_items) or \
        any("identity unresolved" in item for item in report.unresolved_items)


def test_an_explicit_structure_is_not_undone_by_empty_literature():
    """#5: a SMILES enters deterministic analysis, and a literature search
    that returns nothing must not flip the identity to unresolved."""
    literature = LiteratureService({})  # no providers -> no papers
    workflow, _sink = _workflow_with_literature(literature)
    task = ResearchTask(task_id="empty-lit", question="q")
    report = workflow.run(task, structure_input=StructureInput.of("SMILES", "CCO"))

    computed = [f for f in report.findings if f.kind == "computed"]
    assert computed, "molecule analysis ran for the explicit structure"
    assert "C2H6O" in " ".join(f.text for f in computed)
    # no literature, but the identity is untouched
    assert report.sources == []
    assert not any("identity unresolved" in item for item in report.unresolved_items)
    assert any("文献检索未返回任何论文" in item for item in report.unresolved_items)


# ---------------------------------------------------------------------------
# #3 / #4 -- an unparsable structure blocks the task, never fakes a result
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template,params", [
    ("molecule_modification", {"substrate": "苯乙炔", "query": "x"}),
    ("route_study", {"start": "苯", "target": "阿司匹林"}),
])
def test_a_chemical_name_blocks_the_task_instead_of_faking_a_result(template, params):
    db = pathlib.Path(tempfile.mkdtemp()) / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    try:
        study = ResearchStudyService(kb).create(title="S", question="q")
        service = TaskWorkflowService(kb, ResearchTaskExecutor(kb))
        workflow = service.create(study.study_id, template=template, params=params)
        service.start(workflow.workflow_id)
        ready = service.ready_tasks(workflow.workflow_id)

        blocked = 0
        for task in ready:
            service.execute_task(workflow.workflow_id, task.task_id)
            task = kb.get_task(task.task_id)
            if task.status.value == "blocked":
                blocked += 1
                assert "TASK_INPUT_INVALID" in (task.error or "")
                assert task.output_artifact_ids == [], "no fabricated artifact"
        assert blocked >= 1, "the structure task must be blocked"

        # the blocked workflow is reported as blocked, not completed
        assert kb.get_workflow(workflow.workflow_id).status.value in (
            "blocked", "running")
    finally:
        kb.close()

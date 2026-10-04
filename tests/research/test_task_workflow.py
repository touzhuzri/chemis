"""Bounded research workflow (Stage 7.2).

A finite task DAG over a research session. Tasks run one at a time, on
explicit request; each stores an artifact. Cycles are rejected at creation,
dependencies gate readiness, AI tasks use a fake LLM, and the workflow never
auto-runs.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from crea.core.errors import ResearchWorkflowError
from crea.core.models.task_workflow import (
    TaskStatus,
    TaskType,
    WorkflowStatus,
)
from crea.services.knowledge_base import KnowledgeBase
from crea.services.research_study import ResearchStudyService
from crea.services.research_task_executor import ResearchTaskExecutor
from crea.services.task_workflow import TEMPLATES, TaskWorkflowService
from crea.llm.fake import TextScriptedLLMProvider


@pytest.fixture()
def env(tmp_path):
    kb = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    kb.initialize()
    study = ResearchStudyService(kb).create(title="S", question="q")
    return kb, study.study_id


def _service(kb, *, llm=None, literature=None):
    executor = ResearchTaskExecutor(
        kb, literature=literature,
        modification_factory=(lambda: _modification(kb, llm)) if llm else None,
        route_reasoning_factory=(lambda: _route_reasoning(kb, llm)) if llm else None)
    return TaskWorkflowService(kb, executor)


def _modification(kb, llm):
    from crea.services.ai_context import AIContextService
    from crea.services.modification_analysis import ModificationAnalysisService

    return ModificationAnalysisService(AIContextService(kb), llm)


def _route_reasoning(kb, llm):
    from crea.services.ai_context import AIContextService
    from crea.services.route_explorer import RouteExplorerService
    from crea.services.route_reasoning import RouteReasoningService

    return RouteReasoningService(AIContextService(kb), RouteExplorerService(kb=kb), llm)


# -- 1/2. create + template --------------------------------------------------


def test_create_workflow_from_a_template(env):
    kb, study_id = env
    svc = _service(kb)
    wf = svc.create(study_id, template="molecule_modification",
                    params={"substrate": "CCO",
                            "reaction_template_id": "primary-alcohol-oxidation",
                            "query": "x"})
    assert wf.status is WorkflowStatus.READY
    detail = svc.detail(wf.workflow_id)
    assert [t.task_type for t in detail.tasks] == [
        TaskType.TRANSFORMATION_SEARCH, TaskType.REACTION_COMPARISON,
        TaskType.EVIDENCE_REVIEW, TaskType.MODIFICATION_ANALYSIS]
    kb.close()


def test_workflow_needs_a_template_or_tasks(env):
    kb, study_id = env
    with pytest.raises(ResearchWorkflowError) as exc:
        _service(kb).create(study_id)
    assert exc.value.code == "INVALID_INPUT"
    kb.close()


# -- 3. dependency graph -----------------------------------------------------


def test_dependency_graph_is_correct(env):
    kb, study_id = env
    svc = _service(kb)
    wf = svc.create(study_id, template="route_study",
                    params={"start": "CCO", "target": "CC=O"})
    tasks = {t.task_type: t for t in svc.detail(wf.workflow_id).tasks}
    route = tasks[TaskType.ROUTE_EXPLORATION]
    search = tasks[TaskType.TRANSFORMATION_SEARCH]
    assert route.depends_on == [search.task_id]
    kb.close()


# -- 4. cycle detection ------------------------------------------------------


def test_cycles_are_rejected_at_creation(env):
    kb, study_id = env
    with pytest.raises(ResearchWorkflowError) as exc:
        _service(kb).create(study_id, tasks=[
            {"key": "a", "task_type": "molecule_inspection", "title": "A",
             "deps": ["b"], "inputs": {}},
            {"key": "b", "task_type": "molecule_inspection", "title": "B",
             "deps": ["a"], "inputs": {}}])
    assert exc.value.code == "WORKFLOW_CYCLE"
    kb.close()


def test_too_many_tasks_is_rejected(env):
    kb, study_id = env
    with pytest.raises(ResearchWorkflowError) as exc:
        _service(kb).create(study_id, tasks=[
            {"key": f"t{i}", "task_type": "molecule_inspection", "title": "x",
             "inputs": {}} for i in range(30)])
    assert exc.value.code == "WORKFLOW_LIMIT_REACHED"
    kb.close()


# -- 5. ready resolution -----------------------------------------------------


def test_only_ready_tasks_have_satisfied_dependencies(env):
    kb, study_id = env
    svc = _service(kb)
    wf = svc.create(study_id, template="molecule_modification",
                    params={"substrate": "CCO",
                            "reaction_template_id": "primary-alcohol-oxidation"})
    # before start, ready_tasks refreshes pending -> ready
    ready = {t.task_type for t in svc.ready_tasks(wf.workflow_id)}
    assert ready == {TaskType.TRANSFORMATION_SEARCH}, "only the root is ready"
    kb.close()


def test_executing_a_task_readies_its_dependents(env):
    kb, study_id = env
    svc = _service(kb)
    wf = svc.create(study_id, template="molecule_modification",
                    params={"substrate": "CCO",
                            "reaction_template_id": "primary-alcohol-oxidation"})
    svc.start(wf.workflow_id)
    search = svc.ready_tasks(wf.workflow_id)[0]
    svc.execute_task(wf.workflow_id, search.task_id)
    ready = {t.task_type for t in svc.ready_tasks(wf.workflow_id)}
    assert TaskType.REACTION_COMPARISON in ready
    assert TaskType.EVIDENCE_REVIEW in ready
    assert TaskType.MODIFICATION_ANALYSIS not in ready, "still gated on evidence"
    kb.close()


# -- 6. deterministic task execution → artifact ------------------------------


def test_deterministic_task_produces_an_artifact(env):
    kb, study_id = env
    svc = _service(kb)
    wf = svc.create(study_id, template="molecule_modification",
                    params={"substrate": "CCO",
                            "reaction_template_id": "primary-alcohol-oxidation"})
    svc.start(wf.workflow_id)
    task = svc.ready_tasks(wf.workflow_id)[0]
    done = svc.execute_task(wf.workflow_id, task.task_id)
    assert done.status is TaskStatus.COMPLETED
    assert done.output_artifact_ids
    # the artifact is bound to the task and stored on the study
    from crea.services.research_study import ResearchStudyService

    artifacts = ResearchStudyService(kb).detail(study_id).artifacts
    assert artifacts[0].artifact_id == done.output_artifact_ids[0]
    assert artifacts[0].artifact_type.value == "transformation_search_result"
    kb.close()


# -- 7. AI task (fake LLM) ---------------------------------------------------


def test_ai_task_uses_a_fake_llm(env):
    kb, study_id = env
    llm = TextScriptedLLMProvider([json.dumps({"candidates": [], "notes": []})])
    svc = _service(kb, llm=llm)
    wf = svc.create(study_id, tasks=[
        {"key": "modify", "task_type": "modification_analysis", "title": "M",
         "inputs": {"substrate": "CCO", "query": "convert OH to Cl"}}])
    svc.start(wf.workflow_id)
    task = svc.ready_tasks(wf.workflow_id)[0]
    done = svc.execute_task(wf.workflow_id, task.task_id)
    assert done.status is TaskStatus.COMPLETED
    assert llm.calls, "the fake model was called"
    kb.close()


# -- 8/9/10. route / evidence / literature tasks -----------------------------


def test_evidence_task_runs(env):
    kb, study_id = env
    svc = _service(kb)
    wf = svc.create(study_id, tasks=[
        {"key": "ev", "task_type": "evidence_review", "title": "E",
         "inputs": {"reaction_template_id": "primary-alcohol-oxidation"}}])
    svc.start(wf.workflow_id)
    task = svc.ready_tasks(wf.workflow_id)[0]
    assert svc.execute_task(wf.workflow_id, task.task_id).status is TaskStatus.COMPLETED
    kb.close()


def test_route_task_runs(env):
    kb, study_id = env
    svc = _service(kb)
    wf = svc.create(study_id, tasks=[
        {"key": "r", "task_type": "route_exploration", "title": "R",
         "inputs": {"start": "CCO", "target": "CC=O"}}])
    svc.start(wf.workflow_id)
    task = svc.ready_tasks(wf.workflow_id)[0]
    assert svc.execute_task(wf.workflow_id, task.task_id).status is TaskStatus.COMPLETED
    kb.close()


def test_literature_task_without_a_service_fails_honestly(env):
    kb, study_id = env
    svc = _service(kb)  # no literature service
    wf = svc.create(study_id, tasks=[
        {"key": "lit", "task_type": "literature_search", "title": "L",
         "inputs": {"query": "anything"}}])
    svc.start(wf.workflow_id)
    task = svc.ready_tasks(wf.workflow_id)[0]
    done = svc.execute_task(wf.workflow_id, task.task_id)
    assert done.status is TaskStatus.FAILED
    assert "AI/PROVIDER_UNAVAILABLE" in done.error
    # and the workflow is BLOCKED, not quietly complete
    assert svc.get(wf.workflow_id).status is WorkflowStatus.BLOCKED
    kb.close()


def test_missing_input_blocks_a_task(env):
    kb, study_id = env
    svc = _service(kb)
    wf = svc.create(study_id, tasks=[
        {"key": "ev", "task_type": "evidence_review", "title": "E", "inputs": {}}])
    svc.start(wf.workflow_id)
    task = svc.ready_tasks(wf.workflow_id)[0]
    done = svc.execute_task(wf.workflow_id, task.task_id)
    assert done.status is TaskStatus.BLOCKED
    assert "TASK_INPUT_NOT_FOUND" in done.error
    kb.close()


# -- 11. pause / resume ------------------------------------------------------


def test_pause_resume_and_status_guards(env):
    kb, study_id = env
    svc = _service(kb)
    wf = svc.create(study_id, template="evidence_review",
                    params={"reaction_template_id": "primary-alcohol-oxidation"})
    # cannot execute before start
    with pytest.raises(ResearchWorkflowError) as exc:
        svc.execute_task(wf.workflow_id, svc.detail(wf.workflow_id).tasks[0].task_id)
    assert exc.value.code == "INVALID_WORKFLOW_STATE"
    svc.start(wf.workflow_id)
    svc.pause(wf.workflow_id)
    assert svc.get(wf.workflow_id).status is WorkflowStatus.PAUSED
    svc.resume(wf.workflow_id)
    assert svc.get(wf.workflow_id).status is WorkflowStatus.RUNNING
    kb.close()


# -- 12. cancel --------------------------------------------------------------


def test_cancel_is_terminal(env):
    kb, study_id = env
    svc = _service(kb)
    wf = svc.create(study_id, template="molecule_modification",
                    params={"substrate": "CCO",
                            "reaction_template_id": "primary-alcohol-oxidation"})
    svc.start(wf.workflow_id)
    svc.cancel(wf.workflow_id)
    assert svc.get(wf.workflow_id).status is WorkflowStatus.CANCELLED
    # no task may run afterwards
    with pytest.raises(ResearchWorkflowError) as exc:
        svc.execute_task(wf.workflow_id, svc.detail(wf.workflow_id).tasks[0].task_id)
    assert exc.value.code == "INVALID_WORKFLOW_STATE"
    kb.close()


# -- 13. completion is honest ------------------------------------------------


def test_workflow_completes_only_when_every_task_is_satisfied(env):
    kb, study_id = env
    svc = _service(kb)
    wf = svc.create(study_id, template="evidence_review",
                    params={"reaction_template_id": "primary-alcohol-oxidation"})
    svc.start(wf.workflow_id)
    task = svc.ready_tasks(wf.workflow_id)[0]
    svc.execute_task(wf.workflow_id, task.task_id)
    assert svc.get(wf.workflow_id).status is WorkflowStatus.COMPLETED
    kb.close()


def test_skip_completes_a_workflow_with_a_reason_recorded(env):
    kb, study_id = env
    svc = _service(kb)
    wf = svc.create(study_id, template="evidence_review",
                    params={"reaction_template_id": "primary-alcohol-oxidation"})
    svc.start(wf.workflow_id)
    task = svc.detail(wf.workflow_id).tasks[0]
    svc.skip_task(wf.workflow_id, task.task_id, "not needed")
    assert svc.get(wf.workflow_id).status is WorkflowStatus.COMPLETED
    events = ResearchStudyService(kb).detail(study_id).events
    assert any(e.event_type.value == "task_skipped" and "not needed" in e.summary
               for e in events)
    kb.close()


# -- 14. no auto-run, timeline -----------------------------------------------


def test_creating_a_workflow_does_not_run_anything(env):
    kb, study_id = env
    svc = _service(kb)
    svc.create(study_id, template="molecule_modification",
               params={"substrate": "CCO",
                       "reaction_template_id": "primary-alcohol-oxidation"})
    events = [e.event_type.value for e in
              ResearchStudyService(kb).detail(study_id).events]
    assert "workflow_created" in events
    assert all(e not in events for e in ("task_started", "task_completed"))
    kb.close()


def test_timeline_records_the_workflow_lifecycle(env):
    kb, study_id = env
    svc = _service(kb)
    wf = svc.create(study_id, template="evidence_review",
                    params={"reaction_template_id": "primary-alcohol-oxidation"})
    svc.start(wf.workflow_id)
    task = svc.ready_tasks(wf.workflow_id)[0]
    svc.execute_task(wf.workflow_id, task.task_id)
    events = [e.event_type.value for e in
              ResearchStudyService(kb).detail(study_id).events]
    assert "workflow_created" in events and "workflow_started" in events
    assert "task_started" in events and "task_completed" in events
    assert "workflow_completed" in events
    kb.close()


# -- 15. API -----------------------------------------------------------------


def test_task_workflow_api(tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        study_id = c.post("/api/research/sessions", json={"title": "S"}).json()["study_id"]
        wf = c.post(f"/api/research/sessions/{study_id}/workflows", json={
            "template": "molecule_modification",
            "params": {"substrate": "CCO",
                       "reaction_template_id": "primary-alcohol-oxidation"}}).json()
        wid = wf["workflow_id"]

        detail = c.get(f"/api/research/workflows/{wid}").json()
        assert len(detail["tasks"]) == 4

        c.post(f"/api/research/workflows/{wid}/start")
        ready = c.get(f"/api/research/workflows/{wid}/ready").json()["tasks"]
        task_id = ready[0]["task_id"]
        done = c.post(f"/api/research/workflows/{wid}/tasks/{task_id}/execute").json()
        assert done["status"] == "completed"

        # a cycle is refused, and unknown ids are 404
        assert c.post(f"/api/research/sessions/{study_id}/workflows", json={
            "tasks": [{"key": "a", "task_type": "molecule_inspection", "title": "A",
                       "deps": ["b"], "inputs": {}},
                      {"key": "b", "task_type": "molecule_inspection", "title": "B",
                       "deps": ["a"], "inputs": {}}]}).status_code == 409
        assert c.get("/api/research/workflows/wf-nope").status_code == 404
        assert c.post("/api/research/workflows/wf-nope/start").status_code == 404
        assert c.post(f"/api/research/workflows/{wid}/tasks/nope/execute").status_code == 404


def test_all_builtin_templates_are_valid(env):
    kb, study_id = env
    for name in TEMPLATES:
        params = {"substrate": "CCO", "start": "CCO", "target": "CC=O",
                  "reaction_template_id": "primary-alcohol-oxidation",
                  "query": "x"}
        wf = _service(kb).create(study_id, template=name, params=params)
        assert wf.status is WorkflowStatus.READY, name
    kb.close()

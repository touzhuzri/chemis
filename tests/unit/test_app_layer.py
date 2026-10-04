"""Application layer: events, jobs, document intake (Phase 4).

What is under test is the workbench plumbing around the sealed engine:
fact events with id references (§11/§12), cooperative cancellation (§37),
restart honesty (§90), and document intake that trusts nothing the
caller says (§57-§59).
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from crea.app.events import EventLog, ResearchEvent, ResearchEventType
from crea.app.jobs import JobManager, JobStatus, ResearchCancelled
from crea.app.research import ResearchApplication
from crea.app.knowledge import KnowledgeApplication


# -- events (§11/§12) --------------------------------------------------------


def test_events_are_facts_with_id_references_and_a_jsonl_log(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    event = log.append(ResearchEvent(
        job_id="job-1", type=ResearchEventType.REACTION_CREATED,
        message="Reaction RXN-001 created", reaction_ids=["RXN-001"]))
    assert event.event_id, "events carry an identity"
    assert "believe" not in event.message and "probably" not in event.message

    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    stored = json.loads(lines[0])
    assert stored["reaction_ids"] == ["RXN-001"]
    # ids reference objects; the objects are never embedded (§12)
    assert "reaction" not in stored or isinstance(stored.get("reaction"), type(None))
    assert [e.type for e in log.for_job("job-1")] == [
        ResearchEventType.REACTION_CREATED]
    assert log.for_job("other-job") == []


# -- job lifecycle -----------------------------------------------------------


def _slow_factory(document_ids):
    """A stack factory that takes its time, so cancellation is observable."""
    time.sleep(0.4)

    def build():
        from crea.agent.offline import build_offline_workflow_stack

        return build_offline_workflow_stack()

    return build()


def test_a_job_runs_to_completion_without_blocking_the_caller(tmp_path):
    events = EventLog(None)

    def factory(document_ids):
        from crea.agent.offline import build_offline_workflow_stack

        return build_offline_workflow_stack()

    manager = JobManager(registry_path=tmp_path / "jobs.json", events=events,
                         stack_factory=factory, db_path=tmp_path / "crea.db")
    job = manager.start({"question": "研究 phenylacetylene",
                         "structure_kind": "SMILES",
                         "structure_value": "C#Cc1ccccc1"})
    assert job.status in (JobStatus.QUEUED, JobStatus.RUNNING)
    manager.wait(job.job_id, timeout=60)
    assert job.status in (JobStatus.COMPLETED, JobStatus.PARTIAL)
    assert job.session_id, "a finished run names its session"
    types = [e.type.value for e in events.for_job(job.job_id)]
    assert "research_started" in types
    assert types[-1] in ("research_completed", "research_partial")


def test_cancellation_stops_at_a_safe_boundary_and_says_so(tmp_path):
    events = EventLog(None)
    manager = JobManager(registry_path=tmp_path / "jobs.json", events=events,
                         stack_factory=_slow_factory,
                         db_path=tmp_path / "crea.db")
    job = manager.start({"question": "q", "structure_kind": "SMILES",
                         "structure_value": "C#Cc1ccccc1"})
    manager.cancel(job.job_id)  # flag set while the factory is still sleeping
    manager.wait(job.job_id, timeout=60)
    assert job.status == JobStatus.CANCELLED
    assert "取消" in job.error
    types = [e.type.value for e in events.for_job(job.job_id)]
    assert "research_cancelled" in types


def test_a_restart_marks_unfinished_jobs_interrupted_not_completed(tmp_path):
    """§90: the server died mid-run -- the job says so, loudly."""
    registry = tmp_path / "jobs.json"
    registry.write_text(json.dumps([{
        "job_id": "job-zombie", "request": {"question": "q"},
        "status": "running", "created_at": "2026-10-02T00:00:00+00:00",
        "started_at": "2026-10-02T00:00:01+00:00",
    }]), encoding="utf-8")

    def factory(document_ids):
        from crea.agent.offline import build_offline_workflow_stack

        return build_offline_workflow_stack()

    manager = JobManager(registry_path=registry, events=EventLog(None),
                         stack_factory=factory, db_path=tmp_path / "crea.db")
    job = manager.get("job-zombie")
    assert job.status == JobStatus.INTERRUPTED
    assert "中断" in job.error
    # and the registry says so after a reopen
    stored = json.loads(registry.read_text(encoding="utf-8"))
    assert stored[0]["status"] == "interrupted"


def test_a_restart_keeps_what_the_user_asked(tmp_path):
    """§一 data-flow audit: the question and structure the user submitted
    must survive a server restart in the job record. A restart that
    quietly erased them once made an honest run look like the user had
    submitted nothing."""
    registry = tmp_path / "jobs.json"

    def factory(document_ids):
        from crea.agent.offline import build_offline_workflow_stack

        return build_offline_workflow_stack()

    events = EventLog(None)
    manager = JobManager(registry_path=registry, events=events,
                         stack_factory=factory, db_path=tmp_path / "crea.db")
    job = manager.start({"question": "研究苯乙炔的合成路线",
                         "structure_kind": "SMILES",
                         "structure_value": "C#Cc1ccccc1",
                         "document_ids": []})
    manager.wait(job.job_id, timeout=60)

    # a new manager over the same file == a server restart
    manager2 = JobManager(registry_path=registry, events=events,
                          stack_factory=factory,
                          db_path=tmp_path / "crea.db")
    restored = manager2.get(job.job_id)
    assert restored.request.get("question") == "研究苯乙炔的合成路线"
    assert restored.request.get("structure_kind") == "SMILES"
    assert restored.request.get("structure_value") == "C#Cc1ccccc1"
    # and the re-persisted entry still carries them
    stored = json.loads(registry.read_text(encoding="utf-8"))
    entry = next(e for e in stored if e["job_id"] == job.job_id)
    assert entry["structure_value"] == "C#Cc1ccccc1"
    assert entry["question"] == "研究苯乙炔的合成路线"


# -- document intake (§57-§59) ----------------------------------------------


@pytest.fixture()
def research_app(tmp_path):
    def factory(document_ids):
        from crea.agent.offline import build_offline_workflow_stack

        return build_offline_workflow_stack()

    events = EventLog(None)
    manager = JobManager(registry_path=tmp_path / "jobs.json", events=events,
                         stack_factory=factory, db_path=tmp_path / "crea.db")
    knowledge = KnowledgeApplication(tmp_path / "crea.db")
    return ResearchApplication(jobs=manager, events=events,
                               documents_dir=tmp_path / "docs",
                               knowledge_app=knowledge)


def test_upload_accepts_a_real_pdf_and_names_it_by_content_hash(research_app, tmp_path):
    from tests.fakes import pdfs

    data = pdfs.make_pdf([["Experimental", "A procedure."]])
    entry = research_app.upload_document("../../../etc/passwd.pdf", data)
    # the user filename never becomes a path (§58); the content hash is the
    # identity (§59)
    assert "/" not in entry["original_name"] and ".." not in entry["original_name"]
    assert entry["document_id"]
    stored = research_app.document_path(entry["document_id"])
    assert stored.is_file()
    assert stored.parent == research_app._documents_dir
    # the same bytes are the same document (content identity)
    again = research_app.upload_document("other-name.pdf", data)
    assert again["document_id"] == entry["document_id"]


def test_upload_refuses_anything_that_is_not_a_pdf(research_app):
    with pytest.raises(ValueError, match="INVALID_FILE"):
        research_app.upload_document("fake.pdf", b"%PDF-not really a pdf")
    with pytest.raises(ValueError, match="INVALID_FILE"):
        research_app.upload_document("notes.txt", b"hello")
    with pytest.raises(ValueError, match="INVALID_FILE"):
        research_app.upload_document("empty.pdf", b"")

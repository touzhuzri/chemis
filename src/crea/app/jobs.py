"""Research jobs: asynchronous research, honestly supervised.

Phase 4 (§10/§37/§38/§90-§92). A job is one `ResearchWorkflow.run` on a
background thread. The HTTP worker never runs research itself (§101) and
never blocks on it.

Lifecycle:

    queued → running → completed | partial | cancelled
                     → interrupted   (the server died mid-run)

Cancellation (§37) is cooperative: `cancel_requested` is a flag the run
observes at its next safe boundary -- a tool step finishing -- and stops
there. Never `kill -9`, never mid-transaction: the current atomic
operation completes first, and whatever knowledge the run already
established stays in the database (§91). A cancelled run is honest about
it: the session is kept as PARTIAL, not dressed up as complete.

Restart recovery (§90): jobs that were queued/running when the process
died are marked `interrupted` at startup -- never silently completed, and
never left "running" forever. Knowledge persisted before the crash is
still there (§91); the identity/idempotency semantics of Phase 3 make a
re-run of the same research merge rather than duplicate (§92).

One thread per job, and one database connection per thread (§53 of
Phase 3.11: no connection sharing across threads). Concurrent jobs
interleave at the transaction level; the store's busy timeout and the
registries' duplicate recovery keep the knowledge graph consistent (§38).
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from pathlib import Path

from crea.core.ids import content_hash
from crea.core.provenance import utc_now_iso


class ResearchCancelled(Exception):
    """Raised at a safe boundary when a run was asked to stop (§37)."""


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    TERMINAL = (COMPLETED, PARTIAL, CANCELLED, INTERRUPTED)


class ResearchJob:
    """One unit of research work. State is facts and ids, never objects."""

    def __init__(self, request: dict, *, job_id: str | None = None) -> None:
        self.job_id = job_id or content_hash(
            f"{request.get('question', '')}|{utc_now_iso()}"
            f"|{threading.get_ident()}", 16)
        self.request = dict(request)
        self.status = JobStatus.QUEUED
        self.phase = ""
        self.session_id = ""
        self.error = ""
        self.cancel_requested = False
        self.created_at = utc_now_iso()
        self.started_at = ""
        self.finished_at = ""
        #: Filled when the run finishes: ids and renderings by reference.
        self.result: dict = {}

    def snapshot(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "question": self.request.get("question", ""),
            "phase": self.phase,
            "session_id": self.session_id,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "structure_kind": self.request.get("structure_kind"),
            "structure_value": self.request.get("structure_value", ""),
            "document_ids": list(self.request.get("document_ids") or []),
        }


class JobManager:
    """Owns research jobs: threads, the job registry, and recovery.

    `stack_factory(document_ids)` must build one research stack
    (workflow, library, evidence sink, tool registry) exactly as the CLI
    does -- the job never assembles chemistry, providers or LLMs itself.
    """

    def __init__(self, *, registry_path: Path, events, stack_factory,
                 db_path: Path) -> None:
        self._registry_path = Path(registry_path)
        self._db_path = Path(db_path)
        self._events = events
        self._stack_factory = stack_factory
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._jobs: dict[str, ResearchJob] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._load_registry()
        self.recover_interrupted()

    # -- registry persistence (jobs.json, atomic writes) --------------------

    def _load_registry(self) -> None:
        if not self._registry_path.is_file():
            return
        try:
            raw = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for entry in raw if isinstance(raw, list) else []:
            # The snapshot stores the request flattened; rebuild it exactly.
            # Losing it here once meant a restart quietly erased what the
            # user had asked -- the question and structure disappeared from
            # the record even though the run had used them (§一).
            request = dict(entry.get("request") or {})
            for key in ("question", "structure_kind", "structure_value",
                        "document_ids"):
                if not request.get(key) and entry.get(key):
                    request[key] = entry[key]
            job = ResearchJob(request, job_id=entry.get("job_id"))
            job.status = entry.get("status", JobStatus.QUEUED)
            job.phase = entry.get("phase", "")
            job.session_id = entry.get("session_id", "")
            job.error = entry.get("error", "")
            job.cancel_requested = bool(entry.get("cancel_requested"))
            job.created_at = entry.get("created_at", "")
            job.started_at = entry.get("started_at", "")
            job.finished_at = entry.get("finished_at", "")
            job.result = entry.get("result") or {}
            self._jobs[job.job_id] = job

    def _write_registry(self) -> None:
        entries = []
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at)
            for job in jobs:
                entry = job.snapshot()
                entry["result"] = job.result
                entries.append(entry)
        # Serialized writes, unique temp names: concurrent jobs and the HTTP
        # thread all persist state, and two writers racing on one temp file
        # would strand a job half-written (§38).
        with self._write_lock:
            self._registry_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._registry_path.with_name(
                f".{self._registry_path.name}.{os.getpid()}."
                f"{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self._registry_path)

    def recover_interrupted(self) -> None:
        """A server restart turns unfinished work into a stated fact (§90).

        queued/running jobs cannot have survived the process: they are
        marked `interrupted` -- never "completed", never left running.
        Whatever they persisted before the crash is still in the knowledge
        base (§91).
        """
        changed = False
        with self._lock:
            for job in self._jobs.values():
                if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                    job.status = JobStatus.INTERRUPTED
                    job.finished_at = job.finished_at or utc_now_iso()
                    if not job.error:
                        job.error = ("服务器重启导致研究中断；已持久化的知识保留"
                                     "（INTERRUPTED，未假装完成）")
                    changed = True
        if changed:
            self._write_registry()

    # -- lifecycle ----------------------------------------------------------

    def start(self, request: dict) -> ResearchJob:
        job = ResearchJob(request)
        with self._lock:
            self._jobs[job.job_id] = job
        self._write_registry()
        self._events.append(_event(job, "research_started",
                                   f"研究开始：{request.get('question', '')}",
                                   document_ids=list(request.get(
                                       "document_ids") or [])))
        thread = threading.Thread(target=self._run, args=(job,),
                                  name=f"research-{job.job_id[:8]}",
                                  daemon=True)
        with self._lock:
            self._threads[job.job_id] = thread
        thread.start()
        return job

    def cancel(self, job_id: str) -> ResearchJob:
        """Request a cooperative stop (§37). The flag, not a kill.

        A job that already finished says so; a running job stops at its
        next safe boundary with its persisted knowledge intact.
        """
        job = self.get(job_id)
        if job.status in JobStatus.TERMINAL:
            return job
        job.cancel_requested = True
        self._write_registry()
        return job

    def get(self, job_id: str) -> ResearchJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"no research job {job_id!r}")
        return job

    def list(self) -> list[ResearchJob]:
        with self._lock:
            return sorted(self._jobs.values(),
                          key=lambda j: j.created_at, reverse=True)

    def wait(self, job_id: str, timeout: float = 30.0) -> ResearchJob:
        """Join a job's thread (for tests and the demo; not used by HTTP)."""
        with self._lock:
            thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout=timeout)
        return self.get(job_id)

    # -- the worker ---------------------------------------------------------

    def _run(self, job: ResearchJob) -> None:
        from crea.core.models.identity import StructureInput

        try:
            job.status = JobStatus.RUNNING
            job.started_at = utc_now_iso()
            self._write_registry()
            document_ids = list(job.request.get("document_ids") or [])
            workflow, _library, _sink, _registry = self._stack_factory(
                document_ids)
            for document_id in document_ids:
                self._events.append(_event(
                    job, "document_parsed",
                    f"文档已解析：{document_id}", document_ids=[document_id]))

            def on_phase(name: str) -> None:
                job.phase = name
                if job.cancel_requested:
                    raise ResearchCancelled(job.job_id)

            workflow.on_phase = on_phase

            from crea.core.models.research import ResearchTask

            structure_input = None
            kind = job.request.get("structure_kind")
            value = job.request.get("structure_value") or ""
            if kind and value:
                structure_input = StructureInput.of(kind=kind, value=value)
            task = ResearchTask(
                task_id=job.job_id, question=job.request.get("question", ""))
            report = workflow.run(
                task,
                structure_input=structure_input,
                document_id=document_ids[0] if document_ids else None,
            )
            session = workflow.session
            job.session_id = session.session_id if session is not None else ""
            self._record_result(job, report, session, registry=_registry)
            self._emit_result_events(job, report)
            finished_ok = (session is not None
                           and session.status.value == "completed")
            job.status = (JobStatus.COMPLETED if finished_ok
                          else JobStatus.PARTIAL)
            job.error = "; ".join(task.errors[:3])
            self._events.append(_event(
                job, ("research_completed" if finished_ok
                      else "research_partial"),
                f"研究结束：{job.status}", session_id=job.session_id))
        except ResearchCancelled:
            job.status = JobStatus.CANCELLED
            job.error = "用户取消；当前原子操作完成后停止，已持久化的知识保留"
            self._events.append(_event(
                job, "research_cancelled", "研究已取消（协作式停止）",
                session_id=job.session_id))
            self._persist_session_quietly(job)
        except Exception as exc:  # noqa: BLE001 - a crashed run is PARTIAL
            job.status = JobStatus.PARTIAL
            job.error = f"{type(exc).__name__}: {exc}"
            self._events.append(_event(
                job, "research_partial",
                f"研究未完整完成：{job.error}", session_id=job.session_id))
            traceback.print_exc()
            self._persist_session_quietly(job)
        finally:
            job.finished_at = utc_now_iso()
            self._write_registry()

    def _persist_session_quietly(self, job: ResearchJob) -> None:
        """Keep the transcript of an interrupted/cancelled run (§90)."""
        session = getattr(job, "_session", None)
        if session is None:
            return
        try:
            from crea.services.knowledge_base import KnowledgeBase

            with KnowledgeBase(self._db_path) as kb:
                kb.save_session(session)
        except Exception:  # noqa: BLE001 - best effort, never masks the stop
            pass

    def _record_result(self, job, report, session, registry=None) -> None:
        """Ids and renderings by reference (§60): the job result points at
        knowledge; it never copies objects into itself.

        The identity resolution is captured here, at run time, and travels
        with the result: the report must say what the identity layer
        actually established (RESOLVED / INVALID / ...), never fall back to
        "未附带身份解析结果" because a renderer was handed no resolution.
        """
        knowledge = getattr(report, "knowledge", None)
        job._session = session
        coverage = knowledge.coverage if knowledge else None
        resolution = getattr(getattr(registry, "identity", None),
                             "resolution", None)
        job.result = {
            "session_id": session.session_id if session else "",
            "status": session.status.value if session else "",
            "identity": (resolution.model_dump(mode="json")
                         if resolution is not None else None),
            "report_markdown": None,  # rendered by the application layer
            "coverage": ({
                "entity_id": coverage.entity_id,
                "facets": [{"facet": f.facet.value, "state": f.state.value,
                            "detail": f.detail,
                            "object_ids": list(f.object_ids)}
                           for f in coverage.facets],
            } if coverage is not None else None),
            "knowledge_reused": (dict(knowledge.existing) if knowledge else {}),
            "knowledge_added": (dict(knowledge.new) if knowledge else {}),
            "knowledge_gaps": ([g.model_dump(mode="json")
                                for g in knowledge.gaps]
                               if knowledge else []),
            "conflicts": ([c.model_dump(mode="json")
                           for c in knowledge.conflicts]
                          if knowledge else []),
            "entity_ids": list(session.chemical_entity_ids) if session else [],
            "mention_ids": (list(session.chemical_mention_ids)
                            if session else []),
            "reaction_ids": list(session.reaction_ids) if session else [],
            "evidence_ids": list(session.evidence_ids) if session else [],
            "route_ids": list(session.route_ids) if session else [],
            "report": report.model_dump(mode="json"),
        }

    def _emit_result_events(self, job, report) -> None:
        knowledge = getattr(report, "knowledge", None)
        if knowledge is None:
            self._events.append(_event(
                job, "report_generated", "Research report generated",
                session_id=job.session_id))
            return
        for entity_id in knowledge.new.get("entities") or []:
            self._events.append(_event(
                job, "identity_resolved",
                f"Entity {entity_id} established", entity_ids=[entity_id]))
        for reaction_id in knowledge.new.get("reactions") or []:
            self._events.append(_event(
                job, "reaction_created", f"Reaction {reaction_id} created",
                reaction_ids=[reaction_id]))
        for evidence_id in knowledge.new.get("evidence") or []:
            self._events.append(_event(
                job, "evidence_added", f"Evidence {evidence_id} added",
                evidence_ids=[evidence_id]))
        for gap in knowledge.gaps:
            self._events.append(_event(
                job, "knowledge_gap_found",
                f"Knowledge gap: {gap.reason}"))
        for conflict in knowledge.conflicts:
            self._events.append(_event(
                job, "conflict_detected",
                f"Conflict detected: {conflict.field}",
                reaction_ids=[o.reaction_id for o in conflict.observations
                              if o.reaction_id]))
        self._events.append(_event(
            job, "report_generated", "Research report generated",
            session_id=job.session_id))


def _event(job: ResearchJob, event_type: str, message: str, *,
           entity_ids=(), reaction_ids=(), evidence_ids=(), paper_ids=(),
           document_ids=(), route_ids=(), session_id: str = ""):
    from crea.app.events import ResearchEvent, ResearchEventType

    return ResearchEvent(
        job_id=job.job_id,
        session_id=session_id or job.session_id,
        type=ResearchEventType(event_type),
        message=message,
        entity_ids=list(entity_ids),
        reaction_ids=list(reaction_ids),
        evidence_ids=list(evidence_ids),
        paper_ids=list(paper_ids),
        document_ids=list(document_ids),
        route_ids=list(route_ids),
    )


__all__ = ["JobManager", "JobStatus", "ResearchCancelled", "ResearchJob"]

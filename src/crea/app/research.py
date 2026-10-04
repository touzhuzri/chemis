"""Research use cases: start research, watch it, read what it found.

Phase 4 (§10/§57-§60). The workbench's write side is small on purpose:
**start**, **cancel**, **upload a document**, and read the job/session
views. All the chemistry runs inside `ResearchWorkflow` on a job thread;
this module only orchestrates and observes (§41: the HTTP layer never
calls an LLM -- the chain is API → application → ResearchWorkflow →
LLMProvider).

Document intake (§57-§59) refuses to trust the caller: the bytes are
validated as a real PDF by the existing document pipeline (magic
signature, size limits), the content hash IS the identity, and the
stored filename is generated -- a user filename never becomes a path.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from crea.app.dto import (
    GapView,
    JobView,
    KnowledgeStateView,
    SessionDetail,
    StartResearchRequest,
)
from crea.app.events import EventLog
from crea.app.jobs import JobManager, JobStatus


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class ResearchApplication:
    """Start/cancel/observe research, and take documents in."""

    def __init__(self, *, jobs: JobManager, events: EventLog,
                 documents_dir: Path, knowledge_app) -> None:
        self._jobs = jobs
        self._events = events
        self._documents_dir = Path(documents_dir)
        self._documents_dir.mkdir(parents=True, exist_ok=True)
        self._knowledge = knowledge_app
        self._registry_path = self._documents_dir / "documents.json"

    # -- documents (§57-§59) -----------------------------------------------

    def upload_document(self, filename: str, data: bytes) -> dict:
        """Validate and store one uploaded document.

        Checks, in this order: size (the pipeline's own limit), PDF magic
        signature, parseability. Never the filename, never the
        Content-Type header alone (§57). The stored file is named by its
        content hash (§59); the original name is metadata, sanitized and
        never used as a path (§58).
        """
        from crea.services.documents import LocalDocumentService
        from crea.sources.pdf import PypdfDocumentParser

        if not data:
            raise ValueError("INVALID_FILE: empty upload")
        safe_name = _SAFE_NAME.sub("_", Path(filename or "upload.pdf").name)
        if not safe_name.lower().endswith(".pdf"):
            raise ValueError("INVALID_FILE: only .pdf documents are accepted")
        tmp = self._documents_dir / f".upload-{os.getpid()}-{time.time_ns()}.pdf"
        try:
            tmp.write_bytes(data)
            service = LocalDocumentService(PypdfDocumentParser())
            parsed = service.ingest(tmp)  # magic bytes, size, parseability
        except Exception as exc:  # noqa: BLE001 - structured, never leaked raw
            raise ValueError(f"INVALID_FILE: {exc}") from exc
        finally:
            if tmp.exists():
                tmp.unlink()

        document_id = parsed.document.document_id
        stored = self._documents_dir / f"{document_id}.pdf"
        if not stored.exists():
            stored.write_bytes(data)
        from crea.core.provenance import utc_now_iso

        entry = {
            "document_id": document_id,
            "original_name": safe_name,
            "size_bytes": len(data),
            "media_type": "application/pdf",
            "uploaded_at": utc_now_iso(),
        }
        registry = self._load_documents()
        registry[document_id] = entry
        self._write_documents(registry)
        return entry

    def documents(self) -> list[dict]:
        registry = self._load_documents()
        return [registry[key] for key in sorted(registry)]

    def document_path(self, document_id: str) -> Path:
        registry = self._load_documents()
        if document_id not in registry:
            raise KeyError(f"no document {document_id!r}")
        return self._documents_dir / f"{document_id}.pdf"

    def _load_documents(self) -> dict:
        if not self._registry_path.is_file():
            return {}
        try:
            return json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_documents(self, registry: dict) -> None:
        tmp = self._registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(registry, ensure_ascii=False, indent=2,
                                  sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._registry_path)

    # -- research jobs ------------------------------------------------------

    def start(self, request: StartResearchRequest) -> JobView:
        for document_id in request.document_ids:
            self.document_path(document_id)  # KeyError -> unknown document
        job = self._jobs.start({
            "question": request.question,
            "structure_kind": request.structure_kind,
            "structure_value": request.structure_value,
            "document_ids": list(request.document_ids),
        })
        return self.job_view(job)

    def cancel(self, job_id: str) -> JobView:
        try:
            job = self._jobs.cancel(job_id)
        except KeyError as exc:
            raise KeyError(str(exc)) from exc
        return self.job_view(job)

    def job_view(self, job) -> JobView:
        snapshot = job.snapshot()
        return JobView(**snapshot)

    def job(self, job_id: str) -> JobView:
        try:
            return self.job_view(self._jobs.get(job_id))
        except KeyError as exc:
            raise KeyError(str(exc)) from exc

    def jobs(self) -> list[JobView]:
        return [self.job_view(job) for job in self._jobs.list()]

    def events(self, job_id: str) -> list[dict]:
        self._jobs.get(job_id)  # unknown id -> KeyError
        return [e.model_dump(mode="json")
                for e in self._events.for_job(job_id)]

    def report(self, job_id: str) -> dict:
        job = self._jobs.get(job_id)
        result = dict(job.result or {})
        if "report" not in result:
            return {"status": job.status, "report": None,
                    "report_markdown": ""}
        markdown = self._render(result)
        return {"status": job.status, "report": result["report"],
                "report_markdown": markdown}

    def _render(self, result: dict) -> str:
        """Render Report v3 exactly as the run established it.

        The identity resolution captured at run time and the run's own
        session travel into the renderer (§八): the report must agree with
        the API and the UI about identity -- never "未附带身份解析结果"
        over a resolved structure.
        """
        from crea.core.models.identity import ChemicalIdentityResolution
        from crea.core.models.research import ResearchReport
        from crea.report.research import render_research_report_v3

        report = ResearchReport.model_validate(result["report"])
        identity = None
        if result.get("identity"):
            try:
                identity = ChemicalIdentityResolution.model_validate(
                    result["identity"])
            except Exception:  # noqa: BLE001 - render without rather than fail
                identity = None
        session = None
        session_id = result.get("session_id") or ""
        if session_id:
            try:
                session = self._knowledge.session_model(session_id)
            except KeyError:
                session = None
        return render_research_report_v3(report, session=session,
                                         identity=identity)

    def session_detail(self, session_id: str) -> SessionDetail:
        """The full workspace picture of one run (§16/§60).

        Everything by reference: ids for knowledge objects, the rendered
        report as text, the knowledge split into before/reused/added.
        """
        session = self._knowledge.session(session_id)
        job_view = None
        for job in self._jobs.list():
            if job.session_id == session_id:
                job_view = self.job_view(job)
                break
        result = {}
        if job_view is not None:
            try:
                result = self._jobs.get(job_view.job_id).result or {}
            except KeyError:
                result = {}
        coverage = result.get("coverage")
        knowledge_before = None
        if coverage:
            knowledge_before = KnowledgeStateView(
                entity_id=coverage.get("entity_id"),
                facets=coverage.get("facets") or [],
                gaps=result.get("knowledge_gaps") or [],
                conflicts=result.get("conflicts") or [])
        reused = result.get("knowledge_reused") or {}
        added = result.get("knowledge_added") or {}
        gaps = result.get("knowledge_gaps") or []
        conflicts = result.get("conflicts") or []
        report_markdown = self._render(result) if result.get("report") else ""
        identity_view = _identity_view(result.get("identity"))
        if (identity_view is not None and not identity_view.get("entity_id")
                and session.entity_ids):
            # the entity this run's identity established, as the session
            # recorded it (§17): the card names its entity, not just its
            # structure
            identity_view["entity_id"] = session.entity_ids[0]
        analysis = []
        if result.get("report"):
            for finding in result["report"].get("findings") or []:
                if finding.get("kind") == "computed":
                    analysis.append(finding.get("text", ""))
        return SessionDetail(
            session_id=session.session_id,
            question=session.question,
            status=session.status,
            created_at=session.created_at,
            phases=list(session.phases),
            entity_ids=list(session.entity_ids),
            mention_ids=list(session.mention_ids),
            reaction_ids=list(session.reaction_ids),
            evidence_ids=list(session.evidence_ids),
            route_ids=list(session.route_ids),
            source_ids=list(session.source_ids),
            identity=identity_view,
            analysis=analysis,
            knowledge_before=knowledge_before,
            knowledge_gaps=[GapView(**g) if isinstance(g, dict) else g
                            for g in gaps],
            knowledge_reused=reused,
            knowledge_added=added,
            conflicts=([_conflict_from(c) for c in conflicts]
                       if conflicts and isinstance(conflicts[0], dict)
                       else conflicts),
            report_markdown=report_markdown,
            job_id=job_view.job_id if job_view else "")


def _identity_view(resolution: dict | None) -> dict | None:
    """The identity card data (§17), straight from the resolution."""
    if not resolution:
        return None
    selected = resolution.get("selected") or {}
    return {
        "status": resolution.get("status", ""),
        "reason": resolution.get("reason", ""),
        "input_kind": (resolution.get("input") or {}).get("kind", ""),
        "input_value": (resolution.get("input") or {}).get("value", ""),
        "canonical_smiles": selected.get("canonical_smiles", ""),
        "inchi": selected.get("inchi", ""),
        "inchikey": selected.get("inchikey", ""),
        "names": [n for n in [selected.get("input_name"),
                              selected.get("candidate_name")]
                  if n] + list(selected.get("synonyms") or []),
        "entity_id": resolution.get("entity_id"),
    }


def _conflict_from(raw: dict):
    from crea.app.dto import ConflictObservationView, ConflictView

    observations = [
        ConflictObservationView(**{
            k: v for k, v in obs.items()
            if k in ConflictObservationView.model_fields})
        for obs in raw.get("observations") or []]
    return ConflictView(
        field=raw.get("field", ""), kind=raw.get("kind", "conflict"),
        note=raw.get("note", ""), observations=observations)


__all__ = ["ResearchApplication"]

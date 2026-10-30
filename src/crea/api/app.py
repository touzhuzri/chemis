"""The CREA workbench HTTP API.

Phase 4 (§5/§39-§42). FastAPI over the application layer, nothing more:
routes translate HTTP into use-case calls and DTOs back into JSON. No
SQL, no RDKit, no LLM, no provider is reachable from this file except
through `crea.app` (§2/§39/§41).

Read-only where knowledge is concerned (§93/§94): evidence, reactions
and records have GET endpoints and no update endpoints. Errors are
structured (`{"error": {"code", "message", "detail"}}`) -- an RDKit or
SQLite exception never reaches a client (§56). And when the built
frontend exists, this same server serves it (§103): one process, one
origin, `python -m crea.cli serve`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from crea.api.dependencies import WorkbenchContext, create_context
from crea.api.errors import (
    ApiError,
    conformer_failed,
    invalid_molecule,
    invalid_request,
    invalid_structure,
    molecule_not_found,
    not_found,
)
from crea.core.errors import ResearchStudyError


def create_app(*, context: WorkbenchContext | None = None, db_path=None,
               documents_dir=None, offline: bool = False,
               stack_factory=None, web_root: Path | None = None) -> FastAPI:
    """One workbench server. Testable in-process: contexts and stack
    factories are injectable, and nothing here needs a network."""
    ctx = context or create_context(db_path=db_path,
                                    documents_dir=documents_dir,
                                    offline=offline,
                                    stack_factory=stack_factory)
    app = FastAPI(title="CREA — Chemical Research Workbench",
                  version="4.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.ctx = ctx

    # -- structured errors (§56) -------------------------------------------

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status_code, content=exc.body())

    @app.exception_handler(KeyError)
    async def _key_error(_request: Request, exc: KeyError):
        return JSONResponse(status_code=404,
                            content=not_found(str(exc)).body())

    @app.exception_handler(ValueError)
    async def _value_error(_request: Request, exc: ValueError):
        message = str(exc)
        if "INVALID_QUERY" in message:
            return JSONResponse(
                status_code=400,
                content=invalid_request("查询条件无效", message).body())
        return JSONResponse(status_code=400,
                            content=invalid_request(message).body())

    @app.exception_handler(Exception)
    async def _internal(_request: Request, exc: Exception):
        # A failure is a stated failure -- never an exception name and
        # never a stack trace in a response body.
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL",
                               "message": "服务内部错误",
                               "detail": type(exc).__name__}})

    # -- system -------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "service": "crea-workbench",
                "db": str(ctx.db_path), "offline": ctx.offline}

    # -- documents (§57-§59) -------------------------------------------------

    @app.post("/api/documents")
    def upload_document(file: UploadFile = File(...)) -> dict:
        data = file.file.read()
        try:
            return ctx.research.upload_document(file.filename or "upload.pdf",
                                                data)
        except ValueError as exc:
            raise ApiError("INVALID_FILE", str(exc), status_code=400) from exc

    @app.get("/api/documents")
    def list_documents() -> dict:
        return {"documents": ctx.research.documents()}

    # -- research sessions (Stage 7.1) --------------------------------------
    # Declared before `/api/research/{job_id}` so the greedy path parameter
    # does not swallow `/sessions`. A research session is a process container:
    # references point at existing objects, events form the timeline, artifacts
    # hold AI results.

    @app.post("/api/research/sessions")
    def create_study(payload: dict | None = None) -> dict:
        body = dict(payload or {})
        study = ctx.research_study.create(
            title=str(body.get("title") or ""),
            question=str(body.get("question") or ""),
            start_molecule_id=str(body.get("start_molecule_id") or ""),
            target_molecule_id=str(body.get("target_molecule_id") or ""))
        return study.model_dump(mode="json")

    @app.get("/api/research/sessions")
    def list_studies(status: str | None = None,
                     limit: int = Query(default=100, ge=1, le=500),
                     offset: int = Query(default=0, ge=0)) -> dict:
        from crea.core.models.research_study import StudyStatus

        parsed = None
        if status:
            try:
                parsed = StudyStatus(status)
            except ValueError as exc:
                raise invalid_request(f"unknown status {status!r}") from exc
        studies = ctx.research_study.list(status=parsed, limit=limit, offset=offset)
        return {"sessions": [s.model_dump(mode="json") for s in studies],
                "count": len(studies)}

    @app.get("/api/research/sessions/{study_id}")
    def get_study(study_id: str) -> dict:
        from crea.core.errors import ResearchStudyError

        try:
            return ctx.research_study.detail(study_id).model_dump(mode="json")
        except ResearchStudyError as exc:
            raise not_found(exc.message) from exc

    @app.patch("/api/research/sessions/{study_id}")
    def update_study(study_id: str, payload: dict | None = None) -> dict:
        from crea.core.errors import ResearchStudyError
        from crea.core.models.research_study import StudyStatus

        body = dict(payload or {})
        status = None
        if body.get("status"):
            try:
                status = StudyStatus(str(body["status"]))
            except ValueError as exc:
                raise invalid_request(f"unknown status {body['status']!r}") from exc
        try:
            study = ctx.research_study.update(
                study_id, title=body.get("title"), status=status)
        except ResearchStudyError as exc:
            raise _study_error(exc) from exc
        return study.model_dump(mode="json")

    @app.post("/api/research/sessions/{study_id}/references")
    def add_study_reference(study_id: str, payload: dict | None = None) -> dict:
        from crea.core.errors import ResearchStudyError
        from crea.core.models.research_study import ReferenceKind

        body = dict(payload or {})
        try:
            kind = ReferenceKind(str(body.get("kind") or ""))
        except ValueError as exc:
            raise invalid_request(
                "kind must be molecule|reaction|literature|evidence") from exc
        object_id = str(body.get("object_id") or "").strip()
        if not object_id:
            raise invalid_request("需要 object_id")
        try:
            reference = ctx.research_study.add_reference(
                study_id, kind, object_id, label=str(body.get("label") or ""),
                note=str(body.get("note") or ""))
        except ResearchStudyError as exc:
            raise _study_error(exc) from exc
        return reference.model_dump(mode="json")

    @app.delete("/api/research/sessions/{study_id}/references/{reference_id}")
    def remove_study_reference(study_id: str, reference_id: str) -> dict:
        removed = ctx.research_study.remove_reference(study_id, reference_id)
        return {"removed": removed}

    @app.post("/api/research/sessions/{study_id}/events")
    def append_study_event(study_id: str, payload: dict | None = None) -> dict:
        from crea.core.errors import ResearchStudyError
        from crea.core.models.research_study import StudyEventType

        body = dict(payload or {})
        kind = str(body.get("event_type") or "note_added")
        try:
            event_type = StudyEventType(kind)
        except ValueError as exc:
            raise invalid_request(f"unknown event_type {kind!r}") from exc
        try:
            if event_type is StudyEventType.NOTE_ADDED:
                event = ctx.research_study.add_note(
                    study_id, str(body.get("note") or body.get("summary") or ""))
            else:
                event = ctx.research_study.append_event(
                    study_id, event_type,
                    summary=str(body.get("summary") or ""),
                    reference_id=str(body.get("reference_id") or ""),
                    artifact_id=str(body.get("artifact_id") or ""))
        except ResearchStudyError as exc:
            raise _study_error(exc) from exc
        return event.model_dump(mode="json")

    @app.post("/api/research/sessions/{study_id}/artifacts")
    def add_study_artifact(study_id: str, payload: dict | None = None) -> dict:
        from crea.core.errors import ResearchStudyError
        from crea.core.models.research_study import ArtifactType

        body = dict(payload or {})
        try:
            artifact_type = ArtifactType(str(body.get("artifact_type") or ""))
        except ValueError as exc:
            raise invalid_request("artifact_type 无效") from exc
        content = body.get("content") or {}
        if not isinstance(content, dict):
            raise invalid_request("content 必须是对象")
        try:
            artifact = ctx.research_study.add_artifact(
                study_id, artifact_type, content,
                label=str(body.get("label") or ""))
        except ResearchStudyError as exc:
            raise _study_error(exc) from exc
        return artifact.model_dump(mode="json")

    # -- final audit (Stage 7.4) --------------------------------------------
    # A deterministic, offline consistency audit of a session (and one of its
    # reports). It reports; it never repairs.

    @app.post("/api/research/sessions/{study_id}/audit")
    def run_audit(study_id: str, payload: dict | None = None) -> dict:
        from crea.core.errors import ResearchStudyError

        report_artifact_id = str((payload or {}).get("report_artifact_id") or "")
        try:
            return ctx.final_audit.run(study_id, report_artifact_id)
        except ResearchStudyError as exc:
            raise not_found(exc.message) from exc

    @app.get("/api/research/sessions/{study_id}/audit")
    def latest_audit(study_id: str) -> dict:
        from crea.core.errors import ResearchStudyError

        try:
            audit = ctx.final_audit.latest(study_id)
        except ResearchStudyError as exc:
            raise not_found(exc.message) from exc
        if audit is None:
            raise not_found(f"no audit for session {study_id!r}")
        return audit

    @app.get("/api/research/sessions/{study_id}/audit/{artifact_id}")
    def get_audit(study_id: str, artifact_id: str) -> dict:
        audit = ctx.final_audit.get(study_id, artifact_id)
        if audit is None:
            raise not_found(f"no audit {artifact_id!r} for session {study_id!r}")
        return audit

    # -- research report (Stage 7.3) ----------------------------------------
    # A readable expression of a session, assembled read-only from what it
    # holds and stored as an artifact. No search, agent or LLM is re-run.

    @app.post("/api/research/sessions/{study_id}/report")
    def generate_report(study_id: str) -> dict:
        from crea.core.errors import ResearchStudyError

        try:
            return ctx.research_report.generate(study_id)
        except ResearchStudyError as exc:
            raise not_found(exc.message) from exc

    @app.get("/api/research/sessions/{study_id}/report")
    def latest_report(study_id: str) -> dict:
        from crea.core.errors import ResearchStudyError

        try:
            report = ctx.research_report.latest(study_id)
        except ResearchStudyError as exc:
            raise not_found(exc.message) from exc
        if report is None:
            raise not_found(f"no report for session {study_id!r}")
        return report

    @app.get("/api/research/sessions/{study_id}/report/{artifact_id}")
    def get_report(study_id: str, artifact_id: str) -> dict:
        report = ctx.research_report.get(study_id, artifact_id)
        if report is None:
            raise not_found(f"no report {artifact_id!r} for session {study_id!r}")
        return report

    # -- research workflows (Stage 7.2) -------------------------------------
    # A bounded, explicit task DAG over a session. Declared before the greedy
    # `/api/research/{job_id}` route. Tasks run one at a time, on request.

    @app.post("/api/research/sessions/{study_id}/workflows")
    def create_workflow(study_id: str, payload: dict | None = None) -> dict:
        from crea.core.errors import ResearchWorkflowError

        body = dict(payload or {})
        try:
            workflow = ctx.task_workflow.create(
                study_id, template=str(body.get("template") or ""),
                tasks=body.get("tasks"), title=str(body.get("title") or ""),
                params=body.get("params") or {})
        except ResearchWorkflowError as exc:
            raise _workflow_error(exc) from exc
        return workflow.model_dump(mode="json")

    @app.get("/api/research/sessions/{study_id}/workflows")
    def list_workflows(study_id: str) -> dict:
        workflows = ctx.task_workflow.list(study_id)
        return {"workflows": [w.model_dump(mode="json") for w in workflows],
                "count": len(workflows)}

    @app.get("/api/research/workflows/{workflow_id}")
    def get_workflow(workflow_id: str) -> dict:
        from crea.core.errors import ResearchWorkflowError

        try:
            return ctx.task_workflow.detail(workflow_id).model_dump(mode="json")
        except ResearchWorkflowError as exc:
            raise _workflow_error(exc) from exc

    @app.get("/api/research/workflows/{workflow_id}/ready")
    def workflow_ready(workflow_id: str) -> dict:
        from crea.core.errors import ResearchWorkflowError

        try:
            tasks = ctx.task_workflow.ready_tasks(workflow_id)
        except ResearchWorkflowError as exc:
            raise _workflow_error(exc) from exc
        return {"tasks": [t.model_dump(mode="json") for t in tasks]}

    @app.post("/api/research/workflows/{workflow_id}/start")
    def start_workflow(workflow_id: str) -> dict:
        return _workflow_action(ctx.task_workflow.start, workflow_id)

    @app.post("/api/research/workflows/{workflow_id}/pause")
    def pause_workflow(workflow_id: str) -> dict:
        return _workflow_action(ctx.task_workflow.pause, workflow_id)

    @app.post("/api/research/workflows/{workflow_id}/resume")
    def resume_workflow(workflow_id: str) -> dict:
        return _workflow_action(ctx.task_workflow.resume, workflow_id)

    @app.post("/api/research/workflows/{workflow_id}/cancel")
    def cancel_workflow(workflow_id: str) -> dict:
        return _workflow_action(ctx.task_workflow.cancel, workflow_id)

    @app.post("/api/research/workflows/{workflow_id}/tasks/{task_id}/execute")
    def execute_workflow_task(workflow_id: str, task_id: str) -> dict:
        from crea.core.errors import ResearchWorkflowError

        try:
            task = ctx.task_workflow.execute_task(workflow_id, task_id)
        except ResearchWorkflowError as exc:
            raise _workflow_error(exc) from exc
        return task.model_dump(mode="json")

    @app.post("/api/research/workflows/{workflow_id}/tasks/{task_id}/skip")
    def skip_workflow_task(workflow_id: str, task_id: str,
                           payload: dict | None = None) -> dict:
        from crea.core.errors import ResearchWorkflowError

        reason = str((payload or {}).get("reason") or "")
        try:
            task = ctx.task_workflow.skip_task(workflow_id, task_id, reason)
        except ResearchWorkflowError as exc:
            raise _workflow_error(exc) from exc
        return task.model_dump(mode="json")

    # -- AI research intelligence (M2.0) -------------------------------------
    # The formal AI-driven research entry. Registered *before* the
    # `/api/research/{job_id}` routes so `/api/research/intelligence` is
    # read as this path and not as a job id.

    @app.post("/api/research/intelligence")
    def start_ai_research(payload: dict) -> dict:
        """Research one natural-language goal with the model deciding.

        Runs synchronously and returns the whole run: the decisions the
        model made, the tools it chose, what they returned, its
        interpretations, and why it stopped. When it paused on a question,
        `awaiting` carries the question and `session_id` can be used to
        answer it.
        """
        body = payload or {}
        goal = str(body.get("goal") or "").strip()
        if not goal:
            raise invalid_request("研究目标不能为空", "goal")
        try:
            return ctx.research_intelligence.start(
                goal,
                study_id=str(body.get("study_id") or ""),
                approvals=[str(a) for a in (body.get("approvals") or [])],
                document_ids=[str(d) for d in (body.get("document_ids") or [])],
            )
        except ResearchStudyError as exc:
            raise _intelligence_error(exc) from exc

    @app.get("/api/research/intelligence/{session_id}")
    def get_ai_research(session_id: str) -> dict:
        try:
            return ctx.research_intelligence.get(session_id)
        except ResearchStudyError as exc:
            raise _intelligence_error(exc) from exc

    @app.get("/api/research/intelligence/{session_id}/decisions")
    def ai_research_decisions(session_id: str) -> dict:
        """Every turn, accepted or rejected -- the audit trail."""
        try:
            return {"decisions": ctx.research_intelligence.decisions(session_id)}
        except ResearchStudyError as exc:
            raise _intelligence_error(exc) from exc

    @app.get("/api/research/intelligence/{session_id}/report")
    def ai_research_report(session_id: str) -> dict:
        try:
            return {"report": ctx.research_intelligence.report(session_id)}
        except ResearchStudyError as exc:
            raise _intelligence_error(exc) from exc

    @app.post("/api/research/intelligence/{session_id}/answer")
    def answer_ai_research(session_id: str, payload: dict) -> dict:
        """Continue a paused run with the person's answers."""
        body = payload or {}
        try:
            return ctx.research_intelligence.answer(
                session_id,
                answers=dict(body.get("answers") or {}),
                approvals=[str(a) for a in (body.get("approvals") or [])])
        except ResearchStudyError as exc:
            raise _intelligence_error(exc) from exc

    @app.post("/api/research/intelligence/{session_id}/cancel")
    def cancel_ai_research(session_id: str) -> dict:
        """Ask the run to stop before its next decision (cooperative)."""
        try:
            return ctx.research_intelligence.cancel(session_id)
        except ResearchStudyError as exc:
            raise _intelligence_error(exc) from exc

    # -- research jobs -------------------------------------------------------

    @app.post("/api/research")
    def start_research(payload: dict) -> dict:
        from crea.app.dto import StartResearchRequest

        try:
            request = StartResearchRequest.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - pydantic errors, structured
            raise invalid_request("研究请求参数无效", str(exc)) from exc
        if request.structure_kind and request.structure_value:
            _validate_structure(request.structure_kind,
                                request.structure_value)
        job = ctx.research.start(request)
        return job.model_dump(mode="json")

    @app.get("/api/research")
    def list_research() -> dict:
        return {"jobs": [j.model_dump(mode="json")
                         for j in ctx.research.jobs()]}

    @app.get("/api/research/{job_id}")
    def get_research(job_id: str) -> dict:
        return ctx.research.job(job_id).model_dump(mode="json")

    @app.get("/api/research/{job_id}/events")
    def research_events(job_id: str) -> dict:
        return {"events": ctx.research.events(job_id)}

    @app.get("/api/research/{job_id}/report")
    def research_report(job_id: str) -> dict:
        return ctx.research.report(job_id)

    @app.post("/api/research/{job_id}/cancel")
    def cancel_research(job_id: str) -> dict:
        return ctx.research.cancel(job_id).model_dump(mode="json")

    # -- sessions (§60/§96) --------------------------------------------------

    @app.get("/api/sessions")
    def list_sessions() -> dict:
        return {"sessions": [s.model_dump(mode="json")
                             for s in ctx.knowledge.sessions()]}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict:
        return ctx.research.session_detail(
            session_id).model_dump(mode="json")

    # -- chemistry workspaces (Stage F §5-§8) --------------------------------
    # A workspace is a conversation with the ChemistryAgent. The API routes
    # translate HTTP into `WorkspaceApplication` use cases and back to JSON;
    # no chemistry, no SQL, no LLM is reachable from this file except through
    # `crea.app` (§22).

    @app.post("/api/workspaces")
    def create_workspace(payload: dict | None = None) -> dict:
        title = ((payload or {}).get("title") or "")
        return ctx.workspace.create_workspace(title).model_dump(mode="json")

    @app.get("/api/workspaces")
    def list_workspaces() -> dict:
        return {"workspaces": [w.model_dump(mode="json")
                               for w in ctx.workspace.list_workspaces()]}

    @app.get("/api/workspaces/{workspace_id}")
    def get_workspace(workspace_id: str) -> dict:
        return ctx.workspace.get_workspace(workspace_id).model_dump(mode="json")

    @app.get("/api/workspaces/{workspace_id}/messages")
    def list_workspace_messages(workspace_id: str) -> dict:
        return {"messages": [m.model_dump(mode="json")
                             for m in ctx.workspace.list_messages(workspace_id)]}

    @app.post("/api/workspaces/{workspace_id}/messages")
    def send_workspace_message(workspace_id: str, payload: dict | None = None) -> dict:
        content = ((payload or {}).get("content") or "")
        if not str(content).strip():
            raise invalid_request("消息内容不能为空")
        message = ctx.workspace.send_message(workspace_id, str(content))
        # §16: the Stage E result is never flattened -- the agent's answer
        # travels with its tool calls, evidence, provenance and diagnostics.
        return {
            "message": {"id": message.message_id,
                        "role": message.role.value,
                        "content": message.content},
            "tool_calls": [t.model_dump(mode="json")
                           for t in message.tool_calls],
            "evidence": message.evidence,
            "provenance": message.provenance,
            "diagnostics": message.diagnostics,
        }

    # -- literature (Stage 4.2) ---------------------------------------------
    # Search / DOI lookup across the providers (federation + DOI merge), and
    # reading a stored record. Offline, every call reports the providers
    # unavailable rather than crashing. No PDF, no permission decision.

    @app.get("/api/literature/search")
    def literature_search(q: str = Query(min_length=1),
                          limit: int = Query(default=20, ge=1, le=100)) -> dict:
        if not ctx.literature.available():
            raise ApiError("PROVIDERS_UNAVAILABLE_OFFLINE",
                           "文献 provider 当前不可用（offline）", status_code=503)
        result = ctx.literature.search(q, limit=limit)
        return result.model_dump(mode="json")

    # `/access` is declared before the greedy `{doi:path}` route so it is not
    # swallowed by it.
    @app.get("/api/literature/doi/{doi:path}/access")
    def literature_access(doi: str) -> dict:
        if not ctx.literature.available():
            raise ApiError("PROVIDERS_UNAVAILABLE_OFFLINE",
                           "文献 provider 当前不可用（offline）", status_code=503)
        return ctx.literature.access(doi)

    @app.get("/api/literature/doi/{doi:path}")
    def literature_doi(doi: str) -> dict:
        if not ctx.literature.available():
            raise ApiError("PROVIDERS_UNAVAILABLE_OFFLINE",
                           "文献 provider 当前不可用（offline）", status_code=503)
        return ctx.literature.lookup_doi(doi).model_dump(mode="json")

    @app.get("/api/literature/{literature_id}")
    def literature_record(literature_id: str) -> dict:
        try:
            paper = ctx.literature.get(literature_id)
        except Exception as exc:  # noqa: BLE001 - NotFound -> structured 404
            raise not_found(f"literature record {literature_id!r}") from exc
        return paper.model_dump(mode="json")

    # -- knowledge browsing (§97) --------------------------------------------

    @app.get("/api/entities/{entity_id}")
    def get_entity(entity_id: str) -> dict:
        return ctx.knowledge.entity(entity_id).model_dump(mode="json")

    @app.get("/api/entities/{entity_id}/navigation")
    def entity_navigation(entity_id: str) -> dict:
        return ctx.knowledge.entity_navigation(
            entity_id).model_dump(mode="json")

    @app.get("/api/entities/{entity_id}/mentions")
    def entity_mentions(entity_id: str) -> dict:
        return {"mentions": [m.model_dump(mode="json")
                             for m in ctx.knowledge.entity_mentions(entity_id)]}

    @app.get("/api/reactions/{reaction_id}")
    def get_reaction(reaction_id: str) -> dict:
        return ctx.knowledge.reaction(reaction_id).model_dump(mode="json")

    @app.get("/api/reactions/{reaction_id}/navigation")
    def reaction_navigation(reaction_id: str) -> dict:
        return ctx.knowledge.reaction_navigation(
            reaction_id).model_dump(mode="json")

    @app.get("/api/reactions/{reaction_id}/records")
    def reaction_records(reaction_id: str) -> dict:
        return {"records": [r.model_dump(mode="json")
                            for r in ctx.knowledge.reaction_record(reaction_id)]}

    @app.get("/api/reactions/{reaction_id}/evidence")
    def reaction_evidence(reaction_id: str) -> dict:
        return {"evidence": [e.model_dump(mode="json") for e in
                             ctx.knowledge.evidence_for_reaction(reaction_id)]}

    @app.get("/api/evidence/{evidence_id}")
    def get_evidence(evidence_id: str) -> dict:
        return ctx.knowledge.evidence(evidence_id).model_dump(mode="json")

    @app.get("/api/papers/{paper_id}")
    def get_paper(paper_id: str) -> dict:
        return ctx.knowledge.paper(paper_id).model_dump(mode="json")

    @app.get("/api/routes/{route_id}")
    def get_route(route_id: str) -> dict:
        return ctx.knowledge.route(route_id).model_dump(mode="json")

    # -- knowledge state & search (§20/§21/§99) -------------------------------

    @app.get("/api/knowledge/state")
    def knowledge_state(entity_id: str | None = None,
                        name: str | None = None) -> dict:
        return ctx.knowledge.coverage(entity_id=entity_id,
                                      name=name).model_dump(mode="json")

    @app.get("/api/knowledge/search")
    def knowledge_search(
            kind: str = Query(min_length=1),
            name: str | None = None, entity_id: str | None = None,
            reaction_id: str | None = None, paper_id: str | None = None,
            structure: str | None = None) -> dict:
        results = ctx.knowledge.search(
            kind=kind, name=name, entity_id=entity_id,
            reaction_id=reaction_id, paper_id=paper_id, structure=structure)
        return {"results": [r.model_dump(mode="json") for r in results]}

    @app.get("/api/knowledge/export")
    def knowledge_export() -> dict:
        return ctx.knowledge.export()

    # -- molecule depiction (§18) ---------------------------------------------

    @app.get("/api/molecule/svg")
    def molecule_svg(smiles: str = Query(min_length=1)) -> Response:
        from crea.chem.draw import molecule_svg as draw_svg

        svg = draw_svg(smiles)
        if svg is None:
            raise invalid_structure(f"无法解析结构：{smiles}")
        return Response(content=svg, media_type="image/svg+xml")

    # -- molecule explorer (Phase 1, §二) ------------------------------------
    # One SMILES in, a deterministic inspection out: identity, properties,
    # functional groups, scaffold, fragments and curated reaction matches.
    # No LLM; every value is computed by the chemistry layer.

    @app.get("/api/molecule/inspect")
    def molecule_inspect(smiles: str = Query(min_length=1)) -> dict:
        from crea.services.inspection import MoleculeInspectionService

        return MoleculeInspectionService().inspect(smiles).model_dump(mode="json")

    @app.get("/api/molecule/compare")
    def molecule_compare(smiles_a: str = Query(min_length=1),
                         smiles_b: str = Query(min_length=1)) -> dict:
        from crea.services.similarity import SimilarityService

        return SimilarityService().compare(smiles_a, smiles_b).model_dump(mode="json")

    # -- 3D conformer (Stage 2.1, §五/§六) -----------------------------------
    # One generated conformer, coordinates included. Errors are structured --
    # INVALID_MOLECULE for a bad SMILES, CONFORMER_GENERATION_FAILED for a
    # valid structure that could not be embedded; never a raw traceback.

    @app.get("/api/molecule/3d")
    def molecule_3d(smiles: str = Query(min_length=1),
                    optimize: bool = False) -> dict:
        from crea.core.errors import ConformerError
        from crea.services.conformer import ConformerService

        try:
            return ConformerService().generate(
                smiles, optimize=optimize).model_dump(mode="json")
        except ConformerError as exc:
            if exc.code == "INVALID_MOLECULE":
                raise invalid_molecule(exc.message) from exc
            raise conformer_failed(exc.message) from exc

    # -- molecule library (Stage 2.2, §二十) ---------------------------------
    # Records are the existing ChemicalEntity (entity_id = internal record id,
    # identity_key = Standard InChI, inchikey = Standard InChIKey, external
    # ids in external_identifiers). Errors are structured, never a traceback.

    @app.post("/api/molecules")
    def add_molecule(payload: dict | None = None) -> dict:
        from crea.core.errors import MoleculeLibraryError

        smiles = ((payload or {}).get("smiles") or "").strip()
        if not smiles:
            raise invalid_request("需要 smiles 字段")
        source_type = (payload or {}).get("source_type") or "USER_PROVIDED"
        source_id = (payload or {}).get("source_id") or ""
        try:
            entity, is_new = ctx.molecule_library.add(
                smiles, source_type=source_type, source_id=source_id)
        except MoleculeLibraryError as exc:
            raise _molecule_library_error(exc) from exc
        return {"molecule": entity.model_dump(mode="json"), "is_new": is_new}

    @app.get("/api/molecules")
    def list_molecules() -> dict:
        molecules = ctx.molecule_library.list()
        return {"molecules": [m.model_dump(mode="json") for m in molecules],
                "count": len(molecules)}

    @app.get("/api/molecules/search/similar")
    def search_similar(smiles: str = Query(min_length=1),
                       limit: int = Query(default=10, ge=1, le=100)) -> dict:
        from crea.core.errors import MoleculeLibraryError

        try:
            hits = ctx.molecule_library.search_similar(smiles, limit=limit)
        except MoleculeLibraryError as exc:
            raise _molecule_library_error(exc) from exc
        return {"hits": [h.model_dump(mode="json") for h in hits]}

    @app.get("/api/molecules/search/substructure")
    def search_substructure(smiles: str = Query(min_length=1)) -> dict:
        from crea.core.errors import MoleculeLibraryError

        try:
            hits = ctx.molecule_library.search_substructure(smiles)
        except MoleculeLibraryError as exc:
            raise _molecule_library_error(exc) from exc
        return {"hits": [h.model_dump(mode="json") for h in hits]}

    @app.get("/api/molecules/identity/{inchikey}")
    def molecule_by_inchikey(inchikey: str) -> dict:
        entity = ctx.molecule_library.find_by_inchikey(inchikey)
        if entity is None:
            raise molecule_not_found(f"no molecule with InChIKey {inchikey!r}")
        return entity.model_dump(mode="json")

    @app.get("/api/molecules/{record_id}")
    def get_molecule(record_id: str) -> dict:
        from crea.core.errors import MoleculeLibraryError

        try:
            entity = ctx.molecule_library.get(record_id)
        except MoleculeLibraryError as exc:
            raise _molecule_library_error(exc) from exc
        return entity.model_dump(mode="json")

    @app.delete("/api/molecules/{record_id}")
    def delete_molecule(record_id: str) -> dict:
        return {"deleted": ctx.molecule_library.delete(record_id)}

    # -- PubChem exact lookup (Stage 2.3, §二十五) ---------------------------
    # Lookup is separate from import (§9). Offline mode reports UNAVAILABLE
    # rather than crashing the app (§22). Errors are structured, never raw.

    @app.get("/api/providers/pubchem/lookup")
    def pubchem_lookup(smiles: str = Query(min_length=1)) -> dict:
        from crea.core.errors import MoleculeLibraryError

        try:
            result = ctx.pubchem.lookup(smiles)
        except MoleculeLibraryError as exc:
            raise _molecule_library_error(exc) from exc
        return result.model_dump(mode="json")

    @app.post("/api/providers/pubchem/import")
    def pubchem_import(payload: dict | None = None) -> dict:
        from crea.core.errors import MoleculeLibraryError

        smiles = ((payload or {}).get("smiles") or "").strip()
        if not smiles:
            raise invalid_request("需要 smiles 字段")
        try:
            entity, is_new = ctx.pubchem.import_by_smiles(smiles)
        except MoleculeLibraryError as exc:
            raise _molecule_library_error(exc) from exc
        return {"molecule": entity.model_dump(mode="json"), "is_new": is_new}

    # -- reaction explorer (Stage 3.2) --------------------------------------
    # The 18 curated ReactionTemplates (Stage 3.1), browsable. `/api/reactions`
    # is already taken by the knowledge base's Reaction detail, so templates
    # live under `/api/reaction-templates`. The class filter uses the real
    # `ReactionClassFamily` enum values, never a frontend-invented list (§15).

    @app.get("/api/reaction-templates")
    def list_reaction_templates(class_family: str | None = None,
                                q: str | None = None) -> dict:
        from crea.services.reaction_library import list_templates

        templates = list_templates()
        if class_family:
            templates = [t for t in templates
                         if t.class_family.value == class_family]
        if q:
            needle = q.strip().lower()
            templates = [t for t in templates
                         if needle in t.name.lower()
                         or needle in t.reaction_class.lower()]
        return {"reactions": [t.model_dump(mode="json") for t in templates],
                "count": len(templates)}

    @app.get("/api/reaction-templates/{template_id}")
    def get_reaction_template(template_id: str) -> dict:
        from crea.services.reaction_library import get_template

        template = get_template(template_id)
        if template is None:
            raise not_found(f"reaction template {template_id!r}")
        return template.model_dump(mode="json")

    # -- reaction evidence (Stage 3.4) --------------------------------------
    # What supports a template, and how. A curated template returns its
    # CURATED self-evidence; no literature is fabricated.

    @app.get("/api/reaction-templates/{template_id}/evidence")
    def list_reaction_evidence(template_id: str) -> dict:
        from crea.core.errors import ReactionEvidenceError

        try:
            evidence = ctx.reaction_evidence.list_evidence(template_id)
        except ReactionEvidenceError as exc:
            raise not_found(exc.message) from exc
        return {"evidence": [e.model_dump(mode="json") for e in evidence],
                "count": len(evidence)}

    @app.post("/api/reaction-templates/{template_id}/evidence")
    def attach_reaction_evidence(template_id: str,
                                 payload: dict | None = None) -> dict:
        from crea.core.errors import ReactionEvidenceError
        from crea.core.models.reaction_evidence import ReactionEvidence

        body = dict(payload or {})
        body["reaction_template_id"] = template_id
        if not body.get("evidence_id"):
            raise invalid_request("需要 evidence_id 字段")
        try:
            evidence = ReactionEvidence.model_validate(body)
        except Exception as exc:  # noqa: BLE001 - pydantic errors are structured
            raise invalid_request("evidence 记录参数无效", str(exc)) from exc
        try:
            stored = ctx.reaction_evidence.attach_evidence(evidence)
        except ReactionEvidenceError as exc:
            if exc.code == "TEMPLATE_NOT_FOUND":
                raise not_found(exc.message) from exc
            raise invalid_request(exc.message) from exc
        return stored.model_dump(mode="json")

    @app.post("/api/reaction-templates/{template_id}/evidence/literature")
    def link_literature_evidence(template_id: str,
                                 payload: dict | None = None) -> dict:
        """Link a locally-stored paper to a template (Stage 4.4).

        Local only: the DOI/id is resolved against the literature store; no
        provider is contacted. An absent local record is 404 LITERATURE_NOT_FOUND.
        """
        from crea.core.errors import ReactionEvidenceError
        from crea.core.models.reaction_evidence import (
            EvidenceLocator,
            ReactionEvidenceType,
        )
        from crea.core.models.reaction_template import EvidenceStatus

        body = dict(payload or {})
        try:
            evidence_type = ReactionEvidenceType(
                body.get("evidence_type") or "transformation_support")
            locator = EvidenceLocator.model_validate(body.get("locator") or {})
            status = EvidenceStatus(
                body.get("status") or "literature_supported")
        except Exception as exc:  # noqa: BLE001 - pydantic errors are structured
            raise invalid_request("evidence 参数无效", str(exc)) from exc
        try:
            evidence, is_new = ctx.reaction_evidence.link_literature(
                template_id,
                paper_id=str(body.get("paper_id") or ""),
                doi=str(body.get("doi") or ""),
                evidence_type=evidence_type,
                support_scope=str(body.get("support_scope") or ""),
                locator=locator, status=status,
                note=str(body.get("note") or ""))
        except ReactionEvidenceError as exc:
            if exc.code in ("TEMPLATE_NOT_FOUND", "LITERATURE_NOT_FOUND"):
                raise not_found(exc.message) from exc
            raise invalid_request(exc.message) from exc
        return {"evidence": evidence.model_dump(mode="json"), "is_new": is_new}

    @app.get("/api/literature/{literature_id}/reaction-evidence")
    def literature_reaction_evidence(literature_id: str) -> dict:
        """Which reaction evidence cites this paper -- the reverse query (§2)."""
        evidence = ctx.reaction_evidence.evidence_for_literature(literature_id)
        return {"evidence": [e.model_dump(mode="json") for e in evidence],
                "count": len(evidence)}

    # -- transformation search (Stage 5.1) ----------------------------------
    # substrate -> candidate ReactionTemplates; +target promotes a match to
    # transformation_match. Deterministic, local, no AI, no literature lookup.

    @app.get("/api/transformations/search")
    def transformation_search(substrate: str = Query(min_length=1),
                              target: str = "") -> dict:
        from crea.chem.normalize import NormalizationError

        try:
            result = ctx.transformation_search.search(substrate, target)
        except NormalizationError as exc:
            raise invalid_molecule(str(exc)) from exc
        payload = result.model_dump(mode="json")
        payload["count"] = result.count
        return payload

    # -- reaction comparison (Stage 5.2) ------------------------------------
    # Compare the named templates on their known facts. POST because the caller
    # supplies a list. Transient: nothing is persisted, nothing is recommended.

    @app.post("/api/reactions/compare")
    def compare_reactions(payload: dict | None = None) -> dict:
        from crea.core.errors import ReactionComparisonError

        body = dict(payload or {})
        ids = body.get("reaction_template_ids") or []
        if not isinstance(ids, list):
            raise invalid_request("reaction_template_ids 必须是数组")
        try:
            result = ctx.reaction_comparison.compare(
                [str(i) for i in ids],
                substrate=str(body.get("substrate") or ""),
                target=str(body.get("target") or ""))
        except ReactionComparisonError as exc:
            if exc.code == "TEMPLATE_NOT_FOUND":
                raise not_found(exc.message) from exc
            raise invalid_request(exc.message) from exc
        return result.model_dump(mode="json")

    # -- reaction graph (Stage 5.3) -----------------------------------------
    # A derived, depth-limited graph over local data. Representation only:
    # no route planning, no recommendation, no product guessing.

    @app.get("/api/reaction-graph/molecule/{entity_id}")
    def graph_from_molecule(entity_id: str,
                            depth: int = Query(default=1, ge=1, le=2)) -> dict:
        from crea.core.errors import ReactionGraphError

        try:
            return ctx.reaction_graph.molecule_graph(
                entity_id, depth).model_dump(mode="json")
        except ReactionGraphError as exc:
            raise not_found(exc.message) from exc

    @app.get("/api/reaction-graph/reaction/{template_id}")
    def graph_from_reaction(template_id: str,
                            depth: int = Query(default=1, ge=1, le=2)) -> dict:
        from crea.core.errors import ReactionGraphError

        try:
            return ctx.reaction_graph.reaction_graph(
                template_id, depth).model_dump(mode="json")
        except ReactionGraphError as exc:
            raise not_found(exc.message) from exc

    @app.get("/api/reaction-graph/literature/{paper_id}")
    def graph_from_literature(paper_id: str,
                              depth: int = Query(default=1, ge=1, le=2)) -> dict:
        from crea.core.errors import ReactionGraphError

        try:
            return ctx.reaction_graph.literature_graph(
                paper_id, depth).model_dump(mode="json")
        except ReactionGraphError as exc:
            raise not_found(exc.message) from exc

    # -- route explorer (Stage 5.4) -----------------------------------------
    # Bounded, forward-only route search over the known reactions. Transient;
    # no retrosynthesis, no recommendation, no product guessing.

    @app.post("/api/routes/search")
    def search_routes(payload: dict | None = None) -> dict:
        from crea.chem.normalize import NormalizationError
        from crea.core.errors import RouteExplorerError
        from crea.core.models.route_explorer import RouteStatus

        body = dict(payload or {})
        start = str(body.get("start_molecule") or "").strip()
        target = str(body.get("target_molecule") or "").strip()
        if not start or not target:
            raise invalid_request("需要 start_molecule 与 target_molecule")
        try:
            result = ctx.route_explorer.explore(
                start, target, int(body.get("max_steps") or 3))
        except NormalizationError as exc:
            raise invalid_molecule(str(exc)) from exc
        except RouteExplorerError as exc:
            raise invalid_request(exc.message, exc.code) from exc
        return {
            "start_smiles": result.start_smiles,
            "target_smiles": result.target_smiles,
            "max_steps": result.max_steps,
            "routes": [p.model_dump(mode="json")
                       for p in result.by_status(RouteStatus.COMPLETE)],
            "partial_routes": [p.model_dump(mode="json")
                               for p in result.by_status(RouteStatus.PARTIAL)],
            "blocked_paths": [p.model_dump(mode="json")
                              for p in result.by_status(RouteStatus.BLOCKED)],
            "count": result.count,
            "diagnostics": result.diagnostics,
        }

    # -- AI context (Stage 6.1) ---------------------------------------------
    # Structured FACT assembly for a future AI layer. Returns a context, never
    # an answer; no AI is invoked.

    @app.post("/api/ai/context")
    def ai_context(payload: dict | None = None) -> dict:
        from crea.core.errors import ContextIntegrityError
        from crea.core.models.ai_context import AIContextRequest

        try:
            request = AIContextRequest.model_validate(payload or {})
        except Exception as exc:  # noqa: BLE001 - pydantic errors are structured
            raise invalid_request("AI context 请求参数无效", str(exc)) from exc
        try:
            context = ctx.ai_context.build(request)
        except ContextIntegrityError as exc:
            raise ApiError("CONTEXT_INTEGRITY_ERROR", exc.message,
                           detail="; ".join(exc.problems), status_code=422) from exc
        return {"context_version": context.context_version,
                "context": context.model_dump(mode="json")}

    # -- modification analysis (Stage 6.2) ----------------------------------
    # AI interprets a requested modification; every candidate is grounded
    # against the AI Context. Returns a structured analysis, never a bare
    # answer. AI errors are structured and never affect the deterministic tools.

    @app.post("/api/ai/modification-analysis")
    def modification_analysis(payload: dict | None = None) -> dict:
        from crea.core.errors import ModificationAnalysisError
        from crea.core.models.modification import ModificationAnalysisRequest
        from crea.chem.normalize import NormalizationError

        try:
            request = ModificationAnalysisRequest.model_validate(payload or {})
        except Exception as exc:  # noqa: BLE001 - pydantic errors are structured
            raise invalid_request("修改分析请求参数无效", str(exc)) from exc
        if not request.query.strip():
            raise invalid_request("需要 query 字段")
        try:
            analysis = ctx.modification_analysis.analyze(request)
        except NormalizationError as exc:
            raise invalid_molecule(str(exc)) from exc
        except ModificationAnalysisError as exc:
            raise invalid_request(exc.message, exc.code) from exc
        return {"analysis": analysis.model_dump(mode="json")}

    # -- route reasoning (Stage 6.3) ----------------------------------------
    # One bounded AI call explaining the deterministic routes from 5.4. No loop,
    # no tool calling; grounded against the real routes. Never an answer alone.

    @app.post("/api/ai/route-reasoning")
    def route_reasoning(payload: dict | None = None) -> dict:
        from crea.chem.normalize import NormalizationError
        from crea.core.errors import RouteReasoningError, RouteExplorerError
        from crea.core.models.route_reasoning import RouteReasoningRequest

        try:
            request = RouteReasoningRequest.model_validate(payload or {})
        except Exception as exc:  # noqa: BLE001 - pydantic errors are structured
            raise invalid_request("路线推理请求参数无效", str(exc)) from exc
        if not request.start_molecule.strip() or not request.target_molecule.strip():
            raise invalid_request("需要 start_molecule 与 target_molecule")
        try:
            result = ctx.route_reasoning.reason(request)
        except NormalizationError as exc:
            raise invalid_molecule(str(exc)) from exc
        except RouteExplorerError as exc:
            raise invalid_request(exc.message, exc.code) from exc
        except RouteReasoningError as exc:
            raise invalid_request(exc.message, exc.code) from exc
        return {"reasoning": result.model_dump(mode="json")}

    # -- evidence-aware agent (Stage 6.4) -----------------------------------
    # A bounded, read-only, tool-using run. Returns a grounded answer, never a
    # bare string; AI errors never affect the deterministic tools.

    @app.post("/api/ai/agent")
    def evidence_agent(payload: dict | None = None) -> dict:
        from crea.core.errors import ProviderUnavailable
        from crea.core.models.agent_run import EvidenceAgentRequest

        try:
            request = EvidenceAgentRequest.model_validate(payload or {})
        except Exception as exc:  # noqa: BLE001 - pydantic errors are structured
            raise invalid_request("Agent 请求参数无效", str(exc)) from exc
        if not request.question.strip():
            raise invalid_request("需要 question 字段")
        try:
            result = ctx.evidence_agent.run(
                request.question, molecule_smiles=request.molecule_smiles,
                molecule_id=request.molecule_id)
        except ProviderUnavailable as exc:
            raise ApiError("AGENT_PROVIDER_UNAVAILABLE", str(exc), status_code=503) from exc
        return {"answer": result.model_dump(mode="json")}

    # -- frontend (§103) -------------------------------------------------------

    root = Path(web_root) if web_root is not None else _default_web_root()
    if root.is_dir():
        app.mount("/", StaticFiles(directory=str(root), html=True), name="web")

    return app


def _validate_structure(kind: str, value: str) -> None:
    """Structure input is checked before a job starts (§56).

    SMILES/InChI go through the chemistry layer right here; an unparseable
    structure is a structured `INVALID_STRUCTURE` about the *input* --
    never a queued job that dies later, and never a library exception
    name or message leaking to a client.
    """
    if kind == "NAME":
        return  # names resolve through providers during the run
    from crea.core.models.identity import StructureInput
    from crea.services.identity import IdentityResolutionService

    try:
        resolution = IdentityResolutionService().resolve(
            StructureInput.of(kind=kind, value=value))
    except Exception:  # noqa: BLE001 - internals stay internal
        raise invalid_structure(f"无法解析 {kind} 输入") from None
    from crea.core.models.identity import StructureResolutionStatus

    if resolution.status is StructureResolutionStatus.INVALID:
        raise invalid_structure(f"无法解析 {kind} 输入")


def _default_web_root() -> Path:
    # src/crea/api/app.py -> project root
    return Path(__file__).resolve().parents[3] / "web" / "dist"


def _workflow_action(fn, workflow_id: str) -> dict:
    from crea.core.errors import ResearchWorkflowError

    try:
        return fn(workflow_id).model_dump(mode="json")
    except ResearchWorkflowError as exc:
        raise _workflow_error(exc) from exc


def _workflow_error(exc) -> ApiError:
    """Map a `ResearchWorkflowError` to its structured API error."""
    if exc.code in ("WORKFLOW_NOT_FOUND", "TASK_NOT_FOUND"):
        return not_found(exc.message)
    if exc.code in ("WORKFLOW_CYCLE", "INVALID_WORKFLOW_STATE",
                    "TASK_NOT_READY", "TASK_RETRY_LIMIT"):
        return ApiError(exc.code, exc.message, status_code=409)
    if exc.code == "WORKFLOW_LIMIT_REACHED":
        return ApiError(exc.code, exc.message, status_code=422)
    return invalid_request(exc.message, exc.code)


def _intelligence_error(exc) -> ApiError:
    """Map a `ResearchStudyError` raised by the AI research entry.

    `SESSION_NOT_FOUND` and `NOT_AWAITING_INPUT` are client mistakes about
    a specific run; `STACK_NOT_CONFIGURED` and an unusable provider are
    the server's problem and are reported as such rather than as a bad
    request.
    """
    if exc.code in ("SESSION_NOT_FOUND", "REFERENCE_NOT_FOUND"):
        return ApiError(exc.code, exc.message, status_code=404)
    if exc.code == "NOT_AWAITING_INPUT":
        return ApiError(exc.code, exc.message, status_code=409)
    if exc.code in ("STACK_NOT_CONFIGURED",):
        return ApiError(exc.code, exc.message, status_code=503)
    return invalid_request(exc.message, exc.code)


def _study_error(exc) -> ApiError:
    """Map a `ResearchStudyError` to its structured API error."""
    if exc.code == "STUDY_NOT_FOUND":
        return not_found(exc.message)
    if exc.code == "REFERENCE_NOT_FOUND":
        return ApiError("REFERENCE_NOT_FOUND", exc.message, status_code=404)
    if exc.code == "INVALID_STATUS_TRANSITION":
        return ApiError("INVALID_STATUS_TRANSITION", exc.message, status_code=409)
    return invalid_request(exc.message)


def _molecule_library_error(exc) -> ApiError:
    """Map a `MoleculeLibraryError` code to its structured API error."""
    if exc.code == "MOLECULE_NOT_FOUND":
        return molecule_not_found(exc.message)
    if exc.code == "INVALID_MOLECULE":
        return invalid_molecule(exc.message)
    if exc.code == "PROVIDER_NOT_FOUND":
        return ApiError("PROVIDER_NOT_FOUND", exc.message, status_code=404)
    if exc.code == "PROVIDER_RATE_LIMITED":
        return ApiError("PROVIDER_RATE_LIMITED", exc.message, status_code=429)
    if exc.code == "PROVIDER_UNAVAILABLE":
        return ApiError("PROVIDER_UNAVAILABLE", exc.message, status_code=503)
    if exc.code == "PROVIDER_INVALID_RESPONSE":
        return ApiError("PROVIDER_INVALID_RESPONSE", exc.message, status_code=502)
    if exc.code == "PROVIDER_IDENTITY_MISMATCH":
        return ApiError("PROVIDER_IDENTITY_MISMATCH", exc.message, status_code=409)
    return invalid_request(exc.message)


__all__ = ["create_app"]

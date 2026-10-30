"""The AI Research Intelligence's product entry (M2.0).

One use case, four moves: start a research question, read what it is doing,
answer the question it paused on, and read the report. Everything the
application layer needs is injected -- the LLM factory and the tool-registry
factory are built by the composition root, because this layer may not name
a vendor or a transport.

What this layer adds on top of `ResearchIntelligence` itself:

* **A study to remember in.** Every run opens (or continues) a
  `ResearchStudy` and points a `StudyContextService` at it, so facts,
  hypotheses, decisions and open issues survive the run and the next
  session starts from them.
* **A handle to come back to.** A paused run is kept in memory keyed by
  its session id so the caller can answer it. The decisions themselves are
  already persisted by the study context, so a restart loses the ability
  to *resume a pause in this process* but never the audit trail.
* **Redaction.** Everything returned goes through the project's own
  credential redaction before it leaves this layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from crea.core.errors import ResearchStudyError
from crea.core.provenance import utc_now_iso
from crea.core.redaction import redact
from crea.report.intelligence import render_intelligence_report


@dataclass
class _LiveRun:
    """One run this process can still talk to."""

    intelligence: Any
    result: Any
    study_id: str = ""
    goal: str = ""


@dataclass
class ResearchIntelligenceApplication:
    """Start, inspect, answer and report an AI-driven research run."""

    db_path: Path
    #: Builds an LLM, or returns None when none is configured. Built by the
    #: composition root; this layer never names a vendor.
    llm_factory: Callable[[], Any] | None = None
    #: Builds `(registry, library, evidence_sink)`. Built by the composition
    #: root (offline fixtures or live providers).
    stack_factory: Callable[..., tuple] | None = None
    #: Per-run budget overrides, applied when the caller does not pass one.
    budget: Any = None
    _runs: dict[str, _LiveRun] = field(default_factory=dict, repr=False)

    # -- start --------------------------------------------------------------

    def start(self, goal: str, *, study_id: str = "",
              approvals: list[str] | None = None,
              document_ids: list[str] | None = None,
              budget: Any = None) -> dict:
        """Research one goal to a terminal state or a pause.

        Runs synchronously: an AI research loop is a bounded number of
        model and tool calls, and the caller sees the whole result. A
        caller that needs a background job should put this behind its own
        worker, exactly as `ResearchWorkflow` is behind `JobManager`.
        """
        goal = (goal or "").strip()
        if not goal:
            raise ResearchStudyError(
                "INVALID_INPUT", "a research run needs a goal")
        if self.stack_factory is None:
            raise ResearchStudyError(
                "STACK_NOT_CONFIGURED",
                "no tool-registry factory is configured for the AI research "
                "entry; the composition root must supply one")
        llm = self._llm()

        from crea.agent.intelligence import ResearchIntelligence

        registry, library, sink = self.stack_factory(
            document_ids=list(document_ids or []))
        study_id = study_id or self._new_study_id(goal)
        context, kb = self._open_study(study_id, goal)
        try:
            intelligence = ResearchIntelligence(
                registry, llm, study_context=context, study_id=study_id,
                budget=budget or self.budget)
            result = intelligence.run(goal, approvals=approvals)
        finally:
            # The store handle belongs to this request. A later `answer`
            # re-opens it on its own thread; nothing keeps this one.
            _close_kb(kb)
        self._runs[result.session_id] = _LiveRun(
            intelligence=intelligence, result=result, study_id=study_id,
            goal=goal)
        return self.serialize(result)

    # -- inspect ------------------------------------------------------------

    def get(self, session_id: str) -> dict:
        """The last known state of one run in this process."""
        return self.serialize(self._require(session_id).result)

    def decisions(self, session_id: str) -> list[dict]:
        """Every turn of one run, accepted or not -- the audit trail."""
        run = self._require(session_id)
        return [redact(record.model_dump(mode="json"))
                for record in run.result.decision_records]

    def report(self, session_id: str) -> str:
        """The run rendered as a readable, epistemically honest report."""
        return render_intelligence_report(self._require(session_id).result)

    def live_result(self, session_id: str) -> Any:
        """The `IntelligenceRunResult` itself, for a caller that renders.

        A CLI wants the object, not a dict: `render_intelligence_report`
        reads the model's own fields and keeps the epistemic categories
        apart. The HTTP layer wants `serialize` instead.
        """
        return self._require(session_id).result

    # -- answer / cancel ----------------------------------------------------

    def answer(self, session_id: str, *, answers: dict | None = None,
               approvals: list[str] | None = None) -> dict:
        """Continue a paused run with what the person supplied.

        The study context is re-opened on this request's thread: the run
        keeps its study id, and only the handle onto it is rebuilt.
        """
        run = self._require(session_id)
        if run.result.awaiting is None:
            raise ResearchStudyError(
                "NOT_AWAITING_INPUT",
                f"session {session_id!r} is not waiting for an answer "
                f"(outcome: {run.result.outcome})")
        context, kb = self._open_study(run.study_id, run.goal)
        try:
            result = run.intelligence.with_study_context(context).resume(
                run.result, answers=answers, approvals=approvals)
        finally:
            _close_kb(kb)
        run.result = result
        return self.serialize(result)

    def cancel(self, session_id: str) -> dict:
        """Ask a running study to stop before its next decision.

        A run already finished is reported as such, not restarted. The
        stop is cooperative: a tool already executing finishes first, and
        this never claims otherwise.
        """
        run = self._require(session_id)
        run.intelligence.cancel()
        if run.result.awaiting is None and run.result.ended_at:
            return self.serialize(run.result)
        return self.serialize(run.result)

    # -- serialization ------------------------------------------------------

    @staticmethod
    def serialize(result: Any) -> dict:
        """One run, as a JSON-safe, credential-free dict.

        The model's audit records are included in full; the tool results
        are summarised rather than dumped, because a report handed over
        HTTP should not carry a whole document body.
        """
        payload = {
            "session_id": result.session_id,
            "study_id": result.study_id,
            "goal": result.goal,
            "status": result.status.value,
            "outcome": result.outcome,
            "mode": result.mode.value,
            "reason": result.reason,
            "started_at": result.started_at,
            "ended_at": result.ended_at,
            "duration_ms": result.duration_ms,
            "cancelled": result.cancelled,
            "evidence_ids": list(result.evidence_ids),
            "hypothesis_ids": list(result.hypothesis_ids),
            "unresolved_items": list(result.unresolved_items),
            "failures": list(result.failures),
            "diagnostics": list(result.diagnostics),
            "budget": {
                "decisions_used": result.decisions_used,
                "tool_calls_used": result.tool_calls_used,
                "model_calls_used": result.model_calls_used,
                "max_decisions": result.budget.max_decisions,
                "max_tool_calls": result.budget.max_tool_calls,
                "max_model_calls": result.budget.max_model_calls,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            },
            "model": {"provider": result.model_provider,
                      "name": result.model_name,
                      "prompt_version": result.prompt_version},
            "steps": [
                {"step_id": step.step_id, "tool": step.tool,
                 "arguments": step.arguments, "note": step.note,
                 "result_status": (step.result_status.value
                                   if step.result_status else None),
                 "error": step.error}
                for step in result.steps
            ],
            "observations": [
                {"step_id": o.step_id, "tool": o.tool,
                 "kind": o.kind.value, "summary": o.summary,
                 "evidence_ids": list(o.evidence_ids)}
                for o in result.observations
            ],
            "interpretations": [
                {"step_id": i.step_id, "tool": i.tool,
                 "observation_kind": i.observation_kind,
                 "interpretation": i.interpretation}
                for i in result.interpretations
            ],
            "awaiting": (None if result.awaiting is None else {
                "question": result.awaiting.question,
                "why": result.awaiting.why,
                "expected_information": result.awaiting.expected_information,
                "options": list(result.awaiting.options),
            }),
            "completion": (None if result.completion is None else
                           result.completion.model_dump(mode="json")),
            "blocked": (None if result.blocked is None else
                        result.blocked.model_dump(mode="json")),
        }
        return redact(payload)

    # -- internals ----------------------------------------------------------

    def _llm(self) -> Any:
        if self.llm_factory is None:
            return None
        return self.llm_factory()

    def _open_study(self, study_id: str, goal: str) -> tuple[Any, Any]:
        """A study context bound to one study, and the store behind it.

        Returns `(context, kb)`; either may be `None` when no database is
        usable. A run without one still researches -- it just does not
        accumulate, and `IntelligenceRunResult.diagnostics` say which.
        """
        from crea.services.study_context import StudyContextService

        kb = _open_kb(self.db_path)
        if kb is None:
            return None, None
        context = StudyContextService(kb)
        try:
            _ensure_study(kb, study_id, goal)
            context.open(study_id, goal=goal)
        except ResearchStudyError:
            # The study is already open with a fixed question (§二十七): a
            # continuation binds the profile again and keeps the question.
            pass
        return context, kb

    def _require(self, session_id: str) -> _LiveRun:
        run = self._runs.get(session_id)
        if run is None:
            raise ResearchStudyError(
                "SESSION_NOT_FOUND",
                f"no research session {session_id!r} in this process; a "
                f"paused run can only be answered by the process that "
                f"started it")
        return run

    @staticmethod
    def _new_study_id(goal: str) -> str:
        import time

        from crea.core.ids import content_hash
        return "study-" + content_hash(f"{goal}|{time.time_ns()}", 12)


def _ensure_study(kb, study_id: str, goal: str) -> None:
    """Create the study container when it does not exist yet.

    `ResearchStudyService.create` mints its own id, and the run already
    knows the one it wants (it is the id the caller passed, or the one
    derived from the goal), so the container is written here with that id
    rather than discarded and replaced.
    """
    from crea.core.models.research_study import (
        ResearchStudy,
        StudyEventType,
        StudyStatus,
    )
    from crea.services.research_study import ResearchStudyService

    if kb.get_study(study_id) is not None:
        return
    now = utc_now_iso()
    kb.save_study(ResearchStudy(
        study_id=study_id, title=(goal or "Untitled research session")[:120],
        question=goal, status=StudyStatus.ACTIVE,
        created_at=now, updated_at=now))
    ResearchStudyService(kb).append_event(
        study_id, StudyEventType.STUDY_CREATED,
        summary="AI research session created")


def _close_kb(kb) -> None:
    """Close the store handle this request opened.

    Not wrapped: a store that refuses to close is a real failure and
    surfaces as one. The run's own result has already been assembled by
    the time this runs, so nothing the researcher produced is lost.
    """
    if kb is None:
        return
    close = getattr(kb, "close", None)
    if callable(close):
        close()


def _open_kb(db_path: Path):
    """A knowledge base for the study context, or None when unavailable.

    A caller that runs without a database still gets a working research
    loop -- it simply does not accumulate across sessions, and the result
    says so in its diagnostics.
    """
    from crea.services.knowledge_base import KnowledgeBase

    try:
        kb = KnowledgeBase(Path(db_path))
        kb.initialize()
        return kb
    except Exception:  # noqa: BLE001 - an unusable store is not a crash
        return None


__all__ = ["ResearchIntelligenceApplication"]

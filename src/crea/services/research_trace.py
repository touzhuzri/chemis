"""M2.0.1 -- the AI research run's audit trail, persisted.

`StudyContextService` remembers what a study *knows* across sessions --
facts, hypotheses, decision summaries, issues. This service remembers
what one AI research run *did*, turn by turn: every `DecisionRecord` with
the gate's ruling, the tool that ran, the arguments it ran with, the raw
`ToolResult`, the M1.2 `Observation` and the model's
`ScientificInterpretation` in its own field beside the result it reads.
The run's terminal summary -- status, stop reason, budget actually
consumed, model name (never a credential) -- is the second record.

Why it exists: the first real-model acceptance run could only be audited
because its script happened to dump JSON before the process exited. An
audit trail that dies with the process that wrote it is not an audit
trail. This one lives in the same SQLite store as everything else, keyed
by study first, and any process that can open the database can re-read
the trail and re-render the report.

What it is not:

* **Not a second decision loop.** It writes what the intelligence hands
  it and reads rows back; it never calls a model, never executes a tool,
  never validates a decision.
* **Not a task resume.** Reading a trail back rebuilds the *record* of a
  run, not the ability to continue it. A paused run is still answerable
  only by the process that started it.
* **Not a place interpretations become facts.** The interpretation is
  stored in its own column, beside the observation; nothing here promotes
  it, merges it into the tool result, or writes it to the study context.
"""

from __future__ import annotations

from crea.core.errors import ResearchStudyError
from crea.core.models.research_trace import (
    ResearchRunTrace,
    ResearchTurnTrace,
)


class ResearchTraceService:
    """Write and read one study's AI research audit trail, through one door."""

    def __init__(self, kb) -> None:
        self._kb = kb

    # -- writes (called from the loop, one row at a time) --------------------

    def record_turn(self, turn: ResearchTurnTrace) -> ResearchTurnTrace:
        """Persist one audited turn. Upsert by decision id: writing the
        same turn twice refreshes it and never duplicates the trail."""
        if not (turn.study_id or "").strip():
            raise ResearchStudyError(
                "INVALID_INPUT", "a turn trace needs a study_id")
        if not (turn.session_id or "").strip():
            raise ResearchStudyError(
                "INVALID_INPUT", "a turn trace needs a session_id")
        return self._kb.save_ai_research_turn(turn.study_id, turn)

    def record_run(self, run: ResearchRunTrace) -> ResearchRunTrace:
        """Persist one run's terminal summary (latest segment wins)."""
        if not (run.study_id or "").strip():
            raise ResearchStudyError(
                "INVALID_INPUT", "a run trace needs a study_id")
        if not (run.session_id or "").strip():
            raise ResearchStudyError(
                "INVALID_INPUT", "a run trace needs a session_id")
        return self._kb.save_ai_research_run(run.study_id, run)

    # -- reads (the audit door, open to any process that holds the DB) ------

    def turns(self, study_id: str, session_id: str) -> list[ResearchTurnTrace]:
        """Every audited turn of one session, in the order it happened."""
        return self._kb.list_ai_research_turns(study_id, session_id)

    def run(self, study_id: str, session_id: str) -> ResearchRunTrace | None:
        return self._kb.get_ai_research_run(study_id, session_id)

    def find_run(self, session_id: str) -> ResearchRunTrace | None:
        """The run summary behind one session id, across studies.

        The session id is the handle the caller was already given; this
        is how a second process finds the study it belongs to. There is
        no enumeration here -- an id you do not have stays unreachable.
        """
        return self._kb.find_ai_research_run(session_id)

    def runs(self, study_id: str) -> list[ResearchRunTrace]:
        return self._kb.list_ai_research_runs(study_id)


__all__ = ["ResearchTraceService"]

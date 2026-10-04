"""Research events: what happened, as facts.

Phase 4 (§11/§12). A research run leaves a trail a person can watch:
`IDENTITY_RESOLVED`, `KNOWLEDGE_CHECKED`, `REACTION_CREATED`,
`CONFLICT_DETECTED`, `REPORT_GENERATED`, ... Each event is a **fact
statement** -- "Reaction RXN-001 created", never "AI believes this
reaction is probably correct" -- and every event that concerns an object
names it by id (entity / reaction / evidence / paper / document / route),
never by copying the object.

Events are appended to a JSONL log (append-only, one JSON object per
line) and held in memory for polling. They are observability, not
knowledge: the knowledge base remains the only source of truth, and
deleting an event log loses no fact that matters.
"""

from __future__ import annotations

import json
import threading
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from crea.core.ids import content_hash
from crea.core.provenance import utc_now_iso


class ResearchEventType(str, Enum):
    """The fact vocabulary (§11). Progress and outcome, nothing invented."""

    RESEARCH_STARTED = "research_started"
    DOCUMENT_PARSED = "document_parsed"
    IDENTITY_RESOLVED = "identity_resolved"
    KNOWLEDGE_CHECKED = "knowledge_checked"
    KNOWLEDGE_GAP_FOUND = "knowledge_gap_found"
    LITERATURE_SEARCH_STARTED = "literature_search_started"
    LITERATURE_SEARCH_COMPLETED = "literature_search_completed"
    MENTION_RESOLVED = "mention_resolved"
    REACTION_CREATED = "reaction_created"
    EVIDENCE_ADDED = "evidence_added"
    CONFLICT_DETECTED = "conflict_detected"
    ROUTE_VALIDATED = "route_validated"
    REPORT_GENERATED = "report_generated"
    RESEARCH_COMPLETED = "research_completed"
    RESEARCH_PARTIAL = "research_partial"
    RESEARCH_CANCELLED = "research_cancelled"
    RESEARCH_INTERRUPTED = "research_interrupted"


class ResearchEvent(BaseModel):
    """One fact about one research run.

    `message` is a sentence stating what happened. The id lists are pure
    references (§12): the event names objects, it never embeds them.
    """

    event_id: str = ""
    job_id: str = ""
    session_id: str = ""
    type: ResearchEventType
    at: str = Field(default_factory=utc_now_iso)
    message: str = ""
    entity_ids: list[str] = Field(default_factory=list)
    reaction_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    paper_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    route_ids: list[str] = Field(default_factory=list)

    def with_id(self) -> "ResearchEvent":
        self.event_id = content_hash(
            f"{self.job_id}|{self.type.value}|{self.at}|{self.message}", 16)
        return self


class EventLog:
    """Append-only JSONL event log, thread-safe (§35: JSONL is enough).

    One file, one JSON object per line. `for_job` filters; polling reads
    the in-memory list. A crash loses at most the events not yet flushed
    -- never knowledge (that lives in the database).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._events: list[ResearchEvent] = []
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: ResearchEvent) -> ResearchEvent:
        if not event.event_id:
            event = event.with_id()
        with self._lock:
            self._events.append(event)
            if self._path is not None:
                with open(self._path, "a", encoding="utf-8") as handle:
                    handle.write(event.model_dump_json() + "\n")
        return event

    def for_job(self, job_id: str) -> list[ResearchEvent]:
        with self._lock:
            return [e for e in self._events if e.job_id == job_id]

    def all(self) -> list[ResearchEvent]:
        with self._lock:
            return list(self._events)


__all__ = ["EventLog", "ResearchEvent", "ResearchEventType"]

"""A research session: the container for one line of research (Stage 7.1).

This is CREA's **"Research Session"**: a durable record of a research process --
a question, references to the molecules / reactions / evidence / literature it
looks at, a timeline of activities, and the artifacts those activities produced.

Name note: the class is `ResearchStudy`, not `ResearchSession`, because
`core.models.session.ResearchSession` already exists (Phase 3.3) as a *workflow
run transcript* -- a different object. Two classes with one name would be
exactly the second-model confusion this codebase avoids.

It is a process container, never a second chemistry database (§三): references
point at existing objects and are never copied; only AI results, which cannot be
recomputed from the database, are stored as artifacts (§十五/§十七).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.provenance import utc_now_iso


class StudyStatus(str, Enum):
    """The lifecycle of a research session (§五). ACTIVE does NOT mean an agent
    is running (§六) -- a study is a container; an agent run is one event in it."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


#: Allowed status transitions (§四十七). ARCHIVED is terminal; COMPLETED cannot
#: return to ACTIVE in this first version.
_ALLOWED_TRANSITIONS = {
    StudyStatus.ACTIVE: {StudyStatus.ACTIVE, StudyStatus.PAUSED,
                         StudyStatus.COMPLETED, StudyStatus.ARCHIVED},
    StudyStatus.PAUSED: {StudyStatus.PAUSED, StudyStatus.ACTIVE,
                         StudyStatus.COMPLETED, StudyStatus.ARCHIVED},
    StudyStatus.COMPLETED: {StudyStatus.COMPLETED, StudyStatus.ARCHIVED},
    StudyStatus.ARCHIVED: {StudyStatus.ARCHIVED},
}


def status_transition_allowed(current: StudyStatus, target: StudyStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS.get(current, set())


class ReferenceKind(str, Enum):
    """The kinds of existing object a session may reference (§十三)."""

    MOLECULE = "molecule"       # a ChemicalEntity
    REACTION = "reaction"       # a ReactionTemplate
    LITERATURE = "literature"   # a Paper
    EVIDENCE = "evidence"       # a ReactionEvidence record


class StudyEventType(str, Enum):
    """The timeline vocabulary (§三十六). Domain operations create these."""

    STUDY_CREATED = "study_created"
    TITLE_CHANGED = "title_changed"
    STATUS_CHANGED = "status_changed"
    MOLECULE_ADDED = "molecule_added"
    REACTION_ADDED = "reaction_added"
    EVIDENCE_ADDED = "evidence_added"
    LITERATURE_ADDED = "literature_added"
    REFERENCE_REMOVED = "reference_removed"
    TRANSFORMATION_SEARCH = "transformation_search"
    ROUTE_EXPLORED = "route_explored"
    MODIFICATION_ANALYZED = "modification_analyzed"
    ROUTE_REASONED = "route_reasoned"
    AGENT_RUN = "agent_run"
    NOTE_ADDED = "note_added"
    #: Stage 7.2: the workflow lifecycle, recorded on the same timeline (§六十二).
    WORKFLOW_CREATED = "workflow_created"
    WORKFLOW_STARTED = "workflow_started"
    WORKFLOW_PAUSED = "workflow_paused"
    WORKFLOW_RESUMED = "workflow_resumed"
    WORKFLOW_COMPLETED = "workflow_completed"
    WORKFLOW_CANCELLED = "workflow_cancelled"
    WORKFLOW_BLOCKED = "workflow_blocked"
    TASK_CREATED = "task_created"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_BLOCKED = "task_blocked"
    TASK_SKIPPED = "task_skipped"


class ArtifactType(str, Enum):
    """A research result (§十五). Stage 7.2 adds the deterministic task results."""

    MODIFICATION_ANALYSIS = "modification_analysis"
    ROUTE_REASONING = "route_reasoning"
    AGENT_RESULT = "agent_result"
    GRAPH_SNAPSHOT = "graph_snapshot"
    ROUTE_EXPLORATION = "route_exploration"
    #: Stage 7.2: deterministic task results.
    MOLECULE_INSPECTION_RESULT = "molecule_inspection_result"
    TRANSFORMATION_SEARCH_RESULT = "transformation_search_result"
    REACTION_COMPARISON_RESULT = "reaction_comparison_result"
    LITERATURE_SEARCH_RESULT = "literature_search_result"
    EVIDENCE_REVIEW_RESULT = "evidence_review_result"
    #: Stage 7.3: a generated study report, kept as an artifact (not a table).
    RESEARCH_REPORT = "research_report"
    #: Stage 7.4: a final audit of a study (and one of its reports).
    FINAL_AUDIT = "final_audit"
    #: M1.5: the study context profile (goal, constraints), its evidence
    #: references, and one session's execution summary -- results that cannot
    #: be recomputed (§十七), so they are artifacts on the existing study
    #: container rather than new tables.
    STUDY_CONTEXT = "study_context"
    STUDY_EVIDENCE_REF = "study_evidence_ref"
    SESSION_SUMMARY = "session_summary"


class StudyReference(BaseModel):
    """A pointer at an existing object the session is studying (§十三)."""

    reference_id: str
    kind: ReferenceKind
    object_id: str
    label: str = ""
    added_at: str = Field(default_factory=utc_now_iso)
    note: str = ""


class StudyEvent(BaseModel):
    """One entry in the research timeline (§三十六). Append-only (§六十五)."""

    event_id: str
    event_type: StudyEventType
    at: str = Field(default_factory=utc_now_iso)
    summary: str = ""
    reference_id: str = ""
    artifact_id: str = ""
    #: For NOTE_ADDED, the user's own words (§六十).
    note: str = ""


class StudyArtifact(BaseModel):
    """A research result stored because it cannot be regenerated (§十七)."""

    artifact_id: str
    artifact_type: ArtifactType
    created_at: str = Field(default_factory=utc_now_iso)
    label: str = ""
    #: The structured result (e.g. a ModificationAnalysis). Never a raw prompt
    #: or a hidden chain of thought (§十八/§六十七).
    content: dict = Field(default_factory=dict)


class ResearchStudy(BaseModel):
    """The session metadata (§四)."""

    study_id: str
    title: str = ""
    question: str = ""
    status: StudyStatus = StudyStatus.ACTIVE
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    start_molecule_id: str = ""
    target_molecule_id: str = ""


class ResearchStudyDetail(BaseModel):
    """Everything a session holds, for reading it back (§二十六/§三十四)."""

    study: ResearchStudy
    references: list[StudyReference] = Field(default_factory=list)
    events: list[StudyEvent] = Field(default_factory=list)
    artifacts: list[StudyArtifact] = Field(default_factory=list)


__all__ = [
    "ArtifactType",
    "ReferenceKind",
    "ResearchStudy",
    "ResearchStudyDetail",
    "StudyArtifact",
    "StudyEvent",
    "StudyEventType",
    "StudyReference",
    "StudyStatus",
    "status_transition_allowed",
]

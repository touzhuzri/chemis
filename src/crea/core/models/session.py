"""One research run's process record -- explicitly not a memory.

A `ResearchSession` saves what happened during one research task: which
steps ran, what was found, which evidence and routes came out the other
end, and how far the workflow got. It exists so a finished task can be
read back after the fact.

What it is not: a user profile, a history, or anything that accumulates
across tasks. There is no user id here, no preference field and no place
to keep prior questions -- because a session that remembers people is a
different product, and inventing one inside the data model is how that
product arrives unnoticed. One task, one session, and the session dies
with the process that created it.

`WorkflowStatus` names the stages of a workflow-mode run. The first four
are progress, the last two are outcomes; a run in this mode never reports
a third outcome, so a failure always ends as `PARTIAL` with whatever was
collected rather than a report that looks complete.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.models.research import Finding, ResearchStep, ResearchTask, SourceRef
from crea.core.provenance import utc_now_iso


class WorkflowStatus(str, Enum):
    """How far a workflow-mode research run has got, and how it ended.

    Progress stages are ordered -- `RESEARCHING` before
    `COLLECTING_EVIDENCE` before `VALIDATING` before `REPORTING` -- and a
    session's status records the furthest stage reached, not a replay of
    every hop. The terminal pair is the whole outcome vocabulary: in
    workflow mode there is no `FAILED`, because "FAILED with a complete
    looking report" is the failure this phase forbids. An interrupted run
    is `PARTIAL`, and its report says so.
    """

    RESEARCHING = "researching"
    COLLECTING_EVIDENCE = "collecting_evidence"
    VALIDATING = "validating"
    REPORTING = "reporting"
    COMPLETED = "completed"
    PARTIAL = "partial"

    @property
    def is_terminal(self) -> bool:
        return self in (WorkflowStatus.COMPLETED, WorkflowStatus.PARTIAL)


class ResearchSession(BaseModel):
    """Everything one research task produced, for reading afterwards.

    The fields are a transcript, not a cache: `steps` is what ran,
    `findings` is what the report concluded, `evidence_ids` and
    `route_ids` point at what the tools produced, and `status` says how
    far the run got. Nothing here feeds a future task.
    """

    session_id: str
    task: ResearchTask
    created_at: str = Field(default_factory=utc_now_iso)
    steps: list[ResearchStep] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    route_ids: list[str] = Field(default_factory=list)
    #: The reaction intelligence this run produced or found (Phase 3.10):
    #: reactions registered, entities involved, mentions resolved. Deduped,
    #: in deterministic order, and pointing at this run only -- like the
    #: rest of the session, they are a transcript, never a memory.
    reaction_ids: list[str] = Field(default_factory=list)
    chemical_entity_ids: list[str] = Field(default_factory=list)
    chemical_mention_ids: list[str] = Field(default_factory=list)
    #: The internal phase log (researching.identity, researching.reactions,
    #: ...). The external state machine stays the six `WorkflowStatus`
    #: values; this records the fine-grained path through it.
    phases: list[str] = Field(default_factory=list)
    status: WorkflowStatus = WorkflowStatus.RESEARCHING


__all__ = ["ResearchSession", "WorkflowStatus"]

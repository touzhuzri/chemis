"""A bounded research workflow: a task DAG over a research session (Stage 7.2).

A workflow belongs to a research session (7.1) and holds a **finite** set of
tasks. Each task calls one existing service; its result is stored as an
artifact bound to the task. Tasks form a DAG (`depends_on`), checked for cycles
at creation, and are executed **one at a time, on explicit request** -- never in
an automatic loop.

Name note: the classes are `TaskWorkflow` / `WorkflowTask`, not
`ResearchWorkflow` / `ResearchTask`, because those names already exist -- a
`ResearchTask` is one research question (with its report), and a
`ResearchWorkflow` is the single-run pipeline. Reusing the names would be a
second model wearing the first one's.

A workflow is not an agent loop: the workflow orchestrates, a service (or the
agent) is the worker. No model authors the task set.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.provenance import utc_now_iso

#: The hard bounds. Explicit, tested.
MAX_TASKS = 20
MAX_WORKFLOW_DEPTH = 8
MAX_TASK_RETRIES = 2


class WorkflowStatus(str, Enum):
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"      # cannot continue (missing data / input / tool error)
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskStatus(str, Enum):
    PENDING = "pending"      # dependencies not yet satisfied
    READY = "ready"          # dependencies satisfied, not yet run
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"        # the tool/service errored (e.g. provider unavailable)
    BLOCKED = "blocked"      # cannot run: missing input
    SKIPPED = "skipped"      # the user chose to skip it
    CANCELLED = "cancelled"


class TaskType(str, Enum):
    """Each maps to one existing service (§七)."""

    MOLECULE_INSPECTION = "molecule_inspection"
    TRANSFORMATION_SEARCH = "transformation_search"
    REACTION_COMPARISON = "reaction_comparison"
    LITERATURE_SEARCH = "literature_search"
    EVIDENCE_REVIEW = "evidence_review"
    ROUTE_EXPLORATION = "route_exploration"
    MODIFICATION_ANALYSIS = "modification_analysis"   # AI (6.2)
    ROUTE_REASONING = "route_reasoning"               # AI (6.3)
    AGENT_ANALYSIS = "agent_analysis"                 # AI (6.4, read-only)


#: Statuses that satisfy a dependency.
SATISFIED = (TaskStatus.COMPLETED, TaskStatus.SKIPPED)


class WorkflowTask(BaseModel):
    """One unit of research work.

    `input_refs` are the parameters and existing-object ids the task uses;
    `output_artifact_ids` point at the artifacts it produced (never a copy of
    the result).
    """

    task_id: str
    workflow_id: str
    task_type: TaskType
    status: TaskStatus = TaskStatus.PENDING
    title: str = ""
    input_refs: dict = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    output_artifact_ids: list[str] = Field(default_factory=list)
    attempt_count: int = 0
    error: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class TaskWorkflow(BaseModel):
    workflow_id: str
    study_id: str
    template: str = ""
    title: str = ""
    status: WorkflowStatus = WorkflowStatus.READY
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class TaskWorkflowDetail(BaseModel):
    workflow: TaskWorkflow
    #: Deterministic order: by created order.
    tasks: list[WorkflowTask] = Field(default_factory=list)


__all__ = [
    "MAX_TASK_RETRIES",
    "MAX_TASKS",
    "MAX_WORKFLOW_DEPTH",
    "SATISFIED",
    "TaskStatus",
    "TaskType",
    "TaskWorkflow",
    "TaskWorkflowDetail",
    "WorkflowStatus",
    "WorkflowTask",
]

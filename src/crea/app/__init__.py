"""Application layer: use cases, DTOs, orchestration (Phase 4 §3).

No RDKit, no SQL, no HTTP providers, no HTML, no LLM implementations.
The engine below (services / repositories / agent / report) does the
research; this layer shapes it for a human-facing workbench.
"""

from crea.app.events import EventLog, ResearchEvent, ResearchEventType
from crea.app.jobs import JobManager, JobStatus, ResearchCancelled
from crea.app.knowledge import KnowledgeApplication
from crea.app.research import ResearchApplication

__all__ = [
    "EventLog",
    "JobManager",
    "JobStatus",
    "KnowledgeApplication",
    "ResearchApplication",
    "ResearchCancelled",
    "ResearchEvent",
    "ResearchEventType",
]

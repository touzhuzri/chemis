"""Research-session use cases (CREA Stage 7.1).

Opens a short-lived knowledge base and delegates to `ResearchStudyService`.
Persistent (this is the one Stage 7 object that *is* stored), but it stores a
process record, never chemistry (§三).
"""

from __future__ import annotations

from pathlib import Path


class ResearchStudyApplication:
    """Create, read, update and annotate research sessions."""

    def __init__(self, db_path) -> None:
        self._db_path = Path(db_path)

    def _with(self, fn):
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.research_study import ResearchStudyService

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        try:
            return fn(ResearchStudyService(kb))
        finally:
            kb.close()

    def create(self, **kwargs):
        return self._with(lambda svc: svc.create(**kwargs))

    def get(self, study_id: str):
        return self._with(lambda svc: svc.get(study_id))

    def detail(self, study_id: str):
        return self._with(lambda svc: svc.detail(study_id))

    def list(self, **kwargs):
        return self._with(lambda svc: svc.list(**kwargs))

    def update(self, study_id: str, **kwargs):
        return self._with(lambda svc: svc.update(study_id, **kwargs))

    def add_reference(self, study_id: str, kind, object_id: str, **kwargs):
        return self._with(lambda svc: svc.add_reference(study_id, kind, object_id, **kwargs))

    def remove_reference(self, study_id: str, reference_id: str) -> bool:
        return self._with(lambda svc: svc.remove_reference(study_id, reference_id))

    def append_event(self, study_id: str, event_type, **kwargs):
        return self._with(lambda svc: svc.append_event(study_id, event_type, **kwargs))

    def add_note(self, study_id: str, note: str):
        return self._with(lambda svc: svc.add_note(study_id, note))

    def add_artifact(self, study_id: str, artifact_type, content: dict, **kwargs):
        return self._with(lambda svc: svc.add_artifact(
            study_id, artifact_type, content, **kwargs))


__all__ = ["ResearchStudyApplication"]

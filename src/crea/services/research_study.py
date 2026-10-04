"""Research-session service (CREA Stage 7.1).

The container for one line of research: create it, attach references to
existing objects, record timeline events, store AI artifacts, and change its
status. It never creates, modifies or deletes the objects it references
(§三十二/§四十) -- a reference is validated against the knowledge base, and a
missing one is `REFERENCE_NOT_FOUND`, never auto-created.

`updated_at` moves on every change (§四十八); timestamps are UTC (§四十九).
"""

from __future__ import annotations

import time

from crea.core.errors import ResearchStudyError
from crea.core.ids import content_hash
from crea.core.models.research_study import (
    ReferenceKind,
    ResearchStudy,
    ResearchStudyDetail,
    StudyArtifact,
    StudyEvent,
    StudyEventType,
    StudyReference,
    StudyStatus,
    status_transition_allowed,
)
from crea.core.provenance import utc_now_iso


class ResearchStudyService:
    """Create and evolve a research session over a knowledge base."""

    def __init__(self, kb) -> None:
        self._kb = kb

    # -- lifecycle ----------------------------------------------------------

    def create(self, *, title: str = "", question: str = "",
               start_molecule_id: str = "", target_molecule_id: str = "") -> ResearchStudy:
        now = utc_now_iso()
        study = ResearchStudy(
            study_id="study-" + content_hash(f"{title}|{question}|{now}", 16),
            title=title.strip() or "Untitled research session",
            question=question, status=StudyStatus.ACTIVE,
            created_at=now, updated_at=now,
            start_molecule_id=start_molecule_id,
            target_molecule_id=target_molecule_id)
        self._kb.save_study(study)
        self.append_event(study.study_id, StudyEventType.STUDY_CREATED,
                          summary="Research session created")
        return self._kb.get_study(study.study_id)

    def get(self, study_id: str) -> ResearchStudy:
        study = self._kb.get_study(study_id)
        if study is None:
            raise ResearchStudyError(
                "STUDY_NOT_FOUND", f"no research session {study_id!r}")
        return study

    def detail(self, study_id: str) -> ResearchStudyDetail:
        detail = self._kb.get_study_detail(study_id)
        if detail is None:
            raise ResearchStudyError(
                "STUDY_NOT_FOUND", f"no research session {study_id!r}")
        return detail

    def list(self, *, status: StudyStatus | None = None, limit: int = 100,
             offset: int = 0) -> list[ResearchStudy]:
        return self._kb.list_studies(
            status.value if status is not None else None, limit, offset)

    def update(self, study_id: str, *, title: str | None = None,
               status: StudyStatus | None = None) -> ResearchStudy:
        """Change the title or status. `question` is fixed (§二十七)."""
        study = self.get(study_id)
        if title is not None and title.strip() and title.strip() != study.title:
            study.title = title.strip()
            self._touch(study)
            self._kb.save_study(study)
            self.append_event(study_id, StudyEventType.TITLE_CHANGED,
                              summary=f"title changed to {study.title!r}")
        if status is not None and status is not study.status:
            if not status_transition_allowed(study.status, status):
                raise ResearchStudyError(
                    "INVALID_STATUS_TRANSITION",
                    f"cannot move a session from {study.status.value} to "
                    f"{status.value}")
            previous = study.status
            study.status = status
            self._touch(study)
            self._kb.save_study(study)
            self.append_event(
                study_id, StudyEventType.STATUS_CHANGED,
                summary=f"status changed from {previous.value} to {status.value}")
        return self._kb.get_study(study_id)

    # -- references ---------------------------------------------------------

    def add_reference(self, study_id: str, kind: ReferenceKind, object_id: str,
                      *, label: str = "", note: str = "") -> StudyReference:
        """Attach an existing object. A missing object is never created (§四十)."""
        study = self.get(study_id)
        self._require_object(kind, object_id)
        reference = StudyReference(
            reference_id="ref-" + content_hash(f"{study_id}|{kind.value}|{object_id}", 16),
            kind=kind, object_id=object_id, label=label, note=note)
        self._kb.save_study_reference(study_id, reference)
        self._touch(study)
        self._kb.save_study(study)
        event = {ReferenceKind.MOLECULE: StudyEventType.MOLECULE_ADDED,
                 ReferenceKind.REACTION: StudyEventType.REACTION_ADDED,
                 ReferenceKind.LITERATURE: StudyEventType.LITERATURE_ADDED,
                 ReferenceKind.EVIDENCE: StudyEventType.EVIDENCE_ADDED}[kind]
        self.append_event(study_id, event, summary=f"{kind.value} {object_id} added",
                          reference_id=reference.reference_id)
        return reference

    def remove_reference(self, study_id: str, reference_id: str) -> bool:
        """Remove the reference only -- the source object is untouched (§三十二)."""
        study = self.get(study_id)
        removed = self._kb.delete_study_reference(study_id, reference_id)
        if removed:
            self._touch(study)
            self._kb.save_study(study)
            self.append_event(study_id, StudyEventType.REFERENCE_REMOVED,
                              summary="reference removed", reference_id=reference_id)
        return removed

    # -- events + artifacts -------------------------------------------------

    def append_event(self, study_id: str, event_type: StudyEventType,
                     *, summary: str = "", reference_id: str = "",
                     artifact_id: str = "", note: str = "") -> StudyEvent:
        event = StudyEvent(
            event_id="ev-" + content_hash(
                f"{study_id}|{event_type.value}|{summary}|{utc_now_iso()}|"
                f"{time.time_ns()}", 16),
            event_type=event_type, summary=summary, reference_id=reference_id,
            artifact_id=artifact_id, note=note)
        return self._kb.append_study_event(study_id, event)

    def add_note(self, study_id: str, note: str) -> StudyEvent:
        """A user's own note (§六十). Never written by the AI on its own."""
        self.get(study_id)
        return self.append_event(study_id, StudyEventType.NOTE_ADDED,
                                 summary=note.strip()[:200], note=note.strip())

    def add_artifact(self, study_id: str, artifact_type, content: dict,
                     *, label: str = "") -> StudyArtifact:
        """Store a research result that cannot be recomputed (§十七).

        `content` is the structured result (e.g. a ModificationAnalysis), never
        a raw prompt or a hidden chain of thought (§十八/§六十七).
        """
        study = self.get(study_id)
        artifact = StudyArtifact(
            artifact_id="art-" + content_hash(
                f"{study_id}|{artifact_type.value}|{len(content)}|"
                f"{utc_now_iso()}|{time.time_ns()}", 16),
            artifact_type=artifact_type, label=label, content=content)
        self._kb.save_study_artifact(study_id, artifact)
        self._touch(study)
        self._kb.save_study(study)
        return artifact

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _touch(study: ResearchStudy) -> None:
        study.updated_at = utc_now_iso()

    def _require_object(self, kind: ReferenceKind, object_id: str) -> None:
        """The referenced object must already exist (§四十)."""
        from crea.repositories.errors import NotFound

        exists = False
        try:
            if kind is ReferenceKind.MOLECULE:
                exists = self._kb.get_entity(object_id) is not None
            elif kind is ReferenceKind.REACTION:
                from crea.services.reaction_library import get_template

                exists = get_template(object_id) is not None
            elif kind is ReferenceKind.LITERATURE:
                exists = self._kb.store.get_paper(object_id) is not None
            elif kind is ReferenceKind.EVIDENCE:
                exists = self._kb.get_reaction_evidence(object_id) is not None
        except NotFound:
            exists = False
        if not exists:
            raise ResearchStudyError(
                "REFERENCE_NOT_FOUND",
                f"no {kind.value} {object_id!r} in the knowledge base; the "
                f"session references existing objects and never creates them")


__all__ = ["ResearchStudyService"]

"""Final-audit use cases (CREA Stage 7.4).

Runs the deterministic audit and stores it as a `StudyArtifact` of type
`FINAL_AUDIT`. Versions accumulate -- auditing again never overwrites a prior
audit (§七十五). Offline, no LLM, no repair.
"""

from __future__ import annotations

from pathlib import Path


class FinalAuditApplication:
    """Audit a session and read audits back, as artifacts."""

    def __init__(self, db_path) -> None:
        self._db_path = Path(db_path)

    def _with(self, fn):
        from crea.services.knowledge_base import KnowledgeBase

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        try:
            return fn(kb)
        finally:
            kb.close()

    def run(self, study_id: str, report_artifact_id: str = "") -> dict:
        from crea.core.models.research_study import ArtifactType
        from crea.services.final_audit import FinalAuditService
        from crea.services.research_study import ResearchStudyService

        def go(kb):
            result = FinalAuditService(kb).audit(study_id, report_artifact_id)
            artifact = ResearchStudyService(kb).add_artifact(
                study_id, ArtifactType.FINAL_AUDIT,
                result.model_dump(mode="json"), label=result.audit_id)
            return self._shape(artifact.artifact_id, result)

        return self._with(go)

    def latest(self, study_id: str):
        from crea.core.models.final_audit import FinalAuditResult
        from crea.core.models.research_study import ArtifactType

        def go(kb):
            detail = kb.get_study_detail(study_id)
            if detail is None:
                from crea.core.errors import ResearchStudyError

                raise ResearchStudyError(
                    "STUDY_NOT_FOUND", f"no research session {study_id!r}")
            audits = [a for a in detail.artifacts
                      if a.artifact_type is ArtifactType.FINAL_AUDIT]
            if not audits:
                return None
            artifact = audits[-1]
            return self._shape(artifact.artifact_id,
                               FinalAuditResult.model_validate(artifact.content))

        return self._with(go)

    def get(self, study_id: str, artifact_id: str):
        from crea.core.models.final_audit import FinalAuditResult

        def go(kb):
            artifact = kb.get_study_artifact(study_id, artifact_id)
            if artifact is None:
                return None
            return self._shape(artifact.artifact_id,
                               FinalAuditResult.model_validate(artifact.content))

        return self._with(go)

    @staticmethod
    def _shape(artifact_id: str, result) -> dict:
        return {"artifact_id": artifact_id,
                "audit_version": result.audit_version,
                "status": result.status.value,
                "audit": result.model_dump(mode="json"),
                "summary": result.summary}


__all__ = ["FinalAuditApplication"]

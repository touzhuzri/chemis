"""Study-report use cases (CREA Stage 7.3).

Generate a report for a session and read it back. The builder is read-only; the
one write this stage performs is saving the report as a `StudyArtifact` of type
`RESEARCH_REPORT`, so versions accumulate and nothing is overwritten (§四十三/
§四十四). No search, no agent, no LLM is re-run.
"""

from __future__ import annotations

from pathlib import Path


class ResearchReportApplication:
    """Generate and read a session's report, as artifacts."""

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

    def generate(self, study_id: str) -> dict:
        """Build the report and store it as a new artifact (never overwriting)."""
        from crea.core.models.research_study import ArtifactType
        from crea.report.study import render_study_markdown
        from crea.services.research_study import ResearchStudyService
        from crea.services.study_report import StudyReportBuilder

        def run(kb):
            report = StudyReportBuilder(kb).build(study_id)
            artifact = ResearchStudyService(kb).add_artifact(
                study_id, ArtifactType.RESEARCH_REPORT,
                report.model_dump(mode="json"),
                label=f"{report.report_id}")
            return self._shape(artifact.artifact_id, report,
                               render_study_markdown(report))

        return self._with(run)

    def latest(self, study_id: str):
        """The most recent report artifact for this session, or None."""
        from crea.core.models.research_study import ArtifactType
        from crea.report.study import render_study_markdown
        from crea.core.models.study_report import StudyReport

        def run(kb):
            detail = kb.get_study_detail(study_id)
            if detail is None:
                from crea.core.errors import ResearchStudyError

                raise ResearchStudyError(
                    "STUDY_NOT_FOUND", f"no research session {study_id!r}")
            reports = [a for a in detail.artifacts
                       if a.artifact_type is ArtifactType.RESEARCH_REPORT]
            if not reports:
                return None
            artifact = reports[-1]
            report = StudyReport.model_validate(artifact.content)
            return self._shape(artifact.artifact_id, report,
                               render_study_markdown(report))

        return self._with(run)

    def get(self, study_id: str, artifact_id: str):
        """One stored report version, or None."""
        from crea.core.models.study_report import StudyReport
        from crea.report.study import render_study_markdown

        def run(kb):
            artifact = kb.get_study_artifact(study_id, artifact_id)
            if artifact is None:
                return None
            report = StudyReport.model_validate(artifact.content)
            return self._shape(artifact.artifact_id, report,
                               render_study_markdown(report))

        return self._with(run)

    @staticmethod
    def _shape(artifact_id: str, report, markdown: str) -> dict:
        return {"artifact_id": artifact_id,
                "report_version": report.report_version,
                "status": "generated",
                "report": report.model_dump(mode="json"),
                "markdown": markdown}


__all__ = ["ResearchReportApplication"]

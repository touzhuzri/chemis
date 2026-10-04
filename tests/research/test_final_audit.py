"""Final audit (Stage 7.4).

Deterministic, offline, read-only. It reports internal consistency -- dangling
references FAIL, known limitations WARN -- and never repairs, never reaches the
network, never calls a model, and never claims scientific verification.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from crea.core.models.final_audit import AUDIT_VERSION, AuditCheckStatus, AuditStatus
from crea.core.models.research_study import ArtifactType, ReferenceKind
from crea.services.final_audit import FinalAuditService
from crea.services.knowledge_base import KnowledgeBase
from crea.services.research_study import ResearchStudyService


@pytest.fixture()
def env(tmp_path):
    kb = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    kb.initialize()
    study = ResearchStudyService(kb).create(title="S", question="Q")
    return kb, study.study_id


def _check(result, check_id: str):
    return next(c for c in result.checks if c.check_id == check_id)


def _evidence(kb, template_id="primary-alcohol-oxidation", paper_id="paper-a"):
    from crea.core.models.literature import Identifiers, Paper
    from crea.core.models.reaction_evidence import ReactionEvidenceType
    from crea.services.literature_record import LiteratureRecordService
    from crea.services.reaction_evidence import ReactionEvidenceService

    LiteratureRecordService(kb).create(Paper(
        paper_id=paper_id, title="T", identifiers=Identifiers(doi="10.1000/abc"),
        retrieved_at="2026-01-01"))
    return ReactionEvidenceService(kb=kb).link_literature(
        template_id, paper_id=paper_id,
        evidence_type=ReactionEvidenceType.TRANSFORMATION_SUPPORT,
        support_scope="s")[0]


# -- 1. a clean-ish study passes with warnings ------------------------------


def test_audit_runs_all_checks(env):
    kb, study_id = env
    result = FinalAuditService(kb).audit(study_id)
    assert result.audit_version == AUDIT_VERSION
    assert result.summary["total_checks"] >= 15
    assert result.summary["failures"] == 0
    assert result.status in (AuditStatus.PASS, AuditStatus.PASS_WITH_WARNINGS)
    # no report yet -> a warning, never a failure
    assert _check(result, "report_integrity").status is AuditCheckStatus.WARN
    kb.close()


# -- 2. a dangling reference FAILS ------------------------------------------


def test_dangling_literature_reference_fails(env):
    kb, study_id = env
    # link evidence to a paper, then remove the paper so the reference dangles
    record = _evidence(kb)
    ResearchStudyService(kb).add_reference(
        study_id, ReferenceKind.EVIDENCE, record.evidence_id)
    # forge a dangling state: an evidence record whose literature is gone
    kb._store._execute("DELETE FROM papers WHERE paper_id = ?", ("paper-a",))
    result = FinalAuditService(kb).audit(study_id)
    assert result.status is AuditStatus.FAIL
    assert _check(result, "evidence_chain").status is AuditCheckStatus.FAIL
    assert result.failures
    kb.close()


def test_missing_referenced_object_is_reported(env):
    kb, study_id = env
    # a reference to a reaction that the library does not hold
    from crea.repositories.errors import RepositoryError

    # add via the store directly to bypass the reference gate (simulate rot)
    kb._store.save_study_reference(study_id, _forged_reference(study_id))
    result = FinalAuditService(kb).audit(study_id)
    assert _check(result, "reference_integrity").status is AuditCheckStatus.FAIL
    kb.close()


def _forged_reference(study_id: str):
    from crea.core.models.research_study import StudyReference

    return StudyReference(reference_id="ref-forged", kind=ReferenceKind.REACTION,
                          object_id="no-such-template")


# -- 3. literature <-> evidence reverse must agree ---------------------------


def test_literature_evidence_reverse_agrees(env):
    kb, study_id = env
    record = _evidence(kb)
    ResearchStudyService(kb).add_reference(
        study_id, ReferenceKind.EVIDENCE, record.evidence_id)
    result = FinalAuditService(kb).audit(study_id)
    assert _check(result, "evidence_chain").status is AuditCheckStatus.PASS
    assert _check(result, "literature_evidence_reverse").status is AuditCheckStatus.PASS
    kb.close()


# -- 4. curated stays curated, literature permission is independent ---------


def test_unknown_fulltext_permission_is_a_warning_not_a_failure(env):
    kb, study_id = env
    record = _evidence(kb)
    ResearchStudyService(kb).add_reference(
        study_id, ReferenceKind.EVIDENCE, record.evidence_id)
    result = FinalAuditService(kb).audit(study_id)
    # the paper's permission is unknown; that is a boundary, not corruption
    assert _check(result, "fulltext_permission").status is AuditCheckStatus.WARN
    assert result.status is not AuditStatus.FAIL
    kb.close()


# -- 5. no route is a warning, not a failure --------------------------------


def test_no_route_is_a_warning(env):
    kb, study_id = env
    ResearchStudyService(kb).add_artifact(
        study_id, ArtifactType.ROUTE_EXPLORATION,
        {"complete": [], "partial": [{"route_id": "r", "blocked_reason": "X"}],
         "blocked": []})
    result = FinalAuditService(kb).audit(study_id)
    assert _check(result, "route_consistency").status is AuditCheckStatus.WARN
    assert result.status is not AuditStatus.FAIL
    kb.close()


# -- 6. report source integrity ---------------------------------------------


def test_report_sources_must_resolve(env):
    kb, study_id = env
    ResearchStudyService(kb).add_reference(
        study_id, ReferenceKind.REACTION, "primary-alcohol-oxidation")
    from crea.app.research_report import ResearchReportApplication

    generated = ResearchReportApplication(kb.path).generate(study_id)
    result = FinalAuditService(kb).audit(study_id)
    assert result.report_artifact_id == generated["artifact_id"]
    assert _check(result, "report_integrity").status is AuditCheckStatus.PASS
    kb.close()


def test_report_with_a_dangling_source_fails(env):
    kb, study_id = env
    # a report artifact forged to point at evidence that does not exist
    from crea.core.models.study_report import StudyReport

    report = StudyReport(
        report_id="report-forged", study_id=study_id,
        sections=[_forged_section()])
    ResearchStudyService(kb).add_artifact(
        study_id, ArtifactType.RESEARCH_REPORT, report.model_dump(mode="json"))
    result = FinalAuditService(kb).audit(study_id)
    assert _check(result, "report_integrity").status is AuditCheckStatus.FAIL
    kb.close()


def _forged_section():
    from crea.core.models.study_report import ReportSection, ReportSourceRef, SourceRefType

    return ReportSection(section_id="x", title="X", source_refs=[
        ReportSourceRef(type=SourceRefType.EVIDENCE, id="evidence-nope")])


# -- 7. AI grounding ---------------------------------------------------------


def test_ungrounded_ai_reference_fails(env):
    kb, study_id = env
    ResearchStudyService(kb).add_artifact(
        study_id, ArtifactType.AGENT_RESULT,
        {"answer": {"summary": "x", "claims": [
            {"text": "invented", "claim_type": "fact",
             "reaction_ids": ["RXN-INVENTED"]}]}})
    result = FinalAuditService(kb).audit(study_id)
    assert _check(result, "ai_grounding").status is AuditCheckStatus.FAIL
    kb.close()


def test_grounded_ai_reference_passes(env):
    kb, study_id = env
    ResearchStudyService(kb).add_artifact(
        study_id, ArtifactType.AGENT_RESULT,
        {"answer": {"summary": "x", "claims": [
            {"text": "real", "claim_type": "fact",
             "reaction_ids": ["primary-alcohol-oxidation"]}]}})
    result = FinalAuditService(kb).audit(study_id)
    assert _check(result, "ai_grounding").status is AuditCheckStatus.PASS
    kb.close()


# -- 8. workflow integrity ---------------------------------------------------


def test_blocked_workflow_is_a_warning(env):
    kb, study_id = env
    from crea.services.research_task_executor import ResearchTaskExecutor
    from crea.services.task_workflow import TaskWorkflowService

    svc = TaskWorkflowService(kb, ResearchTaskExecutor(kb))
    wf = svc.create(study_id, tasks=[
        {"key": "ev", "task_type": "evidence_review", "title": "E", "inputs": {}}])
    svc.start(wf.workflow_id)
    svc.execute_task(wf.workflow_id, svc.detail(wf.workflow_id).tasks[0].task_id)
    result = FinalAuditService(kb).audit(study_id)
    assert _check(result, "workflow_integrity").status is AuditCheckStatus.WARN
    kb.close()


# -- 9. determinism, no repair, no LLM/network ------------------------------


def test_audit_is_deterministic_and_does_not_change_the_study(env):
    kb, study_id = env
    ResearchStudyService(kb).add_reference(
        study_id, ReferenceKind.REACTION, "primary-alcohol-oxidation")
    before = ResearchStudyService(kb).detail(study_id)
    first = FinalAuditService(kb).audit(study_id)
    second = FinalAuditService(kb).audit(study_id)
    assert [(c.check_id, c.status) for c in first.checks] == [
        (c.check_id, c.status) for c in second.checks]
    after = ResearchStudyService(kb).detail(study_id)
    assert len(after.artifacts) == len(before.artifacts), "the audit wrote nothing"
    assert len(after.references) == len(before.references)
    kb.close()


def test_audit_statement_never_claims_scientific_truth(env):
    kb, study_id = env
    result = FinalAuditService(kb).audit(study_id)
    assert "not a scientific verification" in result.statement
    kb.close()


def test_unknown_study_is_a_structured_error(env):
    kb, _study = env
    from crea.core.errors import ResearchStudyError

    with pytest.raises(ResearchStudyError):
        FinalAuditService(kb).audit("study-nope")
    kb.close()


# -- 10. API -----------------------------------------------------------------


def test_final_audit_api(env, tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    kb, study_id = env
    ctx = create_context(db_path=kb.path, documents_dir=tmp_path, offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        assert c.get(f"/api/research/sessions/{study_id}/audit").status_code == 404

        ran = c.post(f"/api/research/sessions/{study_id}/audit").json()
        assert ran["audit_version"] == AUDIT_VERSION
        assert ran["status"] in ("pass", "pass_with_warnings", "fail")
        assert ran["audit"]["checks"]

        latest = c.get(f"/api/research/sessions/{study_id}/audit").json()
        assert latest["artifact_id"] == ran["artifact_id"]

        by_id = c.get(
            f"/api/research/sessions/{study_id}/audit/{ran['artifact_id']}").json()
        assert by_id["artifact_id"] == ran["artifact_id"]

        assert c.get("/api/research/sessions/study-nope/audit").status_code == 404
        assert c.get(
            f"/api/research/sessions/{study_id}/audit/art-nope").status_code == 404


def test_audits_accumulate_versions(env):
    kb, study_id = env
    from crea.app.final_audit import FinalAuditApplication

    app = FinalAuditApplication(kb.path)
    a = app.run(study_id)
    b = app.run(study_id)
    assert a["artifact_id"] != b["artifact_id"], "audits never overwrite"
    detail = ResearchStudyService(kb).detail(study_id)
    audits = [x for x in detail.artifacts
              if x.artifact_type is ArtifactType.FINAL_AUDIT]
    assert len(audits) == 2
    kb.close()

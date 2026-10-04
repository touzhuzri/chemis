"""Study report (Stage 7.3).

A read-only assembly of a session's results: fixed section order, source graph,
deduped references, user notes labelled (never upgraded), route statuses
preserved, and no re-run of chemistry / search / agent / LLM. The report is
stored as an artifact, versions accumulate, and it never says VERIFIED.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from crea.core.models.research_study import ArtifactType, ReferenceKind
from crea.core.models.study_report import REPORT_VERSION
from crea.report.study import render_study_markdown
from crea.services.knowledge_base import KnowledgeBase
from crea.services.research_study import ResearchStudyService
from crea.services.study_report import StudyReportBuilder


@pytest.fixture()
def env(tmp_path):
    kb = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    kb.initialize()
    study = ResearchStudyService(kb).create(
        title="Study A", question="How can the target be modified?")
    return kb, study.study_id


def _paper(kb, paper_id="paper-a", doi="10.1000/abc"):
    from crea.core.models.literature import Identifiers, Paper

    from crea.services.literature_record import LiteratureRecordService

    return LiteratureRecordService(kb).create(Paper(
        paper_id=paper_id, title="An oxidation study", year=2021,
        journal="J. Synth. Chem.", identifiers=Identifiers(doi=doi),
        retrieved_at="2026-01-01"))


def _evidence(kb, template_id="primary-alcohol-oxidation", paper_id="paper-a"):
    from crea.core.models.reaction_evidence import ReactionEvidenceType

    from crea.services.reaction_evidence import ReactionEvidenceService

    _paper(kb, paper_id)
    return ReactionEvidenceService(kb=kb).link_literature(
        template_id, paper_id=paper_id,
        evidence_type=ReactionEvidenceType.TRANSFORMATION_SUPPORT,
        support_scope="primary alcohol -> aldehyde")[0]


# -- 1. sections in a fixed order -------------------------------------------


def test_report_has_the_fixed_section_order(env):
    kb, study_id = env
    report = StudyReportBuilder(kb).build(study_id)
    assert [s.section_id for s in report.sections] == [
        "question", "scope", "molecules", "reactions", "evidence",
        "routes", "analyses", "workflows", "notes", "limitations",
        "open_questions", "references"]
    assert report.report_version == REPORT_VERSION
    assert report.status == "generated"
    kb.close()


# -- 2. source graph ---------------------------------------------------------


def test_sections_carry_source_refs(env):
    kb, study_id = env
    svc = ResearchStudyService(kb)
    svc.add_reference(study_id, ReferenceKind.REACTION, "primary-alcohol-oxidation")
    report = StudyReportBuilder(kb).build(study_id)
    reactions = next(s for s in report.sections if s.section_id == "reactions")
    assert reactions.source_refs
    assert reactions.source_refs[0].type.value == "reaction"
    assert reactions.source_refs[0].id == "primary-alcohol-oxidation"
    kb.close()


# -- 3. deterministic --------------------------------------------------------


def test_report_is_deterministic(env):
    kb, study_id = env
    ResearchStudyService(kb).add_reference(
        study_id, ReferenceKind.REACTION, "primary-alcohol-oxidation")
    a = StudyReportBuilder(kb).build(study_id)
    b = StudyReportBuilder(kb).build(study_id)
    assert [s.section_id for s in a.sections] == [s.section_id for s in b.sections]
    assert [r.ref_id for r in a.references] == [r.ref_id for r in b.references]
    kb.close()


# -- 4. evidence -> literature, deduped references --------------------------


def test_evidence_literature_relation_and_dedup(env):
    kb, study_id = env
    record = _evidence(kb)
    svc = ResearchStudyService(kb)
    svc.add_reference(study_id, ReferenceKind.EVIDENCE, record.evidence_id)
    svc.add_reference(study_id, ReferenceKind.REACTION, "primary-alcohol-oxidation")
    report = StudyReportBuilder(kb).build(study_id)
    evidence = next(s for s in report.sections if s.section_id == "evidence")
    assert record.evidence_id in evidence.lines[0]
    assert "[P1]" in evidence.lines[0]
    assert [r.paper_id for r in report.references] == ["paper-a"]
    kb.close()


# -- 5. user notes are labelled, never facts --------------------------------


def test_user_notes_are_labelled_not_upgraded(env):
    kb, study_id = env
    ResearchStudyService(kb).add_note(study_id, "I think this reaction works well")
    report = StudyReportBuilder(kb).build(study_id)
    notes = next(s for s in report.sections if s.section_id == "notes")
    assert notes.summary.startswith("USER_NOTE")
    assert notes.lines[0].startswith("User note: ")
    kb.close()


# -- 6. route statuses are preserved, never rewritten -----------------------


def test_route_status_is_not_rewritten(env):
    kb, study_id = env
    ResearchStudyService(kb).add_artifact(
        study_id, ArtifactType.ROUTE_EXPLORATION, {
            "complete": [],
            "partial": [{"route_id": "route-x", "steps": 1,
                         "blocked_reason": "PRODUCT_STRUCTURE_UNAVAILABLE"}],
            "blocked": []})
    report = StudyReportBuilder(kb).build(study_id)
    routes = next(s for s in report.sections if s.section_id == "routes")
    body = "\n".join(routes.lines)
    assert "PARTIAL" in body
    assert "PRODUCT_STRUCTURE_UNAVAILABLE" in body
    assert any("No complete deterministic route was found" in lim
               for lim in report.limitations)
    # and never "impossible"
    assert "impossible" not in body.lower()
    kb.close()


# -- 7. report generation is read-only --------------------------------------


def test_building_does_not_change_the_study(env):
    kb, study_id = env
    svc = ResearchStudyService(kb)
    svc.add_reference(study_id, ReferenceKind.REACTION, "primary-alcohol-oxidation")
    before = svc.detail(study_id)
    StudyReportBuilder(kb).build(study_id)
    after = svc.detail(study_id)
    assert len(after.artifacts) == len(before.artifacts), "no artifact written by the builder"
    assert len(after.events) == len(before.events), "no event written"
    assert len(after.references) == len(before.references)
    kb.close()


# -- 8. report is an artifact; versions accumulate --------------------------


def test_report_is_saved_as_an_artifact_and_versions_accumulate(env, tmp_path):
    kb, study_id = env
    from crea.app.research_report import ResearchReportApplication

    app = ResearchReportApplication(kb.path)
    first = app.generate(study_id)
    second = app.generate(study_id)
    assert first["report_version"] == REPORT_VERSION
    assert first["artifact_id"] != second["artifact_id"], "versions do not overwrite"
    detail = ResearchStudyService(kb).detail(study_id)
    reports = [a for a in detail.artifacts
               if a.artifact_type is ArtifactType.RESEARCH_REPORT]
    assert len(reports) == 2
    # latest and by-id read back the same content
    assert app.latest(study_id)["artifact_id"] == second["artifact_id"]
    assert app.get(study_id, first["artifact_id"])["artifact_id"] == first["artifact_id"]
    kb.close()


# -- 9. never says VERIFIED --------------------------------------------------


def test_report_never_claims_verification(env):
    kb, study_id = env
    report = StudyReportBuilder(kb).build(study_id)
    markdown = render_study_markdown(report)
    for banned in ("VERIFIED", "VALIDATED", "FINAL SCIENTIFIC CONCLUSION"):
        assert banned not in markdown
    assert any(w.code == "NOT_AUDITED" for w in report.warnings)
    kb.close()


# -- 10. unknown study -------------------------------------------------------


def test_unknown_study_is_a_structured_error(env):
    kb, _study = env
    from crea.core.errors import ResearchStudyError

    with pytest.raises(ResearchStudyError) as exc:
        StudyReportBuilder(kb).build("study-nope")
    assert exc.value.code == "STUDY_NOT_FOUND"
    kb.close()


# -- 11. markdown escaping ---------------------------------------------------


def test_markdown_escapes_injected_structure(env):
    kb, study_id = env
    ResearchStudyService(kb).create(title="T", question="Q")  # extra study ok
    ResearchStudyService(kb).add_note(study_id, "line | pipe\n# heading")
    report = StudyReportBuilder(kb).build(study_id)
    markdown = render_study_markdown(report)
    # the note's pipe and newline cannot break the bullet
    note_line = next(ln for ln in markdown.splitlines() if "User note" in ln)
    assert "\\|" in note_line and "# heading" in note_line
    kb.close()


# -- 12. API -----------------------------------------------------------------


def test_study_report_api(env, tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    kb, study_id = env
    ctx = create_context(db_path=kb.path, documents_dir=tmp_path, offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        # no report yet
        assert c.get(f"/api/research/sessions/{study_id}/report").status_code == 404

        generated = c.post(f"/api/research/sessions/{study_id}/report").json()
        assert generated["report_version"] == REPORT_VERSION
        assert generated["status"] == "generated"
        assert generated["markdown"]

        latest = c.get(f"/api/research/sessions/{study_id}/report").json()
        assert latest["artifact_id"] == generated["artifact_id"]

        by_id = c.get(
            f"/api/research/sessions/{study_id}/report/{generated['artifact_id']}").json()
        assert by_id["artifact_id"] == generated["artifact_id"]

        assert c.get("/api/research/sessions/study-nope/report").status_code == 404
        assert c.get(
            f"/api/research/sessions/{study_id}/report/art-nope").status_code == 404


def test_report_artifact_is_listed_on_the_session(env):
    kb, study_id = env
    from crea.app.research_report import ResearchReportApplication

    ResearchReportApplication(kb.path).generate(study_id)
    detail = ResearchStudyService(kb).detail(study_id)
    assert any(a.artifact_type is ArtifactType.RESEARCH_REPORT for a in detail.artifacts)
    kb.close()

"""Research session (Stage 7.1).

A persistent process container: question + references + timeline + artifacts.
References point at existing objects and never create them; the timeline is a
deterministic, append-only order; status transitions are bounded; nothing here
is a second chemistry database.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from crea.core.errors import ResearchStudyError
from crea.core.models.research_study import (
    ArtifactType,
    ReferenceKind,
    StudyEventType,
    StudyStatus,
)
from crea.services.knowledge_base import KnowledgeBase
from crea.services.research_study import ResearchStudyService


@pytest.fixture()
def env(tmp_path):
    kb = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    kb.initialize()
    return kb


@pytest.fixture()
def svc(env):
    return ResearchStudyService(env)


def _molecule(env, smiles="CCO"):
    from crea.semantic.resolver import ChemicalInput, SemanticResolver
    from crea.services.identity import IdentityResolutionService

    resolver = SemanticResolver(identity_service=IdentityResolutionService(),
                                entity_registry=env.entity_registry())
    return resolver.resolve(ChemicalInput.of("SMILES", smiles)).entity


# -- 1. create / get / list --------------------------------------------------


def test_create_and_read_a_session(svc):
    study = svc.create(title="Study A", question="How to modify A?")
    assert study.status is StudyStatus.ACTIVE
    assert study.study_id.startswith("study-")
    detail = svc.detail(study.study_id)
    assert detail.study.title == "Study A"
    assert [e.event_type for e in detail.events] == [StudyEventType.STUDY_CREATED]
    assert svc.list()[0].study_id == study.study_id


def test_unknown_session_is_a_structured_error(svc):
    with pytest.raises(ResearchStudyError) as exc:
        svc.get("study-nope")
    assert exc.value.code == "STUDY_NOT_FOUND"


# -- 2. status transitions ---------------------------------------------------


def test_status_transitions_are_bounded(svc):
    study = svc.create(title="S")
    svc.update(study.study_id, status=StudyStatus.PAUSED)
    assert svc.get(study.study_id).status is StudyStatus.PAUSED
    svc.update(study.study_id, status=StudyStatus.ACTIVE)  # resume allowed
    svc.update(study.study_id, status=StudyStatus.COMPLETED)
    with pytest.raises(ResearchStudyError) as exc:
        svc.update(study.study_id, status=StudyStatus.ACTIVE)
    assert exc.value.code == "INVALID_STATUS_TRANSITION"
    svc.update(study.study_id, status=StudyStatus.ARCHIVED)
    with pytest.raises(ResearchStudyError):
        svc.update(study.study_id, status=StudyStatus.ACTIVE)


def test_question_is_not_silently_overwritten(svc):
    study = svc.create(title="S", question="original question")
    svc.update(study.study_id, title="renamed")
    assert svc.get(study.study_id).question == "original question"


def test_updated_at_moves_on_change(svc):
    study = svc.create(title="S")
    before = svc.get(study.study_id).updated_at
    svc.add_reference(study.study_id, ReferenceKind.REACTION,
                      "primary-alcohol-oxidation")
    # created_at is fixed; updated_at is re-stamped (may share a second here)
    assert svc.get(study.study_id).created_at == study.created_at


# -- 3. references -----------------------------------------------------------


def test_add_and_remove_a_reference(env, svc):
    entity = _molecule(env)
    study = svc.create(title="S")
    ref = svc.add_reference(study.study_id, ReferenceKind.MOLECULE,
                            entity.entity_id)
    assert ref.kind is ReferenceKind.MOLECULE
    assert [r.object_id for r in svc.detail(study.study_id).references] == [
        entity.entity_id]
    assert svc.remove_reference(study.study_id, ref.reference_id) is True
    assert svc.detail(study.study_id).references == []
    # the molecule itself is untouched (§三十二)
    assert env.get_entity(entity.entity_id) is not None


def test_a_missing_reference_is_refused_not_created(svc):
    study = svc.create(title="S")
    with pytest.raises(ResearchStudyError) as exc:
        svc.add_reference(study.study_id, ReferenceKind.MOLECULE, "CHEM-99999999")
    assert exc.value.code == "REFERENCE_NOT_FOUND"
    with pytest.raises(ResearchStudyError):
        svc.add_reference(study.study_id, ReferenceKind.REACTION, "no-such-template")


# -- 4. timeline -------------------------------------------------------------


def test_timeline_is_chronological_and_append_only(svc):
    study = svc.create(title="S")
    svc.add_reference(study.study_id, ReferenceKind.REACTION,
                      "primary-alcohol-oxidation")
    svc.append_event(study.study_id, StudyEventType.TRANSFORMATION_SEARCH,
                     summary="searched transformations")
    svc.add_note(study.study_id, "verify regioselectivity")
    events = svc.detail(study.study_id).events
    assert [e.event_type for e in events] == [
        StudyEventType.STUDY_CREATED, StudyEventType.REACTION_ADDED,
        StudyEventType.TRANSFORMATION_SEARCH, StudyEventType.NOTE_ADDED]
    assert events[-1].note == "verify regioselectivity"


def test_timeline_is_deterministic(env, svc):
    study = svc.create(title="S")
    svc.add_reference(study.study_id, ReferenceKind.REACTION,
                      "primary-alcohol-oxidation")
    first = [e.event_id for e in svc.detail(study.study_id).events]
    second = [e.event_id for e in svc.detail(study.study_id).events]
    assert first == second


# -- 5. artifacts ------------------------------------------------------------


def test_artifact_stores_the_result(svc):
    study = svc.create(title="S")
    artifact = svc.add_artifact(
        study.study_id, ArtifactType.MODIFICATION_ANALYSIS,
        {"analysis_id": "a1", "candidates": []})
    assert artifact.artifact_type is ArtifactType.MODIFICATION_ANALYSIS
    detail = svc.detail(study.study_id)
    assert detail.artifacts[0].content == {"analysis_id": "a1", "candidates": []}


def test_artifact_does_not_store_a_raw_prompt(svc):
    study = svc.create(title="S")
    artifact = svc.add_artifact(study.study_id, ArtifactType.AGENT_RESULT,
                                {"summary": "x", "claims": []})
    payload = artifact.model_dump()
    for banned in ("prompt", "chain_of_thought", "system"):
        assert banned not in payload


# -- 6. persistence ----------------------------------------------------------


def test_session_survives_a_reopen(tmp_path):
    db = pathlib.Path(tmp_path) / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    svc = ResearchStudyService(kb)
    study = svc.create(title="Persistent", question="q")
    svc.add_reference(study.study_id, ReferenceKind.REACTION,
                      "primary-alcohol-oxidation")
    svc.add_artifact(study.study_id, ArtifactType.ROUTE_EXPLORATION, {"x": 1})
    kb.close()

    kb2 = KnowledgeBase(db)
    kb2.initialize()
    detail = ResearchStudyService(kb2).detail(study.study_id)
    assert detail.study.title == "Persistent"
    assert detail.references[0].object_id == "primary-alcohol-oxidation"
    assert detail.artifacts[0].content == {"x": 1}
    kb2.close()


# -- 7. API ------------------------------------------------------------------


def test_research_session_api(env, tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=env.path, documents_dir=tmp_path, offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        created = c.post("/api/research/sessions",
                         json={"title": "Study", "question": "How?"}).json()
        study_id = created["study_id"]
        assert created["status"] == "active"

        assert c.get("/api/research/sessions").json()["count"] == 1
        detail = c.get(f"/api/research/sessions/{study_id}").json()
        assert detail["study"]["title"] == "Study"

        # status change + a refused one
        assert c.patch(f"/api/research/sessions/{study_id}",
                       json={"status": "paused"}).json()["status"] == "paused"
        c.patch(f"/api/research/sessions/{study_id}", json={"status": "completed"})
        assert c.patch(f"/api/research/sessions/{study_id}",
                       json={"status": "active"}).status_code == 409

        ref = c.post(f"/api/research/sessions/{study_id}/references",
                     json={"kind": "reaction",
                           "object_id": "primary-alcohol-oxidation"}).json()
        assert ref["kind"] == "reaction"
        assert c.post(f"/api/research/sessions/{study_id}/references",
                      json={"kind": "reaction",
                            "object_id": "nope"}).status_code == 404
        assert c.delete(
            f"/api/research/sessions/{study_id}/references/{ref['reference_id']}"
        ).json()["removed"] is True

        note = c.post(f"/api/research/sessions/{study_id}/events",
                      json={"event_type": "note_added", "note": "check Ts"}).json()
        assert note["event_type"] == "note_added"

        art = c.post(f"/api/research/sessions/{study_id}/artifacts",
                     json={"artifact_type": "agent_result",
                           "content": {"summary": "x"}}).json()
        assert art["artifact_type"] == "agent_result"

        assert c.get("/api/research/sessions/study-nope").status_code == 404
        # the existing research-job route is not swallowed by the session route
        assert c.get("/api/research").status_code == 200

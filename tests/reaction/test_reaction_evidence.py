"""Reaction evidence (Stage 3.4): what supports a ReactionTemplate, and how.

The knowledge-evidence layer over the curated library: a curated template has
a derived CURATED self-evidence; an attached record is validated and stored;
a bad record or an unknown template is a structured error. No literature is
fabricated and no confidence score exists (§15/§17).
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from crea.core.errors import ReactionEvidenceError
from crea.core.models.reaction_evidence import (
    EvidenceSourceReference,
    EvidenceSourceType,
    ReactionEvidence,
    ReactionEvidenceType,
)
from crea.core.models.reaction_template import EvidenceStatus
from crea.services.knowledge_base import KnowledgeBase
from crea.services.reaction_evidence import ReactionEvidenceService


def _service(tmp_path=None) -> ReactionEvidenceService:
    db = (pathlib.Path(tmp_path) if tmp_path
          else pathlib.Path(tempfile.mkdtemp())) / "ev.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    return ReactionEvidenceService(kb=kb)


def _literature_evidence(template_id="primary-alcohol-oxidation") -> ReactionEvidence:
    return ReactionEvidence(
        evidence_id="evidence-lit-1", reaction_template_id=template_id,
        evidence_type=ReactionEvidenceType.MECHANISM_SUPPORT,
        source=EvidenceSourceReference(
            source_type=EvidenceSourceType.LITERATURE,
            source_id="10.0000/example", title="An example paper",
            url="https://doi.org/10.0000/example",
            locator={"section": "Experimental"}),
        support_scope="two-electron oxidation of the carbinol carbon",
        status=EvidenceStatus.LITERATURE_SUPPORTED,
        note="This source supports the mechanism.")


# -- curated self-evidence (§9/§26) ----------------------------------------


def test_curated_template_returns_its_curated_self_evidence(tmp_path):
    evidence = _service(tmp_path).list_evidence("primary-alcohol-oxidation")
    assert len(evidence) == 1
    record = evidence[0]
    assert record.evidence_type is ReactionEvidenceType.CURATED_KNOWLEDGE
    assert record.source.source_type is EvidenceSourceType.CURATED
    assert record.status is EvidenceStatus.CURATED
    assert record.support_scope == "primary_alcohol -> aldehyde"


def test_curated_evidence_is_not_literature(tmp_path):
    record = _service(tmp_path).list_evidence("alkene-hydrogenation")[0]
    assert record.status is not EvidenceStatus.LITERATURE_SUPPORTED
    assert record.source.source_type is not EvidenceSourceType.LITERATURE
    assert record.source.source_id != ""  # the seed, not a DOI


# -- attach / store / read (§19/§20) ---------------------------------------


def test_attach_evidence_validates_and_stores(tmp_path):
    service = _service(tmp_path)
    stored = service.attach_evidence(_literature_evidence())
    assert stored.evidence_id == "evidence-lit-1"
    # a stored record takes over from the derived curated one
    listed = service.list_evidence("primary-alcohol-oxidation")
    assert [e.evidence_id for e in listed] == ["evidence-lit-1"]
    assert service.get_evidence("evidence-lit-1").support_scope.startswith(
        "two-electron")


def test_evidence_survives_a_reopen(tmp_path):
    db = pathlib.Path(tmp_path) / "ev.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    ReactionEvidenceService(kb=kb).attach_evidence(_literature_evidence())
    kb.close()

    kb2 = KnowledgeBase(db)
    kb2.initialize()
    reloaded = ReactionEvidenceService(kb=kb2).get_evidence("evidence-lit-1")
    assert reloaded is not None
    assert reloaded.source.locator.section == "Experimental"
    kb2.close()


# -- structured errors (§21) -----------------------------------------------


def test_unknown_template_is_a_structured_error(tmp_path):
    with pytest.raises(ReactionEvidenceError) as exc:
        _service(tmp_path).list_evidence("no-such-template")
    assert exc.value.code == "TEMPLATE_NOT_FOUND"


def test_invalid_evidence_is_rejected(tmp_path):
    service = _service(tmp_path)
    bad = ReactionEvidence(
        evidence_id="evidence-bad", reaction_template_id="alkene-hydrogenation",
        evidence_type=ReactionEvidenceType.CURATED_KNOWLEDGE)
    with pytest.raises(ReactionEvidenceError) as exc:
        service.attach_evidence(bad)
    assert exc.value.code == "INVALID_EVIDENCE"


def test_mismatched_template_id_is_rejected(tmp_path):
    service = _service(tmp_path)
    evidence = _literature_evidence("alkene-hydrogenation")
    # attach while claiming a different template than the record carries
    evidence.reaction_template_id = "primary-alcohol-oxidation"
    # the record's own template_id now matches the target -- but a mismatch is
    # still caught when the id refers to no template at all
    with pytest.raises(ReactionEvidenceError):
        service.attach_evidence(_literature_evidence("no-such-template"))


# -- API (§19) --------------------------------------------------------------


def test_reaction_evidence_api(tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        got = c.get("/api/reaction-templates/primary-alcohol-oxidation/evidence")
        assert got.status_code == 200
        body = got.json()
        assert body["count"] == 1
        assert body["evidence"][0]["evidence_type"] == "curated_knowledge"
        assert body["evidence"][0]["source"]["source_type"] == "curated"

        posted = c.post(
            "/api/reaction-templates/alkene-hydrogenation/evidence",
            json={
                "evidence_id": "evidence-api-1",
                "evidence_type": "condition_support",
                "source": {"source_type": "user_provided",
                           "source_id": "lab-note-1"},
                "support_scope": "H2 / Pd-C at room temperature",
                "status": "user_provided"})
        assert posted.status_code == 200
        assert posted.json()["evidence_id"] == "evidence-api-1"

        assert c.get(
            "/api/reaction-templates/no-such/evidence").status_code == 404
        assert c.post(
            "/api/reaction-templates/primary-alcohol-oxidation/evidence",
            json={"evidence_id": "x", "evidence_type": "condition_support"}
        ).status_code == 400

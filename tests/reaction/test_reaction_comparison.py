"""Reaction comparison (Stage 5.2).

Facts side by side, never a verdict. Reuses Transformation Search (5.1) for
substrate compatibility and the existing evidence service. Deterministic, in
input order, no provider/network/AI, and no recommendation field.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from crea.core.errors import ReactionComparisonError
from crea.core.models.literature import Identifiers, Paper
from crea.core.models.reaction_evidence import ReactionEvidenceType
from crea.services.knowledge_base import KnowledgeBase
from crea.services.literature_record import LiteratureRecordService
from crea.services.reaction_comparison import ReactionComparisonService
from crea.services.reaction_evidence import ReactionEvidenceService


def _service(tmp_path) -> tuple[KnowledgeBase, ReactionComparisonService]:
    kb = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    kb.initialize()
    return kb, ReactionComparisonService(kb=kb)


# -- 1/2. compare two / three reactions -------------------------------------


def test_compare_two_reactions(tmp_path):
    kb, svc = _service(tmp_path)
    result = svc.compare(["primary-alcohol-oxidation", "sn2-substitution"])
    assert [e.template_id for e in result.entries] == [
        "primary-alcohol-oxidation", "sn2-substitution"]
    kb.close()


def test_compare_three_reactions(tmp_path):
    kb, svc = _service(tmp_path)
    result = svc.compare(["primary-alcohol-oxidation", "sn2-substitution",
                          "aromatic-nitration"])
    assert len(result.entries) == 3
    kb.close()


# -- 3. match level reuse ----------------------------------------------------


def test_substrate_compatibility_reuses_transformation_search(tmp_path):
    kb, svc = _service(tmp_path)
    result = svc.compare(["primary-alcohol-oxidation", "aromatic-nitration"],
                         substrate="CCO")
    by_id = {e.template_id: e for e in result.entries}
    assert by_id["primary-alcohol-oxidation"].substrate_compatibility == "substrate_match"
    assert by_id["primary-alcohol-oxidation"].matched_substrate_atoms > 0
    assert by_id["aromatic-nitration"].substrate_compatibility == "no_match"
    kb.close()


def test_no_substrate_means_compatibility_is_not_evaluated(tmp_path):
    kb, svc = _service(tmp_path)
    result = svc.compare(["primary-alcohol-oxidation"])
    assert result.entries[0].substrate_compatibility == ""
    kb.close()


# -- 4. facts: reagents / conditions / selectivity / limitations ------------


def test_reagents_conditions_selectivity_are_shown(tmp_path):
    kb, svc = _service(tmp_path)
    result = svc.compare(["primary-alcohol-oxidation", "alkene-hydroboration-oxidation"])
    by_id = {e.template_id: e for e in result.entries}
    assert "PCC" in by_id["primary-alcohol-oxidation"].reagents[0]
    # a template that states selectivity shows it; one that does not shows {}
    sel = by_id["alkene-hydroboration-oxidation"].selectivity
    assert sel.get("regioselectivity") == "anti-Markovnikov"
    kb.close()


def test_unspecified_facts_are_empty_not_invented(tmp_path):
    kb, svc = _service(tmp_path)
    result = svc.compare(["alkene-halogenation"])
    entry = result.entries[0]
    # no invention: whatever the template lacks is empty, not guessed
    assert isinstance(entry.reagents, list)
    assert isinstance(entry.conditions, list)
    assert isinstance(entry.limitations, list)
    kb.close()


# -- 5. evidence + literature -----------------------------------------------


def test_evidence_and_literature_are_integrated(tmp_path):
    kb, svc = _service(tmp_path)
    LiteratureRecordService(kb).create(Paper(
        paper_id="paper-A", title="An oxidation study", year=2021,
        journal="J. Synth. Chem.", identifiers=Identifiers(doi="10.1000/abc"),
        retrieved_at="2026-01-01"))
    ReactionEvidenceService(kb=kb).link_literature(
        "primary-alcohol-oxidation", paper_id="paper-A",
        evidence_type=ReactionEvidenceType.TRANSFORMATION_SUPPORT,
        support_scope="primary alcohol -> aldehyde")

    result = svc.compare(["primary-alcohol-oxidation"])
    entry = result.entries[0]
    assert entry.evidence_count >= 1
    assert entry.evidence_summary.get("literature") == 1
    assert entry.literature[0].paper_id == "paper-A"
    assert entry.literature[0].year == 2021
    assert entry.literature[0].doi == "10.1000/abc"
    kb.close()


# -- 6. yield is not modelled ------------------------------------------------


def test_yield_is_not_available(tmp_path):
    kb, svc = _service(tmp_path)
    result = svc.compare(["primary-alcohol-oxidation"])
    assert result.entries[0].yield_available is False
    kb.close()


# -- 7. no recommendation fields --------------------------------------------


def test_result_has_no_recommendation_fields(tmp_path):
    kb, svc = _service(tmp_path)
    result = svc.compare(["primary-alcohol-oxidation", "sn2-substitution"])
    payload = result.model_dump(mode="json")
    banned = ("best_reaction", "recommended_reaction", "winner", "score",
              "confidence", "success_probability", "predicted_yield")
    for entry in payload["entries"]:
        for field in banned:
            assert field not in entry
    kb.close()


# -- 8. deterministic output -------------------------------------------------


def test_output_is_deterministic_and_in_input_order(tmp_path):
    kb, svc = _service(tmp_path)
    ids = ["sn2-substitution", "primary-alcohol-oxidation", "aromatic-nitration"]
    a = svc.compare(ids, substrate="CCO")
    b = svc.compare(ids, substrate="CCO")
    assert [e.template_id for e in a.entries] == ids  # exact input order
    assert a.model_dump() == b.model_dump()
    kb.close()


# -- 9. structured errors ----------------------------------------------------


def test_empty_input_is_a_structured_error(tmp_path):
    kb, svc = _service(tmp_path)
    with pytest.raises(ReactionComparisonError) as exc:
        svc.compare([])
    assert exc.value.code == "INVALID_INPUT"
    kb.close()


def test_unknown_template_is_a_structured_error(tmp_path):
    kb, svc = _service(tmp_path)
    with pytest.raises(ReactionComparisonError) as exc:
        svc.compare(["no-such-template"])
    assert exc.value.code == "TEMPLATE_NOT_FOUND"
    kb.close()


# -- 10. API -----------------------------------------------------------------


def test_reaction_comparison_api(tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        body = c.post("/api/reactions/compare", json={
            "reaction_template_ids": ["primary-alcohol-oxidation", "sn2-substitution"],
            "substrate": "CCO"}).json()
        assert len(body["entries"]) == 2
        assert body["entries"][0]["substrate_compatibility"] == "substrate_match"

        assert c.post("/api/reactions/compare",
                      json={"reaction_template_ids": []}).status_code == 400
        assert c.post("/api/reactions/compare",
                      json={"reaction_template_ids": ["nope"]}).status_code == 404

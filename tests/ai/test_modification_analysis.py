"""Modification analysis (Stage 6.2).

The AI proposes, the context grounds. Deterministic throughout with a scripted
LLM: no API key, no network. Facts come only from the AI Context; hallucinated
reaction / evidence / literature ids are refused; malformed output and a dead
provider are structured errors; nothing is persisted.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from crea.core.errors import ModificationAnalysisError
from crea.core.models.modification import (
    GroundingStatus,
    ModificationAnalysisRequest,
)
from crea.llm.fake import TextScriptedLLMProvider
from crea.services.ai_context import AIContextService
from crea.services.knowledge_base import KnowledgeBase
from crea.services.modification_analysis import ModificationAnalysisService


class _DeadLLM:
    name = "dead"
    model = "dead-1"

    def complete(self, request):
        from crea.core.errors import ProviderUnavailable

        raise ProviderUnavailable("no provider configured")


@pytest.fixture()
def kb(tmp_path):
    store = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    store.initialize()
    return store


def _service(kb, response: str) -> ModificationAnalysisService:
    return ModificationAnalysisService(
        AIContextService(kb), TextScriptedLLMProvider([response]))


def _candidate(reaction_ids, **kwargs) -> dict:
    return {"description": kwargs.get("description", "a change"),
            "structural_change": {"from_feature": "alcohol",
                                  "to_feature": "alkyl_halide",
                                  "scope": "functional_group"},
            "reaction_template_ids": reaction_ids,
            "evidence_ids": kwargs.get("evidence_ids", []),
            "literature_ids": kwargs.get("literature_ids", []),
            "rationale": kwargs.get("rationale", "fits the request")}


# -- 1. basic analysis -------------------------------------------------------


def test_basic_modification_analysis(kb):
    response = json.dumps({"candidates": [_candidate(["alcohol-to-halide"])]})
    analysis = _service(kb, response).analyze(
        ModificationAnalysisRequest(molecule_smiles="CCO",
                                    query="convert the OH to Cl"))
    assert analysis.analysis_id.startswith("mmod-")
    assert analysis.context_version == "crea-ai-context-v1"
    assert len(analysis.candidates) == 1
    assert analysis.candidates[0].grounding_status is GroundingStatus.GROUNDED


# -- 2. the model receives the AI Context, not raw DB objects ---------------


def test_model_receives_the_ai_context(kb):
    llm = TextScriptedLLMProvider([json.dumps({"candidates": []})])
    service = ModificationAnalysisService(AIContextService(kb), llm)
    service.analyze(ModificationAnalysisRequest(molecule_smiles="CCO", query="x"))
    sent = llm.calls[0].messages[0]["content"]
    assert "crea_context" in sent
    assert "requested_modification" in sent
    assert "CCO" in sent


# -- 3. grounded reaction ----------------------------------------------------


def test_grounded_candidate_references_a_real_template(kb):
    response = json.dumps({"candidates": [_candidate(
        ["primary-alcohol-oxidation"])]})
    analysis = _service(kb, response).analyze(
        ModificationAnalysisRequest(molecule_smiles="CCO", query="oxidise"))
    candidate = analysis.candidates[0]
    assert candidate.reaction_template_ids == ["primary-alcohol-oxidation"]
    assert candidate.grounding_status is GroundingStatus.GROUNDED


# -- 4. hallucinated reaction id is refused ---------------------------------


def test_hallucinated_reaction_id_is_not_a_candidate(kb):
    response = json.dumps({"candidates": [
        _candidate(["RXN-999-fake"], description="invented")]})
    analysis = _service(kb, response).analyze(
        ModificationAnalysisRequest(molecule_smiles="CCO", query="x"))
    assert analysis.candidates == []
    assert analysis.unsupported == ["invented"]
    assert any("RXN-999-fake" in w for w in analysis.warnings)


def test_partially_grounded_keeps_only_resolving_ids(kb):
    response = json.dumps({"candidates": [_candidate(
        ["alcohol-to-halide", "RXN-999-fake"])]})
    analysis = _service(kb, response).analyze(
        ModificationAnalysisRequest(molecule_smiles="CCO", query="x"))
    candidate = analysis.candidates[0]
    assert candidate.reaction_template_ids == ["alcohol-to-halide"]
    assert candidate.grounding_status is GroundingStatus.PARTIALLY_GROUNDED


# -- 5. hallucinated literature is refused ----------------------------------


def test_hallucinated_literature_is_refused(kb):
    response = json.dumps({"candidates": [_candidate(
        ["alcohol-to-halide"], literature_ids=["10.9999/fake-paper"])]})
    analysis = _service(kb, response).analyze(
        ModificationAnalysisRequest(molecule_smiles="CCO", query="x"))
    assert analysis.candidates[0].literature_ids == []
    assert any("10.9999/fake-paper" in w for w in analysis.warnings)


# -- 6. never creates a reaction ---------------------------------------------


def test_candidates_only_reference_context_templates(kb):
    response = json.dumps({"candidates": [_candidate(["alcohol-to-halide"])]})
    analysis = _service(kb, response).analyze(
        ModificationAnalysisRequest(molecule_smiles="CCO", query="x"))
    # the id exists in the shipped library and is not minted by the analysis
    from crea.services.reaction_library import get_template

    for candidate in analysis.candidates:
        for tid in candidate.reaction_template_ids:
            assert get_template(tid) is not None


# -- 7. malformed output -----------------------------------------------------


def test_malformed_output_is_a_structured_error(kb):
    service = ModificationAnalysisService(
        AIContextService(kb),
        TextScriptedLLMProvider(["not json", "still not json"]))
    with pytest.raises(ModificationAnalysisError) as exc:
        service.analyze(ModificationAnalysisRequest(molecule_smiles="CCO", query="x"))
    assert exc.value.code == "AI_OUTPUT_INVALID"


def test_fenced_json_is_accepted(kb):
    fenced = "```json\n" + json.dumps({"candidates": [_candidate(
        ["alcohol-to-halide"])]}) + "\n```"
    analysis = _service(kb, fenced).analyze(
        ModificationAnalysisRequest(molecule_smiles="CCO", query="x"))
    assert analysis.candidates


# -- 8. provider unavailable -------------------------------------------------


def test_provider_unavailable_is_a_structured_error(kb):
    service = ModificationAnalysisService(AIContextService(kb), _DeadLLM())
    with pytest.raises(ModificationAnalysisError) as exc:
        service.analyze(ModificationAnalysisRequest(molecule_smiles="CCO", query="x"))
    assert exc.value.code == "AI_PROVIDER_UNAVAILABLE"


# -- 9. model metadata + no forbidden fields ---------------------------------


def test_model_metadata_and_no_prediction_fields(kb):
    response = json.dumps({"candidates": [_candidate(["alcohol-to-halide"])]})
    analysis = _service(kb, response).analyze(
        ModificationAnalysisRequest(molecule_smiles="CCO", query="x"))
    meta = analysis.model_metadata
    assert meta.provider == "text-scripted"
    assert meta.model == "text-scripted-1"
    assert meta.request_id
    payload = analysis.model_dump(mode="json")
    for banned in ("yield", "predicted_yield", "confidence",
                   "success_probability", "new_reaction_template"):
        assert banned not in payload
    assert "answer" not in payload


# -- 10. API -----------------------------------------------------------------


def test_modification_analysis_api_offline(kb, tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=kb.path, documents_dir=tmp_path, offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        # no AI provider offline -> structured error, deterministic tools intact
        r = c.post("/api/ai/modification-analysis",
                   json={"molecule_smiles": "CCO", "query": "x"})
        assert r.status_code == 400
        assert r.json()["error"]["detail"] == "AI_PROVIDER_UNAVAILABLE"
        # a request without a query is rejected up front
        assert c.post("/api/ai/modification-analysis",
                      json={"molecule_smiles": "CCO"}).status_code == 400
        # and the deterministic tools are unaffected
        assert c.get("/api/health").json()["status"] == "ok"

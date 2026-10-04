"""Route reasoning (Stage 6.3).

One bounded AI call over deterministic routes. Deterministic throughout with a
scripted LLM: no key, no network. Every observation is anchored to a real
route; hallucinated routes/steps are refused; a partial route is never upgraded;
the AI never invents a route.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from crea.core.errors import RouteReasoningError
from crea.core.models.modification import GroundingStatus
from crea.core.models.reaction_template import (
    ReactionClassFamily,
    ReactionTemplate,
    Transformation,
)
from crea.core.models.route_reasoning import RouteReasoningRequest
from crea.llm.fake import TextScriptedLLMProvider
from crea.services.ai_context import AIContextService
from crea.services.knowledge_base import KnowledgeBase
from crea.services.route_explorer import RouteExplorerService
from crea.services.route_reasoning import RouteReasoningService
from crea.services.transformation_search import TransformationSearchService


def _template(template_id, name, substrate, transform):
    return ReactionTemplate(
        template_id=template_id, name=name,
        class_family=ReactionClassFamily.OXIDATION, reaction_class="test",
        transformation=Transformation(input_features=["a"], output_features=["b"],
                                      note=f"{template_id} change"),
        substrate_patterns=[substrate], reaction_smarts=transform)


def _stack(tmp_path, *, extra=None):
    kb = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    kb.initialize()
    templates = [
        _template("test-ox", "Test Ox", "[C;H2,H3][OX2H]",
                  "[C:1][OX2H:2]>>[C:1]=[O:2]"),
        _template("test-acid", "Test Acid", "[CX3H1](=O)[#6]",
                  "[CX3H1:1]=[O:2]>>[CX3:1](=[O:2])[OX2H]"),
    ]
    if extra:
        templates.extend(extra)
    explorer = RouteExplorerService(
        search_service=TransformationSearchService(templates=templates))
    return kb, explorer


def _reason(kb, explorer, response, **kwargs):
    llm = TextScriptedLLMProvider([response])
    service = RouteReasoningService(AIContextService(kb), explorer, llm)
    kwargs.setdefault("start_molecule", "CCO")
    kwargs.setdefault("target_molecule", "CC(=O)O")
    kwargs.setdefault("query", "compare the routes")
    return service.reason(RouteReasoningRequest(**kwargs))


def _complete_route_id(explorer, start="CCO", target="CC(=O)O") -> str:
    explored = explorer.explore(start, target)
    return next(p.route_id for p in explored.paths if p.status.value == "complete")


# -- 1. complete route reasoning --------------------------------------------


def test_complete_route_reasoning(tmp_path):
    kb, explorer = _stack(tmp_path)
    rid = _complete_route_id(explorer)
    response = json.dumps({"observations": [
        {"route_id": rid, "summary": "a two-step oxidation",
         "step_notes": [{"step_index": 1, "reaction_template_id": "test-ox",
                         "note": "first oxidation"}],
         "gaps": ["no conditions recorded"]}],
        "comparisons": [], "gaps": ["no conditions"], "notes": []})
    result = _reason(kb, explorer, response)
    assert result.observations[0].route_id == rid
    assert result.observations[0].grounding_status is GroundingStatus.GROUNDED
    assert result.observations[0].status == "complete"
    kb.close()


# -- 2. multiple routes ------------------------------------------------------


def test_multiple_routes_are_all_reasoned(tmp_path):
    kb, explorer = _stack(tmp_path, extra=[
        _template("test-ox2", "Test Ox 2", "[C;H2,H3][OX2H]",
                  "[C:1][OX2H:2]>>[C:1]=[O:2]")])
    explored = explorer.explore("CCO", "CC=O")
    complete_ids = [p.route_id for p in explored.paths if p.status.value == "complete"]
    assert len(complete_ids) >= 1
    response = json.dumps({"observations": [
        {"route_id": rid, "summary": "x"} for rid in complete_ids]})
    result = _reason(kb, explorer, response, target_molecule="CC=O")
    assert {o.route_id for o in result.observations} == set(complete_ids)
    kb.close()


# -- 3/4. partial and blocked are not upgraded -------------------------------


def test_partial_route_status_is_not_upgraded(tmp_path):
    kb, explorer = _stack(tmp_path)
    # a target nothing reaches -> partial/blocked paths only
    explored = explorer.explore("CCO", "c1ccccc1")
    first = explored.paths[0]
    response = json.dumps({"observations": [
        {"route_id": first.route_id, "summary": "this route completes"}]})
    result = _reason(kb, explorer, response, target_molecule="c1ccccc1")
    observation = result.observations[0]
    # the status comes from the explorer, never the model's claim
    assert observation.status == first.status.value
    assert observation.status != "complete"
    kb.close()


# -- 5. no routes ------------------------------------------------------------


def test_no_routes_does_not_invent_one(tmp_path):
    kb, explorer = _stack(tmp_path)
    response = json.dumps({"observations": [], "comparisons": [],
                           "gaps": ["no complete route"],
                           "notes": ["no deterministic complete route found"]})
    result = _reason(kb, explorer, response, target_molecule="c1ccccc1")
    assert result.observations == []
    assert result.route_summaries  # the partial/blocked paths are still reported
    assert all(s.status != "complete" for s in result.route_summaries)
    kb.close()


# -- 6. hallucinated route id ------------------------------------------------


def test_hallucinated_route_id_is_refused(tmp_path):
    kb, explorer = _stack(tmp_path)
    response = json.dumps({"observations": [
        {"route_id": "route-invented", "summary": "made up"}]})
    result = _reason(kb, explorer, response)
    assert result.observations == []
    assert result.unsupported == ["route-invented"]
    assert any("route-invented" in w for w in result.warnings)
    kb.close()


# -- 7. hallucinated step ----------------------------------------------------


def test_hallucinated_step_is_stripped(tmp_path):
    kb, explorer = _stack(tmp_path)
    rid = _complete_route_id(explorer)
    response = json.dumps({"observations": [
        {"route_id": rid, "summary": "x",
         "step_notes": [
             {"step_index": 1, "reaction_template_id": "test-ox", "note": "ok"},
             {"step_index": 1, "reaction_template_id": "test-INVENTED",
              "note": "bogus"}]}]})
    result = _reason(kb, explorer, response)
    observation = result.observations[0]
    assert [n.reaction_template_id for n in observation.step_notes] == ["test-ox"]
    assert observation.grounding_status is GroundingStatus.PARTIALLY_GROUNDED
    assert any("test-INVENTED" in w for w in result.warnings)
    kb.close()


# -- 8/9. failure handling ---------------------------------------------------


def test_invalid_output_is_a_structured_error(tmp_path):
    kb, explorer = _stack(tmp_path)
    llm = TextScriptedLLMProvider(["not json", "still not json"])
    service = RouteReasoningService(AIContextService(kb), explorer, llm)
    with pytest.raises(RouteReasoningError) as exc:
        service.reason(RouteReasoningRequest(start_molecule="CCO",
                                             target_molecule="CC(=O)O", query="x"))
    assert exc.value.code == "AI_OUTPUT_INVALID"
    kb.close()


def test_provider_unavailable_is_a_structured_error(tmp_path):
    kb, explorer = _stack(tmp_path)

    class _Dead:
        name = "dead"
        model = "dead-1"

        def complete(self, request):
            from crea.core.errors import ProviderUnavailable

            raise ProviderUnavailable("no provider")

    service = RouteReasoningService(AIContextService(kb), explorer, _Dead())
    with pytest.raises(RouteReasoningError) as exc:
        service.reason(RouteReasoningRequest(start_molecule="CCO",
                                             target_molecule="CC(=O)O", query="x"))
    assert exc.value.code == "AI_PROVIDER_UNAVAILABLE"
    kb.close()


# -- 10. metadata + no forbidden fields --------------------------------------


def test_metadata_and_no_ranking_fields(tmp_path):
    kb, explorer = _stack(tmp_path)
    rid = _complete_route_id(explorer)
    response = json.dumps({"observations": [{"route_id": rid, "summary": "x"}]})
    result = _reason(kb, explorer, response)
    assert result.model_metadata.provider == "text-scripted"
    assert result.model_metadata.request_id
    payload = result.model_dump(mode="json")
    for banned in ("best_route", "recommended_route", "yield", "confidence",
                   "success_probability", "answer"):
        assert banned not in payload
    kb.close()


# -- 11. API -----------------------------------------------------------------


def test_route_reasoning_api_offline(tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        r = c.post("/api/ai/route-reasoning",
                   json={"start_molecule": "CCO", "target_molecule": "CC=O",
                         "query": "compare"})
        assert r.status_code == 400
        assert r.json()["error"]["detail"] == "AI_PROVIDER_UNAVAILABLE"
        assert c.post("/api/ai/route-reasoning",
                      json={"start_molecule": "CCO"}).status_code == 400
        assert c.get("/api/health").json()["status"] == "ok"

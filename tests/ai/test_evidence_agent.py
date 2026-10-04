"""Evidence-aware agent (Stage 6.4).

A bounded, read-only, tool-using run whose answer is grounded. Deterministic
throughout with a scripted LLM: no key, no network. Facts come from tools; a
hallucinated id is not a fact; hypotheses are marked; every tool is read-only;
the loop is bounded and stops cleanly.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from crea.agent.evidence_agent import EvidenceAgent
from crea.agent.evidence_tools import build_evidence_tool_registry
from crea.core.models.agent_run import AgentStatus, ClaimType, StopReason
from crea.llm.fake import TextScriptedLLMProvider
from crea.services.knowledge_base import KnowledgeBase
from crea.services.reaction_comparison import ReactionComparisonService
from crea.services.reaction_evidence import ReactionEvidenceService
from crea.services.route_explorer import RouteExplorerService
from crea.services.transformation_search import TransformationSearchService


@pytest.fixture()
def registry(tmp_path):
    kb = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    kb.initialize()
    evidence = ReactionEvidenceService(kb=kb)
    return build_evidence_tool_registry(
        transformation_search=TransformationSearchService(evidence_service=evidence),
        comparison=ReactionComparisonService(kb=kb),
        evidence=evidence, route=RouteExplorerService(kb=kb))


def _agent(registry, script, **kwargs):
    return EvidenceAgent(registry, TextScriptedLLMProvider(script), **kwargs)


def _tool(name, **arguments):
    return json.dumps({"action": "tool_call", "tool": name, "arguments": arguments})


def _final(answer):
    return json.dumps({"action": "final", "answer": answer})


# -- 1. single tool call then a grounded answer ------------------------------


def test_single_tool_call_then_answer(registry):
    script = [
        _tool("search_transformations", substrate="CCO"),
        _final({"summary": "several templates match",
                "claims": [{"text": "Primary Alcohol Oxidation matches CCO",
                            "claim_type": "fact",
                            "reaction_ids": ["primary-alcohol-oxidation"]}]}),
    ]
    result = _agent(registry, script).run("这个分子有哪些已知修饰方向？")
    assert result.status is AgentStatus.COMPLETED
    assert result.stop_reason is StopReason.FINAL_ANSWER
    assert [t.tool_name for t in result.tool_calls] == ["search_transformations"]
    assert result.tool_calls[0].status == "ok"
    assert result.answer.claims[0].grounded is True
    assert "primary-alcohol-oxidation" in result.gathered["reaction_template_ids"]


# -- 2. hallucinated fact id is not a fact ----------------------------------


def test_hallucinated_fact_id_is_not_grounded(registry):
    script = [_final({"summary": "x", "claims": [
        {"text": "invented", "claim_type": "fact",
         "reaction_ids": ["RXN-INVENTED"]}]})]
    result = _agent(registry, script).run("q")
    claim = result.answer.claims[0]
    assert claim.reaction_ids == []      # the id is stripped
    assert claim.grounded is False
    assert result.status is AgentStatus.PARTIAL


# -- 3. hypotheses are marked, never facts ----------------------------------


def test_hypothesis_is_flagged_not_verified(registry):
    script = [_final({"summary": "x", "claims": [
        {"text": "this may be worth investigating",
         "claim_type": "hypothesis"}]})]
    result = _agent(registry, script).run("q")
    claim = result.answer.claims[0]
    assert claim.claim_type is ClaimType.HYPOTHESIS
    assert claim.not_experimentally_verified is True


# -- 4. tool input is schema-validated --------------------------------------


def test_invalid_tool_arguments_are_rejected_not_run(registry):
    # search_transformations requires a substrate
    script = [_tool("search_transformations", wrong="x"), _final({"summary": "no"})]
    result = _agent(registry, script).run("q")
    assert result.tool_calls[0].status == "rejected"
    assert result.tool_calls[0].output_ids == []


# -- 5. bounds ---------------------------------------------------------------


def test_max_tool_calls_stops_the_loop(registry):
    script = [_tool("inspect_molecule", smiles=smi) for smi in
              ("CCO", "CCN", "CCC", "CCCl")]
    result = _agent(registry, script, max_tool_calls=2).run("q")
    assert result.stop_reason is StopReason.MAX_TOOL_CALLS
    assert result.status is AgentStatus.LIMIT_REACHED
    assert len(result.tool_calls) == 2


def test_max_iterations_stops_the_loop(registry):
    script = [_tool("inspect_molecule", smiles=f"C{'C' * i}O") for i in range(10)]
    result = _agent(registry, script, max_iterations=3).run("q")
    assert result.stop_reason is StopReason.MAX_ITERATIONS
    assert result.iterations == 3


def test_repeated_tool_call_stops(registry):
    same = _tool("inspect_molecule", smiles="CCO")
    result = _agent(registry, [same, same]).run("q")
    assert result.stop_reason is StopReason.REPEATED_TOOL_CALL


# -- 6. provider failure -----------------------------------------------------


def test_provider_failure_is_structured(registry):
    class _Dead:
        name = "dead"
        model = "dead-1"

        def complete(self, request):
            raise RuntimeError("boom")

    result = EvidenceAgent(registry, _Dead()).run("q")
    assert result.status is AgentStatus.FAILED
    assert result.stop_reason is StopReason.PROVIDER_ERROR
    assert any("provider failed" in w for w in result.warnings)


# -- 7. read-only: no write tool exists -------------------------------------


def test_every_registered_tool_is_read_only(registry):
    for name in registry.names():
        spec = registry.get(name)
        assert spec.permission == "read_only", name
        assert spec.mutates_evidence is False, name
    # and no tool name suggests a write
    for banned in ("create", "update", "delete", "save", "write", "insert",
                   "sql", "shell", "python", "http", "fetch", "download"):
        assert not any(banned in n for n in registry.names()), banned


# -- 8. literature search is discovery, not evidence ------------------------


def test_search_literature_is_labelled_discovery(registry):
    result = registry.execute("search_literature", {"query": "nitration"})
    # no literature service wired here -> honest unavailability, not empty
    assert result.data["status"] == "unavailable"


# -- 9. bounded tool output --------------------------------------------------


def test_tool_output_is_bounded(registry):
    result = registry.execute("search_transformations", {"substrate": "CCO"})
    data = result.data
    assert data["status"] == "ok"
    assert isinstance(data["candidates"], list)


# -- 10. no forbidden fields -------------------------------------------------


def test_answer_has_no_prediction_fields(registry):
    script = [_final({"summary": "x", "claims": []})]
    result = _agent(registry, script).run("q")
    payload = result.model_dump(mode="json")
    for banned in ("confidence", "success_probability", "yield",
                   "best_route", "recommended"):
        assert banned not in payload
        assert banned not in payload["answer"]


# -- 11. API -----------------------------------------------------------------


def test_agent_api_offline(tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        r = c.post("/api/ai/agent", json={"question": "这个分子有什么已知方向？"})
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "AGENT_PROVIDER_UNAVAILABLE"
        assert c.post("/api/ai/agent", json={}).status_code == 400
        assert c.get("/api/health").json()["status"] == "ok"

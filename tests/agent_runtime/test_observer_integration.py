"""M1.2 -- the observer wired into the unified surface and the old agents.

The unified registry from M1.1 is the subject: real canonical tools, real
aliases, real policy rejections. No network (literature is blocked by policy
before it runs), no database, and no model except the scripted fake.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from crea.agent.chemistry_agent import ChemistryAgent
from crea.agent.evidence_agent import EvidenceAgent
from crea.agent.loop import ResearchAgent
from crea.agent.observer import ObservationKind, observe, observe_from_registry
from crea.agent.tool_surface import build_agent_tools
from crea.agent.tools import ToolRegistry, ToolSpec
from crea.core.models.research import AgentAction, ToolResult, ToolStatus
from crea.llm.fake import FakeLLMProvider, TextScriptedLLMProvider
from crea.services.reaction_builder import ReactionRegistry


class _Args(BaseModel):
    value: str = Field(default="x")


def _tool(name: str, **arguments) -> str:
    return json.dumps({"action": "tool_call", "tool": name,
                       "arguments": arguments})


def _final(answer: dict) -> str:
    return json.dumps({"action": "final", "answer": answer})


def _thread(provider) -> str:
    """The content of the last request's thread, unescaped."""
    return "\n".join(str(message.get("content", ""))
                     for message in provider.calls[-1].messages)


# ---------------------------------------------------------------------------
# The unified surface
# ---------------------------------------------------------------------------


def test_a_canonical_tool_reads_as_success_and_keeps_its_payload():
    registry = build_agent_tools()
    result = registry.execute("analyze_molecule", {"smiles": "CCO"})
    observation = observe_from_registry(registry, "analyze_molecule", result)

    assert observation.kind is ObservationKind.SUCCESS
    assert observation.raw_status == "ok"
    assert observation.canonical_tool == "analyze_molecule"
    assert observation.data["molecular_formula"] == "C2H6O"
    assert observation.data is result.data or observation.data == result.data


def test_an_alias_keeps_the_called_name_and_the_canonical_one():
    registry = build_agent_tools()
    result = registry.execute("analyze_structure", {"smiles": "CCO"})
    observation = observe_from_registry(registry, "analyze_structure", result)

    assert observation.kind is ObservationKind.SUCCESS
    assert observation.tool == "analyze_structure"
    assert observation.canonical_tool == "analyze_molecule"
    assert observation.raw.tool == "analyze_structure"
    assert observation.data["molecular_formula"] == "C2H6O"


def test_an_approval_rejection_becomes_needs_input_with_the_policy_intact():
    registry = build_agent_tools()
    result = registry.execute("resolve_chemical_identity",
                              {"kind": "SMILES", "value": "CCO"})
    observation = observe_from_registry(registry, "resolve_chemical_identity",
                                        result)

    assert result.status is ToolStatus.REJECTED
    assert observation.kind is ObservationKind.NEEDS_INPUT
    assert observation.error_code == "APPROVAL_REQUIRED"
    assert observation.missing_requirements == ["approval"]
    assert observation.needs_user_input is True
    assert "identity_resolved" not in registry.facts
    # the declared metadata survives the reading
    assert observation.retryable is False


def test_a_missing_precondition_names_the_fact():
    registry = build_agent_tools()
    result = registry.execute("retrosynthesize", {"target_smiles": "CCO"})
    observation = observe_from_registry(registry, "retrosynthesize", result)

    assert observation.kind is ObservationKind.NEEDS_INPUT
    assert observation.error_code == "PRECONDITION_NOT_MET"
    assert observation.missing_requirements == ["identity_resolved"]


def test_an_absent_database_is_unavailable_not_no_result():
    registry = build_agent_tools()
    result = registry.execute("get_knowledge_gaps", {})
    observation = observe_from_registry(registry, "get_knowledge_gaps", result)

    assert observation.kind is ObservationKind.UNAVAILABLE
    assert observation.error_code == "no_knowledge_base"
    assert observation.data["status"] == "no_knowledge_base"


def test_an_empty_local_search_is_no_result_and_keeps_its_count():
    registry = build_agent_tools(reactions=ReactionRegistry())
    result = registry.execute("search_reactions",
                              {"reactant_entity_id": "CHEM-NONE"})
    observation = observe_from_registry(registry, "search_reactions", result)

    assert observation.kind is ObservationKind.NO_RESULT
    assert observation.data["count"] == 0
    assert observation.data["reactions"] == []


def test_a_tool_exception_is_error_with_a_stable_code():
    def boom(**_: object):
        raise RuntimeError("extractor exploded")

    registry = ToolRegistry(enforce_policy=True)
    registry.register(ToolSpec(name="boom", description="boom",
                               arguments_model=_Args, run=boom))
    result = registry.execute("boom", {})
    observation = observe_from_registry(registry, "boom", result)

    assert observation.kind is ObservationKind.ERROR
    assert observation.error_type == "RuntimeError"
    assert "extractor exploded" in (observation.error or "")


def test_the_unified_metadata_is_not_lost_in_the_observation():
    registry = build_agent_tools()
    for name in ("analyze_molecule", "search_reactions", "retrosynthesize",
                 "resolve_chemical_identity"):
        result = registry.execute(name, _minimal_arguments(name))
        observation = observe_from_registry(registry, name, result)
        assert observation.raw_status == result.status.value
        assert observation.raw is result or observation.raw == result
        assert observation.raw.data == result.data
        assert observation.evidence_ids == result.evidence_ids
        assert observation.provenance == result.provenance


def _minimal_arguments(name: str) -> dict:
    return {
        "analyze_molecule": {"smiles": "CCO"},
        "search_reactions": {"reactant_entity_id": "CHEM-NONE"},
        "retrosynthesize": {"target_smiles": "CCO"},
        "resolve_chemical_identity": {"kind": "SMILES", "value": "CCO"},
    }[name]


# ---------------------------------------------------------------------------
# ResearchAgent: a rejected call is no longer silently dropped
# ---------------------------------------------------------------------------


def test_research_agent_records_and_reports_a_rejected_call():
    llm = FakeLLMProvider([
        AgentAction(action="tool_call", tool="resolve_chemical_identity",
                    arguments={"kind": "SMILES", "value": "CCO"}),
        AgentAction(action="final", summary="needs approval"),
    ])
    agent = ResearchAgent(llm, build_agent_tools())
    task = agent.run("q")

    assert task.steps[0].result_status is ToolStatus.REJECTED
    assert any("补充信息" in item and "approval" in item
               for item in task.unresolved_items)
    # the model was told why, in the next turn's thread
    thread = _thread(llm)
    assert '"kind": "needs_input"' in thread
    assert "APPROVAL_REQUIRED" in thread
    # and the observer added no model call of its own
    assert len(llm.calls) == 2


def test_research_agent_surfaces_a_precondition_as_missing_facts():
    llm = FakeLLMProvider([
        AgentAction(action="tool_call", tool="retrosynthesize",
                    arguments={"target_smiles": "CCO"}),
        AgentAction(action="final", summary="no route"),
    ])
    agent = ResearchAgent(llm, build_agent_tools())
    task = agent.run("q")

    assert any("identity_resolved" in item for item in task.unresolved_items)
    assert '"kind": "needs_input"' in _thread(llm)


def test_research_agent_reports_a_tool_exception_as_an_error():
    def boom(**_: object):
        raise RuntimeError("backend gone")

    registry = ToolRegistry(enforce_policy=True)
    registry.register(ToolSpec(name="boom", description="boom",
                               arguments_model=_Args, run=boom))
    llm = FakeLLMProvider([
        AgentAction(action="tool_call", tool="boom", arguments={}),
        AgentAction(action="final", summary="failed"),
    ])
    task = ResearchAgent(llm, registry).run("q")

    assert any("工具 boom 未成功" in item for item in task.unresolved_items)
    assert '"kind": "error"' in _thread(llm)


def test_research_agent_records_an_empty_search_as_a_miss_not_a_failure():
    registry = build_agent_tools(reactions=ReactionRegistry())
    llm = FakeLLMProvider([
        AgentAction(action="tool_call", tool="search_reactions",
                    arguments={"reactant_entity_id": "CHEM-NONE"}),
        AgentAction(action="final", summary="nothing known"),
    ])
    task = ResearchAgent(llm, registry).run("q")

    assert any("未找到符合条件的结果" in item for item in task.unresolved_items)
    assert not any("未成功" in item for item in task.unresolved_items)
    assert '"kind": "no_result"' in _thread(llm)


# ---------------------------------------------------------------------------
# ChemistryAgent: needs-input and no-result get distinct diagnostics
# ---------------------------------------------------------------------------


def test_chemistry_agent_differs_between_needs_input_and_an_empty_miss():
    rejected = ToolResult(
        tool="resolve_chemical", status=ToolStatus.REJECTED,
        error_type="APPROVAL_REQUIRED", error="requires user approval")
    diagnostics: list[str] = []
    ChemistryAgent._absorb(rejected, [], [], diagnostics,
                           observe(rejected))
    joined = " ".join(diagnostics)
    assert "needs input" in joined
    assert "approval" in joined
    assert "no matching results" not in joined

    miss = ToolResult(tool="search_reactions", status=ToolStatus.OK,
                      data={"status": "ok", "count": 0, "reactions": []},
                      diagnostics=["no known reaction matches"])
    diagnostics = []
    ChemistryAgent._absorb(miss, [], [], diagnostics, observe(miss))
    joined = " ".join(diagnostics)
    assert "no matching results" in joined
    assert "unavailable" not in joined
    assert "no known reaction matches" in joined


# ---------------------------------------------------------------------------
# EvidenceAgent: the observation reaches the model's thread
# ---------------------------------------------------------------------------


def test_evidence_agent_tells_the_model_that_a_tool_was_unavailable():
    def unavailable(**_: object) -> ToolResult:
        return ToolResult(tool="search_literature",
                          status=ToolStatus.UNAVAILABLE,
                          error="no literature service is configured")

    registry = ToolRegistry()
    registry.register(ToolSpec(name="search_literature",
                               description="search",
                               arguments_model=_Args, run=unavailable))
    llm = TextScriptedLLMProvider([
        _tool("search_literature", value="x"),
        _final({"summary": "could not search", "claims": []}),
    ])
    result = EvidenceAgent(registry, llm).run("q")

    assert result.tool_calls[0].status == "unavailable"
    thread = _thread(llm)
    assert '"kind": "unavailable"' in thread
    assert "no literature service is configured" in thread
    assert '"raw_status": "unavailable"' in thread

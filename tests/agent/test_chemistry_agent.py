"""The Chemistry Agent (Stage E): orchestration over the tool registry.

Deterministic throughout: the LLM is a scripted fake (no key, no network),
while the chemical tools -- resolve_chemical, analyze_structure,
compare_structures, search_reactions -- are the real Stage C/D
implementations wired to a real SemanticResolver and a real ReactionRegistry.
The ten behaviours in §16, plus the end-to-end smoke test in §17.
"""

from __future__ import annotations

import json

from crea.agent.chemistry_agent import ChemistryAgent
from crea.core.models.research import AgentAction, ToolResult, ToolStatus
from crea.llm.base import LLMRequest, LLMResponse
from crea.llm.fake import ScriptExhausted
from crea.tools.registry import default_tool_registry

from tests.fakes.compound import (
    ASCORBIC_ACID,
    PHENYLACETYLENE,
    FakeCompoundProvider,
)


class ScriptedLLM:
    """Replays a script of turns. An entry is either a static `AgentAction`
    or a callable `(LLMRequest) -> AgentAction`, so a later step can be built
    from what an earlier tool actually returned (§9: the agent decides the
    next step from the result)."""

    name = "scripted"
    model = "scripted-1"

    def __init__(self, script, model="scripted-1"):
        self._script = list(script)
        self.model = model
        self.calls: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if not self._script:
            raise ScriptExhausted("the scripted LLM ran out of planned turns")
        entry = self._script.pop(0)
        action = entry(request) if callable(entry) else entry
        return LLMResponse(
            text=json.dumps(action.model_dump(mode="json"), ensure_ascii=False),
            provider=self.name, model=self.model, metadata={"scripted": True})


# -- the stack ---------------------------------------------------------------


def _build_spine(by_name=None):
    from crea.providers.name_lookup import CompoundNameLookup
    from crea.semantic.naming import NameResolver
    from crea.semantic.resolver import SemanticResolver
    from crea.services.entity_registry import ChemicalEntityRegistry
    from crea.services.identity import IdentityResolutionService

    provider = FakeCompoundProvider(
        by_name=by_name or {"phenylacetylene": [PHENYLACETYLENE]})
    entities = ChemicalEntityRegistry()
    name_resolver = NameResolver([CompoundNameLookup(provider)])
    resolver = SemanticResolver(
        identity_service=IdentityResolutionService(),
        name_resolver=name_resolver, entity_registry=entities)
    return resolver, name_resolver, entities, provider


def _tools(by_name=None, reactions=None):
    resolver, name_resolver, _entities, provider = _build_spine(by_name)
    return default_tool_registry(
        semantic_resolver=resolver, name_resolver=name_resolver,
        compound_provider=provider, reactions=reactions)


def _reaction(reaction_id, product, *, evidence_refs=()):
    from crea.core.models.reaction import ComponentRole
    from crea.core.models.reaction_knowledge import (
        Reaction,
        ReactionComponent,
        ReactionStatus,
    )

    return Reaction(
        reaction_id=reaction_id, reaction_key=f"key-{reaction_id}",
        paper_id="paper-a", status=ReactionStatus.EVIDENCE_BACKED,
        reactants=[ReactionComponent(role=ComponentRole.REACTANT, name="A",
                                     entity_id="CHEM-OTHER-A")],
        products=[ReactionComponent(role=ComponentRole.PRODUCT,
                                    name="phenylacetylene", entity_id=product)],
        evidence_refs=list(evidence_refs))


def _stack_with_reaction(evidence_refs=("EV-1",), reaction_id="RXN-1"):
    """One shared spine + a reaction producing phenylacetylene's entity.
    Returns (tools, entity_id) with the id minted by the SAME resolver the
    tools use, so the agent's own resolve step returns the same id."""
    from crea.semantic.resolver import ChemicalInput
    from crea.services.reaction_builder import ReactionRegistry

    resolver, name_resolver, entities, provider = _build_spine()
    entity_id = resolver.resolve(
        ChemicalInput.of("NAME", "phenylacetylene")).entity.entity_id
    registry = ReactionRegistry()
    registry.register(_reaction(reaction_id, product=entity_id,
                               evidence_refs=evidence_refs))
    tools = default_tool_registry(
        semantic_resolver=resolver, name_resolver=name_resolver,
        compound_provider=provider, reactions=registry)
    return tools, entity_id


def _eid_from_thread(request: LLMRequest) -> str:
    """The entity id the last resolve_chemical call actually returned."""
    content = request.messages[-1]["content"]
    payload = json.loads(content.split(" = ", 1)[1])
    return payload["data"]["entity"]["entity_id"]


# -- §16 Test 1: resolve -> final -------------------------------------------


def test_resolve_chemical_then_answer():
    agent = ChemistryAgent(_tools(), ScriptedLLM([
        AgentAction(action="tool_call", tool="resolve_chemical",
                    arguments={"kind": "SMILES", "value": "C#Cc1ccccc1"}),
        AgentAction(action="final", summary="苯乙炔是含碳碳三键的芳烃。"),
    ]))
    result = agent.run("苯乙炔是什么？")
    assert result.status == "answered"
    assert result.answer == "苯乙炔是含碳碳三键的芳烃。"
    assert [t.tool for t in result.tool_calls] == ["resolve_chemical"]
    assert result.tool_calls[0].data["entity"]["entity_id"].startswith("CHEM-")


# -- §16 Test 2: resolve -> analyze -> final ---------------------------------


def test_resolve_then_analyze():
    agent = ChemistryAgent(_tools(), ScriptedLLM([
        AgentAction(action="tool_call", tool="resolve_chemical",
                    arguments={"kind": "SMILES", "value": "C#Cc1ccccc1"}),
        AgentAction(action="tool_call", tool="analyze_structure",
                    arguments={"smiles": "C#Cc1ccccc1"}),
        AgentAction(action="final", summary="分子式 C8H6。"),
    ]))
    result = agent.run("分析苯乙炔的结构。")
    assert [t.tool for t in result.tool_calls] == [
        "resolve_chemical", "analyze_structure"]
    assert result.tool_calls[1].data["descriptors"]["molecular_formula"] == "C8H6"


# -- §16 Test 3: resolve -> search_reactions -> final ------------------------


def test_resolve_then_search_reactions():
    tools, entity_id = _stack_with_reaction()
    agent = ChemistryAgent(tools, ScriptedLLM([
        AgentAction(action="tool_call", tool="resolve_chemical",
                    arguments={"kind": "NAME", "value": "phenylacetylene"}),
        AgentAction(action="tool_call", tool="search_reactions",
                    arguments={"product_entity_id": entity_id}),
        AgentAction(action="final", summary="苯乙炔作为产物出现在反应 RXN-1 中。"),
    ]))
    result = agent.run("苯乙炔有哪些已知反应？")
    assert [t.tool for t in result.tool_calls] == [
        "resolve_chemical", "search_reactions"]
    assert result.tool_calls[1].data["count"] == 1
    assert result.tool_calls[1].data["results"][0]["reaction"][
        "reaction_id"] == "RXN-1"


# -- §16 Test 4: resolve(A) + resolve(B) -> compare -> final -----------------


def test_resolve_two_then_compare():
    agent = ChemistryAgent(_tools(), ScriptedLLM([
        AgentAction(action="tool_call", tool="resolve_chemical",
                    arguments={"kind": "SMILES", "value": "C#Cc1ccccc1"}),
        AgentAction(action="tool_call", tool="resolve_chemical",
                    arguments={"kind": "SMILES", "value": "CCc1ccccc1"}),
        AgentAction(action="tool_call", tool="compare_structures",
                    arguments={"smiles_a": "C#Cc1ccccc1",
                               "smiles_b": "CCc1ccccc1"}),
        AgentAction(action="final", summary="骨架相同，侧链不同。"),
    ]))
    result = agent.run("比较苯乙炔和乙苯。")
    assert [t.tool for t in result.tool_calls] == [
        "resolve_chemical", "resolve_chemical", "compare_structures"]
    assert result.tool_calls[2].data["identity"]["same_entity_identity"] is False


# -- §16 Test 5: AMBIGUOUS is never narrowed to candidates[0] ----------------


def test_ambiguous_resolution_is_never_narrowed_to_a_guess():
    tools = _tools(by_name={"vitamin c": [ASCORBIC_ACID, PHENYLACETYLENE]})
    agent = ChemistryAgent(tools, ScriptedLLM([
        AgentAction(action="tool_call", tool="resolve_chemical",
                    arguments={"kind": "NAME", "value": "vitamin C"}),
        AgentAction(action="final", summary="发现 2 个候选结构，需要进一步确认。"),
    ]))
    result = agent.run("维生素C是什么？")
    assert result.tool_calls[0].data["status"] == "ambiguous"
    assert result.tool_calls[0].data["entity"] is None
    # the agent never picked a candidate and ran chemistry on it
    assert [t.tool for t in result.tool_calls] == ["resolve_chemical"]
    assert "候选" in result.answer


# -- §16 Test 6: unavailable is not empty ------------------------------------


def test_unavailable_is_distinguished_from_empty():
    # empty: the registry ran and found nothing -> an honest miss
    tools, _eid = _stack_with_reaction()
    empty_agent = ChemistryAgent(tools, ScriptedLLM([
        AgentAction(action="tool_call", tool="search_reactions",
                    arguments={"product_entity_id": "CHEM-MISSING"}),
        AgentAction(action="final", summary="没有找到相关反应。"),
    ]))
    empty = empty_agent.run("x")
    assert empty.tool_calls[0].status is ToolStatus.OK
    assert "no known reaction matches" in empty.diagnostics

    # unavailable: the tool could not be queried at all
    unavail_agent = ChemistryAgent(_tools(), ScriptedLLM([
        AgentAction(action="tool_call", tool="search_reactions",
                    arguments={"product_entity_id": "C"}),
        AgentAction(action="final", summary="当前反应知识源不可用，无法判断。"),
    ]))
    unavail = unavail_agent.run("x")
    assert unavail.tool_calls[0].status is ToolStatus.UNAVAILABLE
    assert any("unavailable" in d for d in unavail.diagnostics)

    # the two are never conflated
    assert "no known reaction matches" not in unavail.diagnostics
    assert "unavailable" not in " ".join(empty.diagnostics)


# -- §16 Test 7: tool calls go through the registry --------------------------


def test_the_agent_invokes_through_the_registry():
    class SpyRegistry:
        def __init__(self):
            self.invocations = []
            self.described = 0

        def describe(self):
            self.described += 1
            return [{"name": "echo", "description": "d",
                     "arguments": {"type": "object", "properties": {
                         "value": {"type": "string"}}}}]

        def invoke(self, name, arguments=None, step_id="", **kwargs):
            self.invocations.append((name, arguments))
            return ToolResult(tool=name, status=ToolStatus.OK,
                              data={"echoed": (arguments or {}).get("value")})

    spy = SpyRegistry()
    agent = ChemistryAgent(spy, ScriptedLLM([
        AgentAction(action="tool_call", tool="echo", arguments={"value": "hi"}),
        AgentAction(action="final", summary="done"),
    ]))
    result = agent.run("hello")
    assert spy.invocations == [("echo", {"value": "hi"})]
    assert result.tool_calls[0].tool == "echo"
    assert spy.described >= 1, "the schemas came from the registry too"


# -- §16 Test 9: evidence + provenance travel to AgentResult -----------------


def test_evidence_and_provenance_are_retained():
    tools, entity_id = _stack_with_reaction(evidence_refs=["EV-1"])
    agent = ChemistryAgent(tools, ScriptedLLM([
        AgentAction(action="tool_call", tool="search_reactions",
                    arguments={"product_entity_id": entity_id}),
        AgentAction(action="final", summary="找到 1 条反应。"),
    ]))
    result = agent.run("x")
    assert any(e.get("evidence_id") == "EV-1" for e in result.evidence)
    assert any(p.get("tool") == "search_reactions" for p in result.provenance)
    # the full structured result is retained verbatim, not flattened
    assert result.tool_calls[0].data["results"][0]["evidence"] == [
        {"evidence_id": "EV-1"}]


# -- §16 Test 10: the hard cap stops an infinite loop ------------------------


def test_iteration_limit_stops_a_loop():
    script = [
        AgentAction(action="tool_call", tool="analyze_structure",
                    arguments={"smiles": "CCO"}) for _ in range(10)
    ]
    agent = ChemistryAgent(_tools(), ScriptedLLM(script), max_iterations=3)
    result = agent.run("loop")
    assert result.status == "limit_reached"
    assert len(result.tool_calls) == 3
    assert result.iterations == 3
    assert any("limit" in d for d in result.diagnostics)


# -- §17: end-to-end smoke test ---------------------------------------------


def test_e2e_smoke_known_reactions_of_phenylacetylene():
    """The Stage E victory line: one sentence -> resolve -> search_reactions
    -> a natural-language answer, with evidence and provenance intact. The
    chemistry tools are the real Stage C/D implementations; only the LLM is
    scripted."""
    tools, entity_id = _stack_with_reaction(reaction_id="RXN-E2E")
    agent = ChemistryAgent(tools, ScriptedLLM([
        AgentAction(action="tool_call", tool="resolve_chemical",
                    arguments={"kind": "NAME", "value": "phenylacetylene"}),
        lambda req: AgentAction(
            action="tool_call", tool="search_reactions",
            arguments={"product_entity_id": _eid_from_thread(req)}),
        AgentAction(action="final",
                    summary="苯乙炔作为产物出现在反应 RXN-E2E 中（A → 苯乙炔）。"),
    ]))
    result = agent.run("苯乙炔有哪些已知反应？")

    assert result.status == "answered"
    assert [t.tool for t in result.tool_calls] == [
        "resolve_chemical", "search_reactions"]
    # resolve really minted the entity; search really found the reaction
    assert result.tool_calls[0].data["entity"]["entity_id"] == entity_id
    hits = result.tool_calls[1].data["results"]
    assert len(hits) == 1
    assert hits[0]["reaction"]["reaction_id"] == "RXN-E2E"
    assert hits[0]["match_role"] == "product"
    # evidence + provenance travelled all the way to the AgentResult
    assert any(ev.get("evidence_id") == "EV-1" for ev in result.evidence)
    assert any(p.get("tool") == "search_reactions" for p in result.provenance)
    # a natural-language answer exists
    assert result.answer and "苯乙炔" in result.answer

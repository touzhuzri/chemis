"""Evidence-backed transformations (Stage H): documented reaction facts only.

The service answers one question -- does a stored Reaction directly connect
two entities, and in which direction -- and keeps the hard boundaries of
Stage H: similarity/scaffold/fragment are never a transformation, no
evidence is never "unavailable", a reverse lookup is never the forward
record, and inference is never dressed as documented evidence.
"""

from __future__ import annotations

import json

from crea.core.models.reaction import ComponentRole
from crea.core.models.reaction_knowledge import (
    Reaction,
    ReactionComponent,
    ReactionStatus,
)
from crea.core.models.transformation import (
    TransformationDirection,
    TransformationEvidenceLevel,
    TransformationStatus,
)
from crea.core.provenance import Provenance
from crea.services.reaction_builder import ReactionRegistry
from crea.services.transformation import TransformationEvidenceService


def _comp(role, entity_id):
    return ReactionComponent(role=role, name=entity_id, entity_id=entity_id)


def _reaction(rid, reactants, products, evidence_refs=("EV-1",)):
    return Reaction(
        reaction_id=rid, reaction_key=f"k-{rid}", paper_id="paper-a",
        status=ReactionStatus.EVIDENCE_BACKED,
        reactants=[_comp(ComponentRole.REACTANT, r) for r in reactants],
        products=[_comp(ComponentRole.PRODUCT, p) for p in products],
        evidence_refs=list(evidence_refs),
        provenance=Provenance(tool="reaction-builder", source="local_fixture"))


# -- Case 1: direct transformation ------------------------------------------


def test_direct_transformation_carries_reaction_evidence_and_provenance():
    reg = ReactionRegistry()
    reg.register(_reaction("R-001", ["A", "B"], ["T"]))
    result = TransformationEvidenceService(reactions=reg).find_between("A", "T")
    assert result.status is TransformationStatus.DIRECT_TRANSFORMATION
    assert result.matched_reactions == ["R-001"]
    transformation = result.transformations[0]
    assert transformation.direction is TransformationDirection.FORWARD
    assert transformation.evidence_level is TransformationEvidenceLevel.DOCUMENTED
    assert transformation.evidence == [{"evidence_id": "EV-1"}]
    assert transformation.provenance is not None
    assert [c.entity_id for c in transformation.reaction.reactants] == ["A", "B"]
    assert [c.entity_id for c in transformation.reaction.products] == ["T"]


# -- Case 2: reverse query is reverse_of_recorded ---------------------------


def test_reverse_query_is_marked_reverse_not_forward():
    reg = ReactionRegistry()
    reg.register(_reaction("R-001", ["A", "B"], ["T"]))
    result = TransformationEvidenceService(reactions=reg).find_between("T", "A")
    assert result.status is TransformationStatus.DIRECT_TRANSFORMATION
    transformation = result.transformations[0]
    assert transformation.direction is TransformationDirection.REVERSE
    assert any("reverse" in d for d in transformation.diagnostics)


# -- Case 3: no evidence is a successful empty search -----------------------


def test_no_evidence_is_not_unavailable():
    reg = ReactionRegistry()
    reg.register(_reaction("R-001", ["A", "B"], ["T"]))
    result = TransformationEvidenceService(reactions=reg).find_between("Z", "W")
    assert result.status is TransformationStatus.NO_TRANSFORMATION_EVIDENCE
    assert result.transformations == []
    assert result.matched_reactions == []


# -- Case 4: unavailable is not no-evidence ---------------------------------


def test_unavailable_is_distinct_from_no_evidence():
    result = TransformationEvidenceService(reactions=None).find_between("A", "T")
    assert result.status is TransformationStatus.TRANSFORMATION_SEARCH_UNAVAILABLE
    assert result.status is not TransformationStatus.NO_TRANSFORMATION_EVIDENCE
    assert any("wired" in d for d in result.diagnostics)


# -- Case 5: similarity is never a transformation ---------------------------


def test_two_unreacted_entities_never_become_a_transformation():
    reg = ReactionRegistry()
    reg.register(_reaction("R-001", ["A", "B"], ["T"]))
    result = TransformationEvidenceService(reactions=reg).find_between("X", "Y")
    assert result.status is TransformationStatus.NO_TRANSFORMATION_EVIDENCE
    assert result.transformations == [], "no similarity path can fabricate one"


# -- Case 6: n:m reaction ----------------------------------------------------


def test_n_to_m_reactants_match_one_reaction():
    reg = ReactionRegistry()
    reg.register(_reaction("R-003", ["A", "B", "C"], ["T"]))
    svc = TransformationEvidenceService(reactions=reg)
    assert svc.find_transformations(["A", "B", "C"], ["T"]).matched_reactions == [
        "R-003"]
    # the subset still names the same reaction (n:m co-occurrence kept)
    assert svc.find_transformations(["A", "B"], ["T"]).matched_reactions == [
        "R-003"]


# -- related transformation (not direct, not none) --------------------------


def test_related_transformation_is_not_direct_and_not_none():
    reg = ReactionRegistry()
    reg.register(_reaction("R-001", ["A", "B"], ["T"]))
    # Z has no reaction, but T does -> related via T's reaction history
    result = TransformationEvidenceService(reactions=reg).find_between("Z", "T")
    assert result.status is TransformationStatus.RELATED_TRANSFORMATION
    assert result.matched_reactions == ["R-001"]
    assert result.transformations == [], "no DIRECT transformation exists"


# -- invalid queries ---------------------------------------------------------


def test_invalid_queries_are_rejected():
    svc = TransformationEvidenceService(reactions=ReactionRegistry())
    assert svc.find_transformations([], ["T"]).status is TransformationStatus.INVALID
    assert svc.find_transformations(["A"], []).status is TransformationStatus.INVALID
    assert svc.find_transformations(["A"], ["A"]).status is TransformationStatus.INVALID


# -- tool integration (§16) --------------------------------------------------


def test_find_chemical_transformations_tool_goes_through_the_registry():
    from crea.tools.registry import default_tool_registry

    reg = ReactionRegistry()
    reg.register(_reaction("R-001", ["A", "B"], ["T"]))
    tools = default_tool_registry(reactions=reg)

    result = tools.invoke("find_chemical_transformations",
                          {"candidate": "A", "target": "T"})
    assert result.status.value == "ok"
    assert result.data["status"] == "direct_transformation"
    assert result.data["matched_reactions"] == ["R-001"]

    n_to_m = tools.invoke("find_chemical_transformations",
                          {"reactant_entity_ids": ["A", "B"],
                           "product_entity_ids": ["T"]})
    assert n_to_m.data["status"] == "direct_transformation"

    invalid = tools.invoke("find_chemical_transformations", {})
    assert invalid.data["status"] == "invalid"

    # unwired -> unavailable, never no-evidence
    unwired = default_tool_registry()
    unavail = unwired.invoke("find_chemical_transformations",
                             {"candidate": "A", "target": "T"})
    assert unavail.data["status"] == "transformation_search_unavailable"


# -- Agent natural-language E2E (§18/§28) -----------------------------------


def test_agent_asks_about_transformations_end_to_end():
    from crea.agent.chemistry_agent import ChemistryAgent
    from crea.core.models.research import AgentAction
    from crea.llm.base import LLMRequest, LLMResponse
    from crea.providers.name_lookup import CompoundNameLookup
    from crea.semantic.naming import NameResolver
    from crea.semantic.resolver import ChemicalInput, SemanticResolver
    from crea.services.entity_registry import ChemicalEntityRegistry
    from crea.services.identity import IdentityResolutionService
    from crea.tools.registry import default_tool_registry
    from tests.fakes.compound import FakeCompoundProvider, PHENYLACETYLENE

    provider = FakeCompoundProvider(by_name={"phenylacetylene": [PHENYLACETYLENE]})
    entities = ChemicalEntityRegistry()
    name_resolver = NameResolver([CompoundNameLookup(provider)])
    resolver = SemanticResolver(identity_service=IdentityResolutionService(),
                                name_resolver=name_resolver,
                                entity_registry=entities)
    target = resolver.resolve(
        ChemicalInput.of("SMILES", "C#Cc1ccccc1")).entity
    candidate = resolver.resolve(
        ChemicalInput.of("SMILES", "Cc1ccccc1")).entity

    reg = ReactionRegistry()
    reg.register(_reaction("R-H", [candidate.entity_id], [target.entity_id]))
    tools = default_tool_registry(semantic_resolver=resolver,
                                  name_resolver=name_resolver,
                                  compound_provider=provider,
                                  reactions=reg)

    def _ids(request: LLMRequest):
        found = []
        for msg in reversed(request.messages):
            content = msg.get("content", "")
            if content.startswith("tool_result(resolve_chemical)"):
                payload = json.loads(content.split(" = ", 1)[1])
                found.append(payload["data"]["entity"]["entity_id"])
            if len(found) == 2:
                break
        return found  # [candidate (last resolve), target (first resolve)]

    class ScriptedLLM:
        name = "scripted"
        model = "s"
        def __init__(self, script):
            self._script = list(script)
        def complete(self, request):
            entry = self._script.pop(0)
            action = entry(request) if callable(entry) else entry
            return LLMResponse(
                text=json.dumps(action.model_dump(mode="json"), ensure_ascii=False),
                provider=self.name, model=self.model)

    agent = ChemistryAgent(tools, ScriptedLLM([
        AgentAction(action="tool_call", tool="resolve_chemical",
                    arguments={"kind": "SMILES", "value": "C#Cc1ccccc1"}),
        AgentAction(action="tool_call", tool="resolve_chemical",
                    arguments={"kind": "SMILES", "value": "Cc1ccccc1"}),
        lambda req: AgentAction(
            action="tool_call", tool="find_chemical_transformations",
            arguments={"candidate": _ids(req)[0], "target": _ids(req)[1]}),
        AgentAction(action="final",
                    summary="发现 1 条已有反应记录，候选可以直接转化为目标 "
                            "（反应 R-H，documented）。"),
    ]))
    result = agent.run("这个候选分子能不能通过已有反应转化成目标分子？")
    assert result.status == "answered"
    tools_used = [t.tool for t in result.tool_calls]
    assert "find_chemical_transformations" in tools_used
    transformation = result.tool_calls[-1].data
    assert transformation["status"] == "direct_transformation"
    assert transformation["matched_reactions"] == ["R-H"]
    assert transformation["transformations"][0]["direction"] == "forward"
    assert "已有反应" in result.answer

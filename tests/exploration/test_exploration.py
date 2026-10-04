"""Chemical Exploration (Stage G): structure space, not retrosynthesis.

Deterministic throughout. The explorer composes the existing chemistry
services (normalize / fingerprint / scaffold / fragment) and the existing
identity + reaction seams; only the candidate space is a fixed fixture.
Reaction evidence stays four-way distinct (direct / related / none /
unavailable), similarity is a number never a verdict, and dedup is by the
existing identity key, never by SMILES string equality (§24).
"""

from __future__ import annotations

import pytest

from crea.core.models.exploration import (
    ReactionEvidenceStatus,
    RelationshipKind,
)
from crea.core.models.reaction import ComponentRole
from crea.core.models.reaction_knowledge import (
    Reaction,
    ReactionComponent,
    ReactionStatus,
)
from crea.semantic.resolver import ChemicalInput, SemanticResolver
from crea.services.entity_registry import ChemicalEntityRegistry
from crea.services.exploration import (
    ChemicalExplorer,
    FixedChemicalSpace,
    LocalEntitySpace,
)
from crea.services.identity import IdentityResolutionService
from crea.services.reaction_builder import ReactionRegistry

PHENYLACETYLENE = "C#Cc1ccccc1"
TOLUENE = "Cc1ccccc1"
DIPHENYLACETYLENE = "c1ccccc1C#Cc1ccccc1"
BENZENE = "c1ccccc1"
THIOPHENE = "c1ccsc1"


def _registry_with(smiles_list):
    """A resolver + entity registry with the given structures resolved."""
    from crea.providers.name_lookup import CompoundNameLookup
    from crea.semantic.naming import NameResolver
    from tests.fakes.compound import FakeCompoundProvider, PHENYLACETYLENE as P

    provider = FakeCompoundProvider(by_name={"phenylacetylene": [P]})
    entities = ChemicalEntityRegistry()
    name_resolver = NameResolver([CompoundNameLookup(provider)])
    resolver = SemanticResolver(identity_service=IdentityResolutionService(),
                                name_resolver=name_resolver,
                                entity_registry=entities)
    for smiles in smiles_list:
        resolver.resolve(ChemicalInput.of("SMILES", smiles))
    return resolver, entities


def _entity(resolver, smiles):
    return resolver.resolve(ChemicalInput.of("SMILES", smiles)).entity


def _reaction(rid, reactant_id, product_id, evidence_refs=("EV-1",)):
    return Reaction(
        reaction_id=rid, reaction_key=f"key-{rid}", paper_id="paper-a",
        status=ReactionStatus.EVIDENCE_BACKED,
        reactants=[ReactionComponent(role=ComponentRole.REACTANT, name="r",
                                     entity_id=reactant_id)],
        products=[ReactionComponent(role=ComponentRole.PRODUCT, name="p",
                                    entity_id=product_id)],
        evidence_refs=list(evidence_refs))


# -- Test 1: similarity candidates -----------------------------------------


def test_similarity_candidates_are_returned_with_scores():
    resolver, entities = _registry_with([BENZENE, THIOPHENE])
    target = _entity(resolver, BENZENE)
    explorer = ChemicalExplorer(entity_registry=entities)
    result = explorer.explore(target.entity_id)
    assert result.target.entity_id == target.entity_id
    assert result.candidates, "the similar candidate was returned"
    candidate = result.candidates[0]
    assert candidate.similarity >= 0.2
    assert candidate.scaffold_relation == "different"
    assert candidate.relationship is RelationshipKind.SIMILAR


# -- Test 2: dedup by identity, never by SMILES string ----------------------


def test_candidates_dedupe_by_identity_not_smiles():
    resolver, entities = _registry_with([PHENYLACETYLENE])
    target = _entity(resolver, PHENYLACETYLENE)
    # two spellings of toluene, plus one unrelated
    space = FixedChemicalSpace(["Cc1ccccc1", "C1(=CC=CC=C1)C", "CCO"])
    explorer = ChemicalExplorer(entity_registry=entities, space=space)
    result = explorer.explore(target.entity_id)
    got = [c.entity.canonical_smiles for c in result.candidates]
    assert got == ["Cc1ccccc1"], "the two toluene spellings are one candidate"


# -- Test 3: same scaffold --------------------------------------------------


def test_same_scaffold_relationship():
    resolver, entities = _registry_with([PHENYLACETYLENE, TOLUENE])
    target = _entity(resolver, PHENYLACETYLENE)
    explorer = ChemicalExplorer(entity_registry=entities)
    result = explorer.explore(target.entity_id)
    candidate = result.candidates[0]
    assert candidate.scaffold_relation == "same"
    assert candidate.relationship is RelationshipKind.SAME_SCAFFOLD


# -- Test 4: fragment relationship (different scaffold, shared fragment) ----


def test_fragment_relationship_without_same_scaffold():
    resolver, entities = _registry_with([DIPHENYLACETYLENE, PHENYLACETYLENE])
    target = _entity(resolver, DIPHENYLACETYLENE)
    explorer = ChemicalExplorer(entity_registry=entities)
    result = explorer.explore(target.entity_id)
    candidates = {c.entity.canonical_smiles: c for c in result.candidates}
    assert PHENYLACETYLENE in candidates
    candidate = candidates[PHENYLACETYLENE]
    assert candidate.fragment_related is True
    assert candidate.relationship is RelationshipKind.FRAGMENT_RELATED


# -- Test 5: direct reaction evidence ---------------------------------------


def test_direct_reaction_evidence_candidate_to_target():
    resolver, entities = _registry_with([PHENYLACETYLENE, TOLUENE])
    target = _entity(resolver, PHENYLACETYLENE)
    toluene = _entity(resolver, TOLUENE)
    reactions = ReactionRegistry()
    reactions.register(_reaction("RXN-DIRECT", toluene.entity_id,
                                 target.entity_id))
    explorer = ChemicalExplorer(entity_registry=entities, reactions=reactions)
    result = explorer.explore(target.entity_id)
    candidate = result.candidates[0]
    assert candidate.reaction_evidence is (
        ReactionEvidenceStatus.DIRECT_REACTION_EVIDENCE)
    assert [r["reaction_id"] for r in candidate.reactions] == ["RXN-DIRECT"]


# -- Test 6: no reaction evidence (search succeeds, empty) ------------------


def test_no_reaction_evidence_is_a_successful_empty_search():
    resolver, entities = _registry_with([PHENYLACETYLENE, TOLUENE])
    target = _entity(resolver, PHENYLACETYLENE)
    reactions = ReactionRegistry()  # wired, but empty
    explorer = ChemicalExplorer(entity_registry=entities, reactions=reactions)
    result = explorer.explore(target.entity_id)
    candidate = result.candidates[0]
    assert candidate.reaction_evidence is ReactionEvidenceStatus.NO_REACTION_EVIDENCE
    assert candidate.reactions == []


# -- Test 7: unavailable is not empty ---------------------------------------


def test_reaction_search_unavailable_is_not_empty():
    resolver, entities = _registry_with([PHENYLACETYLENE, TOLUENE])
    target = _entity(resolver, PHENYLACETYLENE)
    explorer = ChemicalExplorer(entity_registry=entities, reactions=None)
    result = explorer.explore(target.entity_id)
    candidate = result.candidates[0]
    assert candidate.reaction_evidence is (
        ReactionEvidenceStatus.REACTION_SEARCH_UNAVAILABLE)
    # it is not the same fact as "searched and found nothing"
    assert candidate.reaction_evidence is not (
        ReactionEvidenceStatus.NO_REACTION_EVIDENCE)


# -- Test 8/9: provenance + evidence on candidates --------------------------


def test_candidate_carries_evidence_and_provenance():
    resolver, entities = _registry_with([PHENYLACETYLENE, TOLUENE])
    target = _entity(resolver, PHENYLACETYLENE)
    toluene = _entity(resolver, TOLUENE)
    reactions = ReactionRegistry()
    reactions.register(_reaction("RXN-EV", toluene.entity_id, target.entity_id,
                                 evidence_refs=["EV-9"]))
    explorer = ChemicalExplorer(entity_registry=entities, reactions=reactions)
    result = explorer.explore(target.entity_id)
    candidate = result.candidates[0]
    assert any(e.get("evidence_id") == "EV-9" for e in candidate.evidence)
    assert candidate.provenance and candidate.provenance["count"] == 1
    # the reaction payload is traceable to its reaction + evidence
    assert candidate.reactions[0]["evidence_refs"] == ["EV-9"]


# -- Test 10: tool integration through the registry -------------------------


def test_explore_chemical_space_tool_goes_through_the_registry():
    from crea.tools.registry import default_tool_registry

    resolver, entities = _registry_with([PHENYLACETYLENE, TOLUENE])
    target = _entity(resolver, PHENYLACETYLENE)
    tools = default_tool_registry(semantic_resolver=resolver,
                                  name_resolver=resolver._names,
                                  reactions=ReactionRegistry())
    result = tools.invoke("explore_chemical_space",
                          {"entity_id": target.entity_id})
    assert result.status.value == "ok"
    assert result.data["target"]["entity_id"] == target.entity_id
    assert result.data["source"] == "local"
    assert result.data["candidates"][0]["entity"]["canonical_smiles"] == TOLUENE

    # the name path resolves through the resolver first
    named = tools.invoke("explore_chemical_space",
                         {"chemical": "phenylacetylene"})
    assert named.status.value == "ok"
    assert named.data["target"]["entity_id"] == target.entity_id


# -- Test 12: natural-language E2E through the agent ------------------------


def test_agent_explores_chemical_space_end_to_end():
    import json

    from crea.agent.chemistry_agent import ChemistryAgent
    from crea.core.models.research import AgentAction
    from crea.llm.base import LLMRequest, LLMResponse
    from crea.tools.registry import default_tool_registry

    resolver, entities = _registry_with([PHENYLACETYLENE, TOLUENE])
    target = _entity(resolver, PHENYLACETYLENE)
    toluene = _entity(resolver, TOLUENE)
    reactions = ReactionRegistry()
    reactions.register(_reaction("RXN-E2E", toluene.entity_id, target.entity_id,
                                 evidence_refs=["EV-1"]))
    tools = default_tool_registry(semantic_resolver=resolver,
                                  name_resolver=resolver._names,
                                  reactions=reactions)

    def _eid(request: LLMRequest) -> str:
        payload = json.loads(request.messages[-1]["content"].split(" = ", 1)[1])
        return payload["data"]["entity"]["entity_id"]

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
                    arguments={"kind": "NAME", "value": "phenylacetylene"}),
        lambda req: AgentAction(
            action="tool_call", tool="explore_chemical_space",
            arguments={"entity_id": _eid(req)}),
        AgentAction(action="final",
                    summary="苯乙炔周围找到 1 个候选：甲苯，共享苯环骨架，"
                            "相似度约 0.32。"),
    ]))
    result = agent.run("帮我探索一下苯乙炔周围的化学结构。")
    assert result.status == "answered"
    assert [t.tool for t in result.tool_calls] == [
        "resolve_chemical", "explore_chemical_space"]
    exploration = result.tool_calls[1].data
    assert exploration["target"]["entity_id"] == target.entity_id
    assert exploration["candidates"][0]["entity"]["canonical_smiles"] == TOLUENE
    assert exploration["candidates"][0]["reaction_evidence"] == (
        "direct_reaction_evidence")
    assert "苯乙炔" in result.answer

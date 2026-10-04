"""The Chemistry Tool Layer: one contract, honest boundaries (Stage C).

What is under test (§16/§20): the registry registers/discovers/invokes;
every result is the ONE structured `ToolResult`; failures arrive as
normalized codes, never raw exceptions; execution modes are declared and
real; the agent bridge carries the same contract; and a backend that is
absent reports `unavailable` -- never a fabricated success.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from crea.core.models.research import ToolResult, ToolStatus
from crea.semantic.naming import NameLookupHit, NameLookupResult, NameResolver
from crea.semantic.resolver import SemanticResolver
from crea.services.entity_registry import ChemicalEntityRegistry
from crea.services.identity import IdentityResolutionService
from crea.tools.contract import (
    ChemistryTool,
    ExecutionMode,
    ToolError,
    ToolErrorCode,
)
from crea.tools.registry import ChemistryToolRegistry, default_tool_registry
from tests.fakes.compound import FakeCompoundProvider, PHENYLACETYLENE

_OPSIN_HOME = Path(__file__).resolve().parents[2] / "runtime" / "opsin"
_OPSIN_READY = (shutil.which("java") is not None
                and any(_OPSIN_HOME.glob("*.jar")))


class _Args:
    """Minimal pydantic-free args stand-in is not used -- the contract
    requires a real pydantic model; see `_EchoTool` below."""

    pass


from pydantic import BaseModel  # noqa: E402


class EchoArgs(BaseModel):
    value: str = ""


class EchoTool(ChemistryTool):
    name = "echo"
    description = "test tool"
    arguments_model = EchoArgs

    def execute(self, args: EchoArgs):
        return {"value": args.value}


class ExplodingTool(ChemistryTool):
    name = "explode"
    description = "raises a raw exception at the boundary"
    arguments_model = EchoArgs

    def execute(self, args: EchoArgs):
        raise RuntimeError("boom")


class UnavailableTool(ChemistryTool):
    name = "absent"
    description = "backend missing"
    arguments_model = EchoArgs

    def execute(self, args: EchoArgs):
        raise ToolError(ToolErrorCode.UNAVAILABLE, "no backend here",
                        diagnostics=["backend not configured"])


@pytest.fixture()
def spine():
    from crea.semantic.naming import NameResolver
    from crea.providers.name_lookup import CompoundNameLookup

    entities = ChemicalEntityRegistry()
    resolver = SemanticResolver(
        identity_service=IdentityResolutionService(),
        name_resolver=NameResolver([CompoundNameLookup(
            FakeCompoundProvider(by_name={"phenylacetylene": [PHENYLACETYLENE]}))]),
        entity_registry=entities)
    return resolver


@pytest.fixture()
def tools(spine):
    return default_tool_registry(
        semantic_resolver=spine, name_resolver=spine._names,
        compound_provider=FakeCompoundProvider(
            by_name={"phenylacetylene": [PHENYLACETYLENE]}))


# -- registry: register / discover / invoke (§20.1) -----------------------


def test_registry_registers_discovers_and_invokes(tools):
    names = tools.names()
    for expected in ("resolve_chemical", "name_structure", "analyze_structure",
                     "compare_structures", "search_chemical",
                     "search_literature", "search_reactions",
                     "retrosynthesize"):
        assert expected in names
    described = {d["name"]: d for d in tools.describe()}
    assert described["resolve_chemical"]["capabilities"]
    assert "arguments" in described["resolve_chemical"]

    result = tools.invoke("analyze_structure", {"smiles": "CCO"})
    assert result.status is ToolStatus.OK
    assert result.data["descriptors"]["molecular_formula"] == "C2H6O"

    # duplicates and unknowns are refusals, not accidents
    with pytest.raises(ValueError):
        tools.register(tools.get("analyze_structure"))
    missing = tools.invoke("nope", {})
    assert missing.status is ToolStatus.ERROR
    assert missing.error_type == "not_found"


# -- one result contract, structured (§20.2-4) -----------------------------


def test_every_result_is_the_one_structured_contract(tools):
    result = tools.invoke("analyze_structure", {"smiles": "CCO"})
    assert isinstance(result, ToolResult)
    assert isinstance(result.diagnostics, list)
    assert result.provenance and "tool" in result.provenance
    assert result.provenance.get("backend") == "rdkit"


def test_errors_are_normalized_at_the_boundary():
    registry = ChemistryToolRegistry()
    registry.register(ExplodingTool())
    registry.register(UnavailableTool())
    registry.register(EchoTool())

    internal = registry.invoke("explode", {"value": "x"})
    assert internal.status is ToolStatus.ERROR
    assert internal.error_type == "internal"
    assert "RuntimeError" in internal.error and "boom" in internal.error

    unavailable = registry.invoke("absent", {})
    assert unavailable.status is ToolStatus.UNAVAILABLE
    assert unavailable.error_type == "unavailable"
    assert unavailable.diagnostics == ["backend not configured"]

    # truly invalid input -> REJECTED, never executed
    class StrictArgs(BaseModel):
        count: int

    class StrictTool(ChemistryTool):
        name = "strict"
        description = ""
        arguments_model = StrictArgs

        def execute(self, args):
            return {"count": args.count}

    registry.register(StrictTool())
    rejected = registry.invoke("strict", {"count": "not-a-number"})
    assert rejected.status is ToolStatus.REJECTED
    assert rejected.error_type == "invalid_input"


# -- execution modes (§20.5-6) --------------------------------------------


def test_execution_modes_are_declared_and_the_worker_transport_is_separate(
        tools):
    described = {d["name"]: d for d in tools.describe()}
    assert "in_process" in described["resolve_chemical"]["execution_modes"]
    assert "isolated_worker" in described["retrosynthesize"]["execution_modes"]
    assert "remote_provider" in described["search_chemical"]["execution_modes"]
    # the worker envelope never surfaces in tool results
    for entry in described.values():
        assert "crea-engine" not in json_of(entry)


def json_of(value) -> str:
    import json

    return json.dumps(value, default=str)


def test_isolated_worker_execution_runs_through_the_engine_adapter():
    """ISOLATED_WORKER without a runtime is honest unavailability (§:
    never fake success)."""
    from crea.tools.execution import IsolatedWorkerExecutor

    class DeadClient:
        def name_to_structure(self, name, expected_provider="opsin"):
            from crea.engine.errors import WorkerUnavailable

            raise WorkerUnavailable("no worker")

    executor = IsolatedWorkerExecutor(client_factory=lambda: DeadClient())
    with pytest.raises(ToolError) as excinfo:
        executor.name_to_structure("ethynylbenzene")
    assert excinfo.value.code is ToolErrorCode.UNAVAILABLE


@pytest.mark.skipif(not _OPSIN_READY,
                    reason="OPSIN runtime (java + jars) not present")
def test_name_structure_runs_through_the_real_isolated_worker():
    from crea.providers.name_lookup import OpsinNameLookup

    tools = default_tool_registry(
        name_resolver=NameResolver([OpsinNameLookup()]))
    result = tools.invoke("name_structure", {"name": "ethynylbenzene"})
    assert result.status is ToolStatus.OK
    assert result.data["status"] == "resolved"
    assert result.data["identity_evidence"] == "supported"
    providers = {p.get("provider"): p.get("status")
                 for p in result.data["providers"]}
    assert providers.get("opsin") == "ok"
    assert "isolated_worker" in result.provenance["execution_modes"]


# -- resolve_chemical (§20.7) ---------------------------------------------


def test_resolve_chemical_is_the_unified_identity_entry(tools):
    resolved = tools.invoke("resolve_chemical",
                            {"kind": "SMILES", "value": "C#Cc1ccccc1"})
    assert resolved.status is ToolStatus.OK
    assert resolved.data["status"] == "resolved"
    assert resolved.data["identity_evidence"] == "exact"
    assert resolved.data["entity"]["entity_id"].startswith("CHEM-")

    named = tools.invoke("resolve_chemical",
                         {"kind": "NAME", "value": "phenylacetylene"})
    assert named.data["entity"]["entity_id"] == resolved.data["entity"][
        "entity_id"], "one identity, one entity"

    bad = tools.invoke("resolve_chemical",
                       {"kind": "SMILES", "value": "not-a-smiles"})
    assert bad.data["status"] == "invalid"
    assert bad.data["entity"] is None


# -- name_structure (§20.8) + CIR honesty ---------------------------------


def test_name_structure_reports_its_providers_and_never_fakes_cir(tools):
    result = tools.invoke("name_structure", {"name": "phenylacetylene"})
    assert result.status is ToolStatus.OK
    assert result.data["selected"], "the chain answered"
    assert any(p["provider"] != "chain" for p in result.data["providers"])
    # CIR is declared disabled -- not silently skipped, not faked
    assert any("nci_cir" in d for d in result.data["providers_disabled"])

    absent = tools.invoke("name_structure", {"name": "zzznocnamezz"})
    assert absent.data["status"] in ("not_found", "unavailable")
    assert absent.data["selected"] is None


# -- compare_structures: identity != similarity (§7) -----------------------


def test_compare_structures_separates_identity_from_similarity(tools):
    same = tools.invoke("compare_structures", {
        "smiles_a": "C#Cc1ccccc1", "smiles_b": "C(#C)c1ccccc1"})
    assert same.data["identity"]["same_entity_identity"] is True
    assert same.data["identity"]["basis"] == "exact_structure"

    related = tools.invoke("compare_structures", {
        "smiles_a": "C#Cc1ccccc1", "smiles_b": "CCO"})
    assert related.data["identity"]["same_entity_identity"] is False
    assert related.data["similarity"]["screening_only"] is True
    assert "never identity" in related.data["similarity"]["note"]


# -- literature policy survives the tool boundary (§8) ---------------------


def test_search_literature_keeps_the_access_policy_fields(tools):
    result = tools.invoke("search_literature",
                          {"query": "phenylacetylene", "limit": 3})
    # the offline fixture literature is not wired in this registry: the
    # tool must say unavailable rather than invent papers
    assert result.status is ToolStatus.UNAVAILABLE
    assert result.error_type == "unavailable"


def test_search_literature_with_a_real_service_preserves_policy_fields():
    from crea.agent.offline import FixtureTransport
    from crea.services.literature import LiteratureService
    from crea.sources.crossref import CrossrefProvider, WORKS_ENDPOINT as CR
    from crea.sources.openalex import OpenAlexProvider, WORKS_ENDPOINT as OA
    from crea.tools.chemistry import SearchLiteratureTool

    transport = FixtureTransport({
        OA: {"meta": {"count": 1, "page": 1, "per_page": 10},
             "results": [{
                 "id": "https://openalex.org/W1", "doi": "https://doi.org/10.0000/x",
                 "display_name": "A synthetic record", "title": "A synthetic record",
                 "publication_year": 2021, "ids": {},
                 "authorships": [{"author": {"display_name": "A Synthetic"}}],
                 "abstract_inverted_index": None,
                 "open_access": {"is_oa": False, "oa_status": "closed",
                                 "oa_url": None},
                 "best_oa_location": None}]},
        CR: {"status": "ok", "message-type": "work-list",
             "message": {"items": [{
                 "DOI": "10.0000/x", "title": ["A synthetic record"],
                 "container-title": ["J"], "author": [],
                 "issued": {"date-parts": [[2021, 1, 1]]},
                 "publisher": "P", "type": "journal-article"}],
                 "total-results": 1}},
    })
    literature = LiteratureService(providers={
        "openalex": OpenAlexProvider(transport=transport, endpoint=OA),
        "crossref": CrossrefProvider(transport=transport, endpoint=CR),
    })
    result = SearchLiteratureTool(literature=literature).invoke(
        query="q", limit=3)
    assert result.status is ToolStatus.OK
    paper = result.data["papers"][0]
    assert paper["doi"] == "10.0000/x"
    assert paper["oa_status"], "OA status travels with the record"
    assert paper["fulltext_permission"], "permission travels too"
    assert "fail-closed" in result.data["note"] or (
        "never evidence" in result.data["note"])


# -- search_reactions: a real MVP tool (Stage D §12/§13) ------------------


def _reaction_fixture():
    from crea.core.models.entity import ChemicalEntity
    from crea.core.models.reaction import ComponentRole
    from crea.core.models.reaction_knowledge import (
        Reaction,
        ReactionComponent,
        ReactionStatus,
    )
    from crea.services.reaction_builder import ReactionRegistry

    def comp(role, eid):
        return ReactionComponent(role=role, name="x", entity_id=eid)

    reaction = Reaction(
        reaction_id="RXN-1", reaction_key="k-demo", paper_id="paper-a",
        status=ReactionStatus.EVIDENCE_BACKED,
        reactants=[comp(ComponentRole.REACTANT, "A"),
                   comp(ComponentRole.REACTANT, "B")],
        products=[comp(ComponentRole.PRODUCT, "C")],
        evidence_refs=["EV-1"])
    return reaction


def test_search_reactions_returns_reasons_not_just_ids():
    from crea.services.reaction_builder import ReactionRegistry

    registry = ReactionRegistry()
    reaction = _reaction_fixture()
    registry.register(reaction)
    tools = default_tool_registry(reactions=registry)

    by_product = tools.invoke("search_reactions", {"product_entity_id": "C"})
    assert by_product.status is ToolStatus.OK
    assert by_product.data["count"] == 1
    entry = by_product.data["results"][0]
    assert entry["reaction"]["reaction_id"] == "RXN-1"
    assert entry["match_role"] == "product"
    assert entry["matched_entities"] == ["C"]
    assert entry["evidence"] == [{"evidence_id": "EV-1"}]
    assert "atom mapping not performed" in entry["diagnostics"]

    by_reactant = tools.invoke("search_reactions", {"reactant_entity_id": "A"})
    assert by_reactant.data["results"][0]["match_role"] == "reactant"

    empty = tools.invoke("search_reactions", {"product_entity_id": "Z"})
    assert empty.status is ToolStatus.OK and empty.data["count"] == 0
    assert empty.diagnostics, "a miss is stated, not hidden (§5)"


def test_search_reactions_failures_are_honest():
    from crea.services.reaction_builder import ReactionRegistry

    unwired = default_tool_registry()
    assert unwired.invoke("search_reactions",
                          {"product_entity_id": "C"}).status is (
        ToolStatus.UNAVAILABLE)

    wired = default_tool_registry(reactions=ReactionRegistry())
    invalid = wired.invoke("search_reactions", {})
    assert invalid.status is ToolStatus.REJECTED
    assert invalid.error_type == "invalid_input"


# -- agent bridge (§4/§20) -------------------------------------------------


def test_tools_bridge_into_the_agent_registry_with_the_same_contract(tools):
    from crea.agent.tools import ToolRegistry

    agent_registry = ToolRegistry()
    tools.bridge_to(agent_registry)
    assert "resolve_chemical" in agent_registry.names()
    result = agent_registry.execute("resolve_chemical",
                                    {"kind": "SMILES", "value": "CCO"})
    assert result.status is ToolStatus.OK
    assert result.data["result"]["status"] == "resolved"
    assert "provenance" in result.data


# -- discovery metadata (§20.5) --------------------------------------------


def test_describe_carries_contract_metadata(tools):
    entry = next(d for d in tools.describe() if d["name"] == "name_structure")
    assert entry["version"]
    assert entry["timeout_s"] > 0
    assert entry["provenance_policy"]
    assert set(entry["execution_modes"]) == {
        "in_process", "isolated_worker", "remote_provider"}

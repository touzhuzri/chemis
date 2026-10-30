"""M1.1 -- the unified agent tool surface and its execution guards.

The old registries stay non-enforcing and untouched. `build_agent_tools()`
composes them into one enforcing registry: canonical names, aliases, declared
access/effects/approval, preconditions, output budgets and honest timeout
reporting.

No test here reaches the network or a real knowledge base. Literature uses the
offline service with no providers; routes use the fake retrosynthesis provider;
document tools are only tested at the approval boundary.
"""

from __future__ import annotations

import time

import pytest
from pydantic import BaseModel, Field

from crea.agent.evidence_tools import build_evidence_tool_registry
from crea.agent.tool_surface import (
    ALIASES,
    ALL_CANONICALS,
    build_agent_tools,
)
from crea.agent.tools import (
    Access,
    Approval,
    Effects,
    ExecutionPolicy,
    ToolRegistry,
    ToolSpec,
)
from crea.agent.toolsets import build_registry
from crea.agent.workspace import IdentityState, RouteWorkbench
from crea.core.models.research import ToolResult, ToolStatus
from crea.providers.fake_retrosynthesis import FakeRetrosynthesisProvider
from crea.services.identity import IdentityResolutionService
from crea.services.literature import LiteratureService
from crea.services.reaction_builder import ReactionRegistry
from crea.services.route_validation import RouteValidationService
from crea.tools.registry import default_tool_registry

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"


class _Args(BaseModel):
    value: str = Field(default="value")


def _spec(name: str, run, **kwargs) -> ToolSpec:
    return ToolSpec(name=name, description=name, arguments_model=_Args,
                    run=run, **kwargs)


class _Exploding:
    def __getattr__(self, name):
        def boom(*_args, **_kwargs):
            raise AssertionError(f"a tool body executed and used {name!r}")

        return boom


# ---------------------------------------------------------------------------
# The unified surface
# ---------------------------------------------------------------------------


def test_build_agent_tools_is_enforcing_and_has_one_canonical_per_capability():
    registry = build_agent_tools()
    assert registry.enforce_policy is True
    assert len(registry.canonical_names) == 36
    assert len(set(registry.canonical_names)) == 36
    assert set(registry.canonical_names) == ALL_CANONICALS
    assert len(registry.names()) == 44  # 36 canonical + 8 aliases


def test_every_alias_points_at_an_existing_canonical():
    registry = build_agent_tools()
    for alias, canonical in registry.alias_of.items():
        assert canonical in registry.canonical_names, alias
        assert registry.get(alias).alias_of == canonical
        assert registry.get(alias).canonical is False


def test_alias_metadata_matches_its_canonical():
    registry = build_agent_tools()
    for alias, canonical in registry.alias_of.items():
        a = registry.get(alias)
        c = registry.get(canonical)
        assert a.access == c.access
        assert a.effects == c.effects
        assert a.approval == c.approval
        assert a.timeout_s == c.timeout_s
        assert a.requires == c.requires


def test_describe_carries_the_declared_policy():
    registry = build_agent_tools()
    for entry in registry.describe():
        assert entry["access"], entry["name"]
        assert entry["effects"] in {e.value for e in Effects}
        assert entry["approval"] in {a.value for a in Approval}
        assert entry["timeout_s"] > 0
        assert entry["output_limit_chars"] > 0
        assert entry["input_schema"] == entry["arguments"]


def test_building_and_describing_executes_no_tool_body():
    registry = build_agent_tools(
        literature=_Exploding(), documents=_Exploding(), routes=_Exploding(),
        transformation_search=_Exploding(), comparison=_Exploding(),
        reaction_evidence=_Exploding(), route_explorer=_Exploding(),
        explorer=_Exploding(), transformation_service=_Exploding(),
    )
    assert registry.describe()


def test_analyze_alias_and_canonical_return_the_same_result():
    registry = build_agent_tools()
    direct = registry.execute("analyze_molecule", {"smiles": "CCO"})
    alias = registry.execute("analyze_structure", {"smiles": "CCO"})
    assert direct.status is ToolStatus.OK
    assert alias.status is ToolStatus.OK
    assert direct.data["molecular_formula"] == alias.data["molecular_formula"]
    assert alias.tool == "analyze_structure"


def test_compare_structures_merges_both_surfaces_without_losing_fields():
    registry = build_agent_tools()
    result = registry.execute("compare_structures",
                              {"smiles_a": "CCO", "smiles_b": "CCN"})
    assert result.status is ToolStatus.OK
    # research verdict/MCS fields
    assert {"verdict", "verdict_reasons", "common_atoms",
            "differing_regions"} <= set(result.data)
    # workspace identity/similarity fields
    assert {"identity", "similarity", "scaffold"} <= set(result.data)


def test_search_literature_uses_one_envelope():
    registry = build_agent_tools(
        literature=LiteratureService({}),
        policy=ExecutionPolicy(allow_network=True))
    result = registry.execute("search_literature", {"query": "phenylacetylene"})
    assert result.status is ToolStatus.OK
    assert {"query", "outcome", "count", "paper_count", "papers",
            "warnings", "note"} <= set(result.data)
    assert result.data["count"] == result.data["paper_count"]


def test_search_reactions_accepts_the_workspace_singular_argument():
    registry = build_agent_tools(reactions=ReactionRegistry())
    result = registry.execute("search_reactions",
                              {"reactant_entity_id": "CHEM-LEGACY"})
    assert result.status is ToolStatus.OK
    assert result.data["status"] == "ok"
    assert result.data["count"] == 0


def test_retrosynthesize_says_routes_are_session_stored_not_persisted():
    workbench = RouteWorkbench(provider=FakeRetrosynthesisProvider(),
                               validator=RouteValidationService())
    registry = build_agent_tools(
        routes=workbench,
        identity=IdentityState(service=IdentityResolutionService()))
    registry.facts.add("identity_resolved")

    result = registry.execute("retrosynthesize", {"target_smiles": DIYNE})
    assert result.status is ToolStatus.OK
    assert result.data["route_count"] > 0
    assert result.data["persisted"] is False
    assert result.data["stored_for_validation"] is True


def test_retrosynthesize_requires_the_identity_fact():
    registry = build_agent_tools(
        routes=RouteWorkbench(provider=FakeRetrosynthesisProvider(),
                              validator=RouteValidationService()))
    blocked = registry.execute("retrosynthesize", {"target_smiles": DIYNE})
    assert blocked.status is ToolStatus.REJECTED
    assert blocked.error_type == "PRECONDITION_NOT_MET"


# ---------------------------------------------------------------------------
# Execution guards
# ---------------------------------------------------------------------------


def test_identity_resolution_requires_approval_and_sets_a_fact_when_approved():
    registry = build_agent_tools()
    blocked = registry.execute("resolve_chemical_identity",
                               {"kind": "SMILES", "value": "CCO"})
    assert blocked.status is ToolStatus.REJECTED
    assert blocked.error_type == "APPROVAL_REQUIRED"
    assert "APPROVAL_REQUIRED" in " ".join(blocked.diagnostics)
    assert "identity_resolved" not in registry.facts

    ok = registry.execute("resolve_chemical_identity",
                          {"kind": "SMILES", "value": "CCO"}, approved=True)
    assert ok.status is ToolStatus.OK
    assert "identity_resolved" in registry.facts


def test_extract_reactions_is_blocked_without_approval():
    registry = build_agent_tools(
        policy=ExecutionPolicy(allow_model_calls=True))
    blocked = registry.execute("extract_reactions", {"document_id": "doc"})
    assert blocked.status is ToolStatus.REJECTED
    assert blocked.error_type == "APPROVAL_REQUIRED"


def test_unapproved_persistent_tool_never_runs_its_body():
    calls: list[str] = []

    def write(value):
        calls.append(value)
        return {"ok": True}

    registry = ToolRegistry(enforce_policy=True)
    registry.register(_spec("write", write,
                            effects=Effects.PERSISTENT.value))
    blocked = registry.execute("write", {"value": "x"})
    assert blocked.error_type == "APPROVAL_REQUIRED"
    assert calls == [], "the tool body must not run before approval"

    ok = registry.execute("write", {"value": "x"}, approved=True)
    assert ok.status is ToolStatus.OK
    assert calls == ["x"]


def test_network_access_is_blocked_by_policy_and_allowed_when_configured():
    calls: list[str] = []

    def fetch(value):
        calls.append(value)
        return {"ok": True}

    registry = ToolRegistry(enforce_policy=True)
    registry.register(_spec("fetch", fetch, access=(Access.NETWORK.value,)))
    blocked = registry.execute("fetch", {"value": "x"})
    assert blocked.status is ToolStatus.REJECTED
    assert blocked.error_type == "NETWORK_NOT_ALLOWED"
    assert calls == []

    registry.policy.allow_network = True
    assert registry.execute("fetch", {"value": "x"}).status is ToolStatus.OK
    assert calls == ["x"]


def test_model_access_is_blocked_by_policy():
    registry = ToolRegistry(enforce_policy=True)
    registry.register(_spec("ask", lambda value: {"ok": True},
                            access=(Access.MODEL.value,)))
    blocked = registry.execute("ask", {"value": "x"})
    assert blocked.error_type == "MODEL_NOT_ALLOWED"


def test_precondition_gate():
    registry = ToolRegistry(enforce_policy=True)
    registry.register(_spec("guarded", lambda value: {"ok": True},
                            requires=("identity_resolved",)))
    blocked = registry.execute("guarded", {"value": "x"})
    assert blocked.error_type == "PRECONDITION_NOT_MET"
    registry.facts.add("identity_resolved")
    assert registry.execute("guarded", {"value": "x"}).status is ToolStatus.OK


def test_output_limit_is_explicit_and_keeps_a_diagnostic():
    registry = ToolRegistry(enforce_policy=True)
    registry.register(_spec("big", lambda value: {"items": list(range(1000))},
                            output_limit_chars=200))
    result = registry.execute("big", {"value": "x"})
    assert result.status is ToolStatus.OK
    assert result.data["truncated"] is True
    assert result.data["original_chars"] > 200
    assert any("truncated" in d for d in result.diagnostics)


def test_tool_exception_is_a_structured_error_not_a_silent_failure():
    def boom(value):
        raise ValueError("backend said no")

    registry = ToolRegistry(enforce_policy=True)
    registry.register(_spec("boom", boom))
    result = registry.execute("boom", {"value": "x"})
    assert result.status is ToolStatus.ERROR
    assert result.error_type == "ValueError"
    assert "backend said no" in result.error
    assert result.diagnostics


# ---------------------------------------------------------------------------
# Timeouts: report the limit, never claim an interruption that did not happen
# ---------------------------------------------------------------------------


def test_advisory_timeout_is_reported_as_not_interrupted():
    def slow(value):
        time.sleep(0.05)
        return {"ok": True}

    registry = ToolRegistry(enforce_policy=True)
    registry.register(_spec("slow", slow, timeout_s=0.01,
                            timeout_mode="advisory"))
    result = registry.execute("slow", {"value": "x"})
    assert result.status is ToolStatus.OK
    assert any("not interrupted" in d for d in result.diagnostics)


def test_native_timeout_is_reported_as_a_budget_overrun():
    def slow(value):
        time.sleep(0.05)
        return {"ok": True}

    registry = ToolRegistry(enforce_policy=True)
    registry.register(_spec("slow", slow, timeout_s=0.01,
                            timeout_mode="native"))
    result = registry.execute("slow", {"value": "x"})
    assert result.status is ToolStatus.OK
    assert any("did not interrupt" in d for d in result.diagnostics)


def test_timeout_never_turns_into_a_fake_success_or_a_fake_failure():
    def quick(value):
        return {"ok": True}

    registry = ToolRegistry(enforce_policy=True)
    registry.register(_spec("quick", quick, timeout_s=5.0,
                            timeout_mode="advisory"))
    result = registry.execute("quick", {"value": "x"})
    assert result.status is ToolStatus.OK
    assert not any("timeout" in d for d in result.diagnostics)


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


def test_legacy_registry_keeps_its_old_non_enforcing_behavior():
    registry = ToolRegistry()  # enforce_policy defaults to False
    registry.register(_spec("write", lambda value: {"ok": True},
                            effects=Effects.PERSISTENT.value))
    result = registry.execute("write", {"value": "x"})
    assert result.status is ToolStatus.OK


def test_the_three_legacy_registries_still_build_and_run():
    research = build_registry(None, None, [])
    workspace = default_tool_registry()
    evidence = build_evidence_tool_registry(
        transformation_search=None, comparison=None, evidence=None,
        literature=None, route=None)
    assert len(research.names()) == 26
    assert len(workspace.names()) == 10
    assert len(evidence.names()) == 8
    ok = research.execute("analyze_molecule", {"smiles": "CCO"})
    assert ok.status is ToolStatus.OK


def test_old_alias_names_are_all_registered():
    registry = build_agent_tools()
    for alias in ALIASES:
        assert registry.get(alias) is not None, alias
        assert registry.get(alias).alias_of in registry.canonical_names


def test_no_canonical_name_is_an_alias():
    registry = build_agent_tools()
    assert not (set(registry.canonical_names) & set(registry.alias_of))

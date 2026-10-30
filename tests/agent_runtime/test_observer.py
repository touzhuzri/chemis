"""M1.2 -- the observer's six kinds, derived from machine signals.

Every classification here is deterministic: `ToolStatus`, a policy code, or a
structured `status`/`outcome` token a tool already emits. No model is involved
and no prose is keyword-matched. The tests use the real `ToolRegistry` for the
policy and exception paths, so the codes under test are the ones the execution
guard actually produces.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

from crea.agent.observer import (
    Observation,
    ObservationKind,
    observe,
    observe_from_registry,
)
from crea.agent.tools import Effects, ToolRegistry, ToolSpec
from crea.core.models.research import ToolResult, ToolStatus


class _Args(BaseModel):
    value: str = Field(default="x")


def _spec(name: str = "probe", run=None, **kwargs) -> ToolSpec:
    return ToolSpec(name=name, description=name, arguments_model=_Args,
                    run=run or (lambda **_: {"ok": True}), **kwargs)


def _result(*, tool: str = "probe", status: ToolStatus = ToolStatus.OK,
            data=None, **kwargs) -> ToolResult:
    return ToolResult(tool=tool, status=status, data=data, **kwargs)


def _observe(result: ToolResult, spec: ToolSpec | None = None,
             facts=None) -> Observation:
    return observe(result, spec=spec, facts=facts)


# ---------------------------------------------------------------------------
# The six kinds
# ---------------------------------------------------------------------------


def test_the_observer_has_exactly_one_vocabulary_of_six_kinds():
    assert {kind.value for kind in ObservationKind} == {
        "success", "no_result", "needs_input", "unavailable", "partial",
        "error"}


def test_an_ambiguous_identity_needs_a_user_choice():
    observation = _observe(_result(
        tool="resolve_chemical_identity",
        data={"status": "ambiguous", "candidates": [{"name": "a"},
                                                    {"name": "b"}],
              "chemistry_allowed": False}))
    assert observation.kind is ObservationKind.NEEDS_INPUT
    assert observation.missing_requirements == ["user_selection"]
    assert observation.data["candidates"]


def test_ok_with_valid_data_is_success():
    observation = _observe(_result(data={"canonical_smiles": "CCO",
                                         "molecular_formula": "C2H6O"}))
    assert observation.kind is ObservationKind.SUCCESS
    assert observation.succeeded is True
    assert observation.data["molecular_formula"] == "C2H6O"
    assert observation.needs_user_input is False


def test_ok_with_an_empty_result_list_is_no_result_not_success():
    observation = _observe(_result(data={"status": "ok", "count": 0,
                                         "reactions": []}))
    assert observation.kind is ObservationKind.NO_RESULT
    assert observation.error_code == "no_results"
    assert observation.data["count"] == 0


def test_ok_with_no_results_outcome_is_no_result():
    observation = _observe(_result(data={"outcome": "no_results",
                                         "paper_count": 0, "papers": []}))
    assert observation.kind is ObservationKind.NO_RESULT


def test_ok_with_provider_unavailable_is_unavailable_not_success():
    observation = _observe(_result(data={"status": "provider_unavailable",
                                         "error": "no credential"}))
    assert observation.kind is ObservationKind.UNAVAILABLE
    assert observation.error_code == "provider_unavailable"
    assert "no credential" in (observation.error or "")


def test_a_missing_database_is_unavailable_not_an_empty_answer():
    observation = _observe(_result(
        tool="get_papers_for_entity",
        data={"status": "no_knowledge_base",
              "error": "no persistent knowledge base is wired"}))
    assert observation.kind is ObservationKind.UNAVAILABLE
    assert observation.kind is not ObservationKind.NO_RESULT


def test_all_providers_failed_is_unavailable():
    observation = _observe(_result(data={"outcome": "all_providers_failed",
                                         "paper_count": 0, "papers": []}))
    assert observation.kind is ObservationKind.UNAVAILABLE


def test_no_result_and_unavailable_are_strictly_distinct():
    miss = _observe(_result(data={"status": "ok", "count": 0, "papers": []}))
    outage = _observe(_result(data={"status": "unavailable",
                                    "reason": "service down"}))
    assert miss.kind is ObservationKind.NO_RESULT
    assert outage.kind is ObservationKind.UNAVAILABLE
    assert miss.kind is not outage.kind


# ---------------------------------------------------------------------------
# Policy rejections are needs-input, not errors
# ---------------------------------------------------------------------------


def test_approval_required_is_needs_input():
    registry = ToolRegistry(enforce_policy=True)
    spec = registry.register(_spec(effects=Effects.PERSISTENT.value))
    result = registry.execute("probe", {})
    assert result.status is ToolStatus.REJECTED

    observation = _observe(result, spec=spec, facts=registry.facts)
    assert observation.kind is ObservationKind.NEEDS_INPUT
    assert observation.error_code == "APPROVAL_REQUIRED"
    assert observation.missing_requirements == ["approval"]
    assert observation.needs_user_input is True
    assert observation.retryable is False
    assert observation.raw_status == "rejected"


def test_precondition_not_met_names_the_missing_fact():
    registry = ToolRegistry(enforce_policy=True)
    spec = registry.register(_spec(requires=("identity_resolved",)))
    observation = _observe(registry.execute("probe", {}),
                           spec=spec, facts=registry.facts)
    assert observation.kind is ObservationKind.NEEDS_INPUT
    assert observation.error_code == "PRECONDITION_NOT_MET"
    assert observation.missing_requirements == ["identity_resolved"]


def test_network_policy_rejection_is_needs_input():
    registry = ToolRegistry(enforce_policy=True)
    spec = registry.register(_spec(access=("network",)))
    observation = _observe(registry.execute("probe", {}),
                           spec=spec, facts=registry.facts)
    assert observation.kind is ObservationKind.NEEDS_INPUT
    assert observation.missing_requirements == ["network_access"]


def test_model_policy_rejection_is_needs_input():
    registry = ToolRegistry(enforce_policy=True)
    spec = registry.register(_spec(access=("model",)))
    observation = _observe(registry.execute("probe", {}),
                           spec=spec, facts=registry.facts)
    assert observation.kind is ObservationKind.NEEDS_INPUT
    assert observation.missing_requirements == ["model_access"]


def test_invalid_arguments_are_needs_input():
    registry = ToolRegistry(enforce_policy=True)
    spec = _spec()
    registry.register(spec)
    observation = _observe(registry.execute("probe", {"value": 123}),
                           spec=spec, facts=registry.facts)
    assert observation.kind is ObservationKind.NEEDS_INPUT
    assert observation.error_code == "InvalidArguments"
    assert observation.missing_requirements == ["valid_arguments"]


# ---------------------------------------------------------------------------
# Partial results
# ---------------------------------------------------------------------------


def test_a_compare_with_one_failed_side_is_partial():
    observation = _observe(_result(
        tool="compare_structures",
        data={"verdict": "different", "common_atoms": 3,
              "partial": True,
              "limitations": ["workspace: ValueError: backend said no"]}))
    assert observation.kind is ObservationKind.PARTIAL
    # the half that did succeed is not discarded
    assert observation.data["verdict"] == "different"
    assert "workspace" in observation.summary


def test_partial_success_outcome_is_partial():
    observation = _observe(_result(data={
        "outcome": "partial_success", "paper_count": 1,
        "papers": [{"paper_id": "P-1"}], "warnings": ["one provider failed"]}))
    assert observation.kind is ObservationKind.PARTIAL
    assert observation.data["papers"][0]["paper_id"] == "P-1"


def test_a_step_status_is_not_a_tool_outcome():
    # validate_route_step reports how supported *the step* is. `candidate`
    # must not be read as "the call needs input".
    observation = _observe(_result(tool="validate_route_step",
                                   data={"status": "candidate",
                                         "support_level": "no_support",
                                         "warnings": []}))
    assert observation.kind is ObservationKind.SUCCESS


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_an_exception_is_an_error_with_the_exception_type():
    def boom(**_: object):
        raise ValueError("backend said no")

    registry = ToolRegistry(enforce_policy=True)
    spec = registry.register(_spec(run=boom))
    observation = _observe(registry.execute("probe", {}), spec=spec,
                           facts=registry.facts)
    assert observation.kind is ObservationKind.ERROR
    assert observation.error_type == "ValueError"
    assert "backend said no" in (observation.error or "")
    assert observation.needs_user_input is False
    assert observation.retryable is True


def test_an_unclassifiable_rejection_is_an_error_not_success():
    observation = _observe(_result(status=ToolStatus.REJECTED,
                                   error_type="SOMETHING_NEW"))
    assert observation.kind is ObservationKind.ERROR
    assert observation.kind is not ObservationKind.SUCCESS
    assert observation.error_code == "SOMETHING_NEW"


def test_an_unrecognized_explicit_status_is_not_success():
    observation = _observe(_result(data={"status": "quantum", "count": 1}))
    assert observation.kind is ObservationKind.ERROR
    assert observation.error_code == "quantum"


# ---------------------------------------------------------------------------
# Evidence, diagnostics, provenance, truncation, retryability
# ---------------------------------------------------------------------------


def test_truncation_is_preserved_and_is_not_a_failure():
    observation = _observe(_result(data={
        "items": [1, 2, 3], "truncated": True, "original_chars": 9000}))
    assert observation.kind is ObservationKind.SUCCESS
    assert observation.truncated is True
    assert observation.original_chars == 9000


def test_evidence_ids_provenance_and_diagnostics_are_copied_verbatim():
    result = _result(
        data={"paper_id": "P-1"}, evidence_ids=["EV-1", "EV-2"],
        provenance={"declared_access": ["local"], "delegated_tool": "x"},
        diagnostics=["a warning"],
    )
    observation = _observe(result)
    assert observation.evidence_ids == ["EV-1", "EV-2"]
    assert observation.provenance == {"declared_access": ["local"],
                                      "delegated_tool": "x"}
    assert observation.diagnostics == ["a warning"]
    assert observation.data == {"paper_id": "P-1"}
    # the raw result is retained, not reconstructed
    assert observation.raw is not None
    assert observation.raw.evidence_ids == ["EV-1", "EV-2"]


def test_an_advisory_timeout_stays_a_success_with_its_diagnostic():
    def slow(**_: object):
        time.sleep(0.05)
        return {"ok": True}

    registry = ToolRegistry(enforce_policy=True)
    spec = registry.register(_spec(run=slow, timeout_s=0.01,
                                   timeout_mode="advisory"))
    observation = _observe(registry.execute("probe", {}), spec=spec,
                           facts=registry.facts)
    assert observation.kind is ObservationKind.SUCCESS
    assert any("not interrupted" in d for d in observation.diagnostics)
    assert "TIMEOUT" not in (observation.error_code or "")


def test_retryability_comes_from_the_declared_spec():
    registry = ToolRegistry(enforce_policy=True)
    spec = registry.register(_spec(run=lambda **_: ToolResult(
        tool="probe", status=ToolStatus.UNAVAILABLE,
        error="provider down"), retryable=False))
    observation = _observe(registry.execute("probe", {}), spec=spec,
                           facts=registry.facts)
    assert observation.kind is ObservationKind.UNAVAILABLE
    assert observation.retryable is False


def test_a_plain_miss_is_not_retryable():
    observation = _observe(_result(data={"status": "ok", "count": 0,
                                         "papers": []}))
    assert observation.kind is ObservationKind.NO_RESULT
    assert observation.retryable is False


# ---------------------------------------------------------------------------
# Registry integration and purity
# ---------------------------------------------------------------------------


def test_observe_from_registry_uses_the_tool_spec_without_executing_it():
    calls: list[str] = []

    def run(**_: object):
        calls.append("ran")
        return {"ok": True}

    registry = ToolRegistry(enforce_policy=True)
    registry.register(_spec(run=run))
    result = _result()
    observation = observe_from_registry(registry, "probe", result)
    assert observation.kind is ObservationKind.SUCCESS
    assert calls == [], "the observer must not run the tool again"


def test_an_alias_reports_both_the_called_and_canonical_names():
    registry = ToolRegistry(enforce_policy=True)
    registry.register(_spec("canonical"))
    registry.register(_spec("old_name", alias_of="canonical", canonical=False))
    result = registry.execute("old_name", {})
    observation = observe_from_registry(registry, "old_name", result)
    assert observation.tool == "old_name"
    assert observation.canonical_tool == "canonical"


def test_an_unknown_tool_is_an_error():
    registry = ToolRegistry(enforce_policy=True)
    observation = observe_from_registry(registry, "nope",
                                        registry.execute("nope", {}))
    assert observation.kind is ObservationKind.ERROR
    assert observation.error_type == "UnknownTool"
    assert "no tool named" in (observation.error or "")


def test_the_model_facing_context_keeps_the_payload():
    observation = _observe(_result(
        data={"doi": "10.1000/x", "reaction_id": "R-1"},
        evidence_ids=["EV-1"], diagnostics=["careful"],
        step_id="s1", duration_ms=12))
    context = observation.to_context()
    assert context["kind"] == "success"
    assert context["raw_status"] == "ok"
    assert context["data"] == {"doi": "10.1000/x", "reaction_id": "R-1"}
    assert context["evidence_ids"] == ["EV-1"]
    assert context["diagnostics"] == ["careful"]
    assert context["step_id"] == "s1"
    assert context["duration_ms"] == 12

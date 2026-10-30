"""M1.3 -- the planner: deterministic plans, constrained model help, and
decisions driven by M1.2 observations.

Nothing here executes a tool. The registry supplies declared metadata; the
scripted LLM only ever returns plan JSON. Decisions are checked against the
policy the registry would enforce anyway -- not re-implemented here.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from crea.agent.observer import Observation, ObservationKind
from crea.agent.planner import (
    MAX_PLAN_DECISIONS,
    MAX_PLAN_REVISIONS,
    MAX_PLAN_STEPS,
    PlanChangeKind,
    PlanDecisionKind,
    Planner,
    PlanSession,
)
from crea.agent.tool_surface import build_agent_tools
from crea.agent.tools import ExecutionPolicy, ToolRegistry, ToolSpec
from crea.core.models.knowledge import (
    ResearchPlan,
    ResearchPlanAction,
    ResearchPlanStep,
)
from crea.llm.fake import FakeLLMProvider, TextScriptedLLMProvider


class _Args(BaseModel):
    value: str = Field(default="x")


@pytest.fixture(scope="module")
def registry():
    # Literature is network; the M1.1 registry's default policy forbids it.
    # Tests that plan a search use a registry that allows the call it plans.
    return build_agent_tools(policy=ExecutionPolicy(allow_network=True))


def _obs(kind: ObservationKind, tool: str = "search_literature",
         **kwargs) -> Observation:
    kwargs.setdefault("raw_status", "ok")
    return Observation(kind=kind, tool=tool, **kwargs)


def _plan(*steps: ResearchPlanStep, goal: str = "goal") -> ResearchPlan:
    return ResearchPlan(goal=goal, steps=list(steps))


def _step(step_id: str, tool: str, action: ResearchPlanAction,
          arguments: dict | None = None, **kwargs) -> ResearchPlanStep:
    return ResearchPlanStep(step_id=step_id, tool=tool, action=action,
                            arguments=arguments or {}, **kwargs)


def _model_plan(*steps: dict) -> str:
    return json.dumps({"steps": list(steps)})


# ---------------------------------------------------------------------------
# Plan generation
# ---------------------------------------------------------------------------


def test_a_simple_goal_gets_a_deterministic_plan_without_a_model(registry):
    llm = FakeLLMProvider([])
    planner = Planner(registry, llm)
    proposal = planner.initial_plan("研究 1,4-diphenyl-1,3-diyne")

    assert proposal.source == "deterministic"
    assert llm.calls == [], "a simple goal must not cost a model call"
    assert [s.step_id for s in proposal.plan.steps] == [
        "assess-knowledge", "search-literature", "report"]
    assert planner.validate_plan(proposal.plan) == []


def test_a_complex_goal_can_use_one_constrained_model_call(registry):
    payload = _model_plan(
        {"step_id": "s1", "action": "assess_knowledge",
         "tool": "get_existing_knowledge", "reason": "read what is known"},
        {"step_id": "s2", "action": "search_literature",
         "tool": "search_literature", "arguments": {"query": "x"},
         "depends_on": ["s1"], "reason": "find papers"},
        {"step_id": "s3", "action": "report", "reason": "summarise"},
    )
    llm = TextScriptedLLMProvider([payload])
    planner = Planner(registry, llm)
    proposal = planner.initial_plan("Compare A and B then search literature",
                                    use_llm=True)

    assert proposal.source == "model"
    assert [s.step_id for s in proposal.plan.steps] == ["s1", "s2", "s3"]
    assert len(llm.calls) == 1


def test_a_model_tool_that_is_not_registered_is_rejected(registry):
    payload = _model_plan(
        {"step_id": "s1", "action": "search_literature",
         "tool": "make_gold", "arguments": {}})
    planner = Planner(registry, TextScriptedLLMProvider([payload]))
    proposal = planner.initial_plan("complex goal", use_llm=True)

    assert proposal.source == "model_rejected"
    assert any("unregistered tool" in w for w in proposal.warnings)
    # the deterministic plan is used instead, never the invalid one
    assert all(s.tool != "make_gold" for s in proposal.plan.steps)
    assert planner.validate_plan(proposal.plan) == []


def test_model_arguments_outside_the_schema_are_rejected(registry):
    payload = _model_plan(
        {"step_id": "s1", "action": "analyze_structure",
         "tool": "analyze_molecule", "arguments": {"smiles": "CCO",
                                                    "invented": 1}})
    planner = Planner(registry, TextScriptedLLMProvider([payload]))
    proposal = planner.initial_plan("complex", use_llm=True)

    assert proposal.source == "model_rejected"
    assert any("unknown argument" in w for w in proposal.warnings)


def test_a_dependency_on_a_missing_step_is_rejected(registry):
    payload = _model_plan(
        {"step_id": "s1", "action": "report", "depends_on": ["ghost"]})
    planner = Planner(registry, TextScriptedLLMProvider([payload]))
    proposal = planner.initial_plan("complex", use_llm=True)

    assert proposal.source == "model_rejected"
    assert any("does not exist" in w for w in proposal.warnings)


def test_a_dependency_cycle_is_rejected(registry):
    payload = _model_plan(
        {"step_id": "s1", "action": "assess_knowledge",
         "tool": "get_existing_knowledge", "depends_on": ["s2"]},
        {"step_id": "s2", "action": "search_literature",
         "tool": "search_literature", "arguments": {"query": "x"},
         "depends_on": ["s1"]})
    planner = Planner(registry, TextScriptedLLMProvider([payload]))
    proposal = planner.initial_plan("complex", use_llm=True)

    assert proposal.source == "model_rejected"
    assert any("cycle" in w for w in proposal.warnings)


def test_a_plan_over_the_step_budget_is_rejected(registry):
    payload = _model_plan(
        {"step_id": "s1", "action": "assess_knowledge",
         "tool": "get_existing_knowledge"},
        {"step_id": "s2", "action": "search_literature",
         "tool": "search_literature", "arguments": {"query": "x"}},
        {"step_id": "s3", "action": "report"})
    planner = Planner(registry, TextScriptedLLMProvider([payload]),
                      max_steps=2)
    proposal = planner.initial_plan("complex", use_llm=True)

    assert proposal.source == "model_rejected"
    assert any("maximum" in w for w in proposal.warnings)
    assert len(proposal.plan.steps) <= 2


def test_default_budgets_are_conservative():
    assert MAX_PLAN_STEPS == 8
    assert MAX_PLAN_REVISIONS == 3
    assert MAX_PLAN_DECISIONS == 16


# ---------------------------------------------------------------------------
# SUCCESS / COMPLETE
# ---------------------------------------------------------------------------


def _three_step_plan() -> ResearchPlan:
    return _plan(
        _step("a", "get_existing_knowledge", ResearchPlanAction.ASSESS_KNOWLEDGE),
        _step("b", "search_literature", ResearchPlanAction.SEARCH_LITERATURE,
              {"query": "x"}, depends_on=["a"]),
        ResearchPlanStep(step_id="r", action=ResearchPlanAction.REPORT),
    )


def test_success_continues_to_the_next_runnable_step(registry):
    planner = Planner(registry)
    session = PlanSession(plan=_three_step_plan())

    start = planner.start(session)
    assert start.kind is PlanDecisionKind.CONTINUE
    assert start.next_step_id == "a"

    after = planner.decide(session, "a",
                           _obs(ObservationKind.SUCCESS, "get_existing_knowledge"))
    assert after.kind is PlanDecisionKind.CONTINUE
    assert after.next_step_id == "b"
    assert after.executed_step_id == "a"


def test_success_on_the_report_step_completes_the_plan(registry):
    planner = Planner(registry)
    session = PlanSession(plan=_plan(
        ResearchPlanStep(step_id="r", action=ResearchPlanAction.REPORT)))
    decision = planner.decide(
        session, "r", _obs(ObservationKind.SUCCESS, "report"))
    assert decision.kind is PlanDecisionKind.COMPLETE
    assert decision.partial is False


# ---------------------------------------------------------------------------
# NO_RESULT
# ---------------------------------------------------------------------------


def test_no_result_is_finished_and_the_same_query_is_never_repeated(registry):
    planner = Planner(registry)
    session = PlanSession(plan=_plan(
        _step("s", "search_reactions", ResearchPlanAction.SEARCH_LITERATURE,
              {"reactant_entity_id": "CHEM-1"}),
        ResearchPlanStep(step_id="r", action=ResearchPlanAction.REPORT),
    ))
    planner.start(session)
    decision = planner.decide(
        session, "s",
        _obs(ObservationKind.NO_RESULT, "search_reactions"))

    assert decision.kind is PlanDecisionKind.CONTINUE
    assert decision.next_step_id == "r"
    assert decision.changes == []
    assert session.executed_calls, "the call signature was recorded"
    # the miss is described as a result, not as an outage or a failure
    assert "无命中" in decision.reason
    assert "UNAVAILABLE" in decision.reason or "不可用" in decision.reason


def test_no_result_may_adjust_the_query_once_using_a_declared_fallback(registry):
    planner = Planner(registry)
    session = PlanSession(plan=_plan(
        _step("s", "search_reactions", ResearchPlanAction.SEARCH_LITERATURE,
              {"reactant_entity_id": "CHEM-1"},
              fallback_tool="search_reactions",
              fallback_arguments={"product_entity_id": "CHEM-1"}),
        ResearchPlanStep(step_id="r", action=ResearchPlanAction.REPORT),
    ))
    planner.start(session)
    decision = planner.decide(
        session, "s", _obs(ObservationKind.NO_RESULT, "search_reactions"))

    assert decision.kind is PlanDecisionKind.CONTINUE
    assert decision.next_step_id == "s-alt"
    assert [c.kind for c in decision.changes] == [PlanChangeKind.ADD_STEP]
    added = decision.changes[0].after
    assert added["tool"] == "search_reactions"
    assert added["arguments"] == {"product_entity_id": "CHEM-1"}
    assert added["arguments"] != {"reactant_entity_id": "CHEM-1"}


# ---------------------------------------------------------------------------
# NEEDS_INPUT
# ---------------------------------------------------------------------------


def test_approval_requires_the_user_and_never_runs_the_step(registry):
    planner = Planner(registry)
    session = PlanSession(plan=_plan(
        _step("i", "resolve_chemical_identity",
              ResearchPlanAction.RESOLVE_IDENTITY,
              {"kind": "SMILES", "value": "CCO"})))
    decision = planner.decide(
        session, "i",
        _obs(ObservationKind.NEEDS_INPUT, "resolve_chemical_identity",
             error_code="APPROVAL_REQUIRED", missing_requirements=["approval"]))

    assert decision.kind is PlanDecisionKind.ASK_USER
    assert decision.requires_approval is True
    assert decision.missing_requirements == ["approval"]
    assert decision.changes == []


def test_a_missing_identity_is_planned_before_asking_the_user(registry):
    planner = Planner(registry)
    session = PlanSession(plan=_plan(
        _step("s", "search_literature", ResearchPlanAction.SEARCH_LITERATURE,
              {"query": "x"})))
    decision = planner.decide(
        session, "s",
        _obs(ObservationKind.NEEDS_INPUT, "search_literature",
             error_code="PRECONDITION_NOT_MET",
             missing_requirements=["resolved_identity"]))

    assert [c.kind for c in decision.changes] == [PlanChangeKind.ADD_STEP]
    added = decision.changes[0].after["tool"]
    assert added == "resolve_chemical_identity"
    assert decision.next_step_id == decision.changes[0].step_id
    # the added step itself needs approval; the planner does not skip it
    assert decision.requires_approval is True
    assert any(s.tool == "resolve_chemical_identity"
               for s in session.plan.steps)


def test_valid_arguments_are_completed_from_session_context(registry):
    planner = Planner(registry)
    session = PlanSession(
        plan=_plan(_step("a", "analyze_molecule",
                         ResearchPlanAction.ANALYZE_STRUCTURE)),
        context={"smiles": "CCO"})
    decision = planner.start(session)

    assert decision.kind is PlanDecisionKind.CONTINUE
    assert decision.next_step_id == "a"
    assert [c.kind for c in decision.changes] == [
        PlanChangeKind.UPDATE_ARGUMENTS]
    assert session.plan.steps[0].arguments == {"smiles": "CCO"}


def test_arguments_that_cannot_be_filled_are_asked_for(registry):
    planner = Planner(registry)
    session = PlanSession(plan=_plan(
        _step("a", "analyze_molecule", ResearchPlanAction.ANALYZE_STRUCTURE)))
    decision = planner.start(session)

    assert decision.kind is PlanDecisionKind.ASK_USER
    assert decision.needs_user_input is True
    assert "smiles" in decision.missing_requirements
    assert session.plan.steps[0].arguments == {}


# ---------------------------------------------------------------------------
# UNAVAILABLE / ERROR / PARTIAL
# ---------------------------------------------------------------------------


def test_unavailable_tries_a_declared_alternative_source(registry):
    planner = Planner(registry)
    session = PlanSession(plan=_plan(
        _step("s", "search_reactions", ResearchPlanAction.SEARCH_LITERATURE,
              {"reactant_entity_id": "CHEM-1"},
              fallback_tool="get_existing_knowledge",
              fallback_arguments={"name": "X"})))
    planner.start(session)
    decision = planner.decide(
        session, "s", _obs(ObservationKind.UNAVAILABLE, "search_reactions",
                           error_code="no_knowledge_base"))

    assert decision.kind is PlanDecisionKind.CONTINUE
    assert decision.next_step_id == "s-alt"
    assert decision.changes[0].after["tool"] == "get_existing_knowledge"
    assert "不可用" in decision.reason


def test_an_error_is_never_reported_as_no_result(registry):
    planner = Planner(registry)
    session = PlanSession(plan=_plan(
        _step("a", "analyze_molecule", ResearchPlanAction.ANALYZE_STRUCTURE,
              {"smiles": "CCO"})))
    decision = planner.decide(
        session, "a", _obs(ObservationKind.ERROR, "analyze_molecule",
                           error_type="ValueError", error="boom"))

    assert decision.kind is PlanDecisionKind.BLOCKED
    assert decision.kind is not PlanDecisionKind.COMPLETE
    assert "未成功" in decision.reason
    assert decision.next_step_id == ""


def test_a_partial_result_is_kept_and_its_missing_branch_recorded(registry):
    planner = Planner(registry)
    session = PlanSession(plan=_plan(
        _step("s", "search_reactions", ResearchPlanAction.SEARCH_LITERATURE,
              {"reactant_entity_id": "CHEM-1"}),
        ResearchPlanStep(step_id="r", action=ResearchPlanAction.REPORT),
    ))
    planner.start(session)
    observation = _obs(ObservationKind.PARTIAL, "search_reactions",
                       data={"count": 1, "results": [{"x": 1}],
                             "limitations": ["workspace side failed"]})
    decision = planner.decide(session, "s", observation)

    assert decision.kind is PlanDecisionKind.CONTINUE
    assert decision.next_step_id == "r"
    assert session.observations["s"] is observation
    assert any("workspace side failed" in note for note in session.notes)

    final = planner.decide(session, "r", _obs(ObservationKind.SUCCESS, "report"))
    assert final.kind is PlanDecisionKind.COMPLETE
    assert final.partial is True


# ---------------------------------------------------------------------------
# Plan constraints and tool metadata
# ---------------------------------------------------------------------------


def test_an_unregistered_tool_is_not_planned(registry):
    planner = Planner(registry)
    issues = planner.validate_plan(_plan(
        _step("x", "make_gold", ResearchPlanAction.SEARCH_LITERATURE)))
    assert any("unregistered tool" in issue for issue in issues)


def test_a_precondition_is_never_bypassed(registry):
    planner = Planner(registry)
    session = PlanSession(plan=_plan(
        _step("r", "retrosynthesize", ResearchPlanAction.RETROSYNTHESIZE,
              {"target_smiles": "CCO"})))
    decision = planner.start(session)

    # not the guarded step; the planner added its prerequisite first
    assert decision.next_step_id != "r"
    assert decision.changes
    assert decision.changes[0].after["tool"] == "resolve_chemical_identity"
    assert any(s.tool == "resolve_chemical_identity"
               for s in session.plan.steps)


def test_a_persistent_effect_step_needs_recording_approval(registry):
    planner = Planner(registry)
    session = PlanSession(plan=_plan(
        _step("i", "resolve_chemical_identity",
              ResearchPlanAction.RESOLVE_IDENTITY,
              {"kind": "SMILES", "value": "CCO"})))

    blocked = planner.start(session)
    assert blocked.kind is PlanDecisionKind.ASK_USER
    assert blocked.requires_approval is True

    session.approve("i")
    allowed = planner.decide(session)
    assert allowed.kind is PlanDecisionKind.CONTINUE
    assert allowed.next_step_id == "i"


def test_retryable_metadata_does_not_trigger_an_automatic_retry(registry):
    planner = Planner(registry)
    session = PlanSession(plan=_plan(
        _step("a", "analyze_molecule", ResearchPlanAction.ANALYZE_STRUCTURE,
              {"smiles": "CCO"})))
    decision = planner.decide(
        session, "a", _obs(ObservationKind.ERROR, "analyze_molecule"))

    assert decision.kind is PlanDecisionKind.BLOCKED
    assert len(session.executed_calls) == 1
    assert decision.next_step_id == ""
    assert len(session.plan.steps) == 1


def test_the_revision_budget_stops_further_plan_edits(registry):
    planner = Planner(registry, max_revisions=0)
    session = PlanSession(plan=_plan(
        _step("s", "search_reactions", ResearchPlanAction.SEARCH_LITERATURE,
              {"reactant_entity_id": "CHEM-1"},
              fallback_tool="search_reactions",
              fallback_arguments={"product_entity_id": "CHEM-1"})))
    decision = planner.decide(
        session, "s", _obs(ObservationKind.NO_RESULT, "search_reactions"))

    assert decision.kind is PlanDecisionKind.BLOCKED
    assert "revision budget" in decision.reason
    assert len(session.plan.steps) == 1


def test_the_decision_budget_stops_the_loop(registry):
    planner = Planner(registry, max_decisions=1)
    session = PlanSession(plan=_three_step_plan())
    assert planner.start(session).kind is PlanDecisionKind.CONTINUE
    blocked = planner.decide(session)
    assert blocked.kind is PlanDecisionKind.BLOCKED
    assert "decision budget" in blocked.reason


def test_the_same_state_and_observation_produce_the_same_decision(registry):
    def run() -> dict:
        planner = Planner(registry)
        session = PlanSession(plan=_three_step_plan())
        planner.start(session)
        return planner.decide(
            session, "a",
            _obs(ObservationKind.SUCCESS, "get_existing_knowledge")
        ).model_dump(mode="json")

    assert run() == run()


def test_adopting_an_old_plan_assigns_ids_and_canonical_tools(registry):
    planner = Planner(registry)
    adopted, warnings = planner.adopt_plan(_plan(
        ResearchPlanStep(action=ResearchPlanAction.ANALYZE_STRUCTURE,
                         tool="analyze_structure", arguments={"smiles": "CCO"})))
    assert adopted.steps[0].step_id == "step-1"
    assert adopted.steps[0].tool == "analyze_molecule"
    assert any("canonicalized" in w for w in warnings)


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


def test_the_planner_never_executes_a_tool():
    calls: list[str] = []

    def boom(**_: object):
        calls.append("ran")
        raise AssertionError("the planner executed a tool")

    registry = ToolRegistry(enforce_policy=True)
    registry.register(ToolSpec(name="probe", description="probe",
                               arguments_model=_Args, run=boom))
    planner = Planner(registry)
    session = PlanSession(plan=_plan(
        _step("p", "probe", ResearchPlanAction.SEARCH_LITERATURE)))
    planner.start(session)
    planner.decide(session, "p", _obs(ObservationKind.SUCCESS, "probe"))
    assert calls == []


def test_the_planner_trusts_the_observer_kind_instead_of_reclassifying():
    """A SUCCESS observation with empty-looking data stays SUCCESS here.

    Classification belongs to M1.2; the planner must not invent a second
    reading from the payload.
    """
    registry = build_agent_tools()
    planner = Planner(registry)
    session = PlanSession(plan=_plan(
        _step("s", "search_reactions", ResearchPlanAction.SEARCH_LITERATURE,
              {"reactant_entity_id": "CHEM-1"}),
        ResearchPlanStep(step_id="r", action=ResearchPlanAction.REPORT)))
    planner.start(session)
    decision = planner.decide(
        session, "s",
        _obs(ObservationKind.SUCCESS, "search_reactions",
             data={"count": 0, "reactions": []}))
    assert session.outcomes["s"] is ObservationKind.SUCCESS
    assert decision.kind is PlanDecisionKind.CONTINUE


def test_an_ordinary_decision_costs_no_model_call(registry):
    llm = FakeLLMProvider([])
    planner = Planner(registry, llm)
    session = PlanSession(plan=_three_step_plan())
    planner.start(session)
    planner.decide(session, "a",
                   _obs(ObservationKind.SUCCESS, "get_existing_knowledge"))
    assert llm.calls == [], "the planner must not call a model per tool result"


def test_the_planner_does_not_run_a_network_tool_the_policy_forbids():
    registry = build_agent_tools()  # allow_network=False
    planner = Planner(registry)
    session = PlanSession(plan=_plan(
        _step("s", "search_literature", ResearchPlanAction.SEARCH_LITERATURE,
              {"query": "x"})))
    decision = planner.start(session)
    assert decision.kind is PlanDecisionKind.BLOCKED
    assert "network_access" in decision.missing_requirements

"""M2.0 -- the unified research decision protocol.

What is pinned here: a decision is a closed sum (one kind, one payload,
nothing smuggled in beside it); the six moves are the whole vocabulary; a
reply that is not a decision is not executed; evidence and hypothesis ids
are references that the validator checks, never values the model mints;
and the audit record carries the observability fields the M2.0 brief
requires -- without the model's private reasoning.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from crea.agent.intelligence_prompt import parse_decision
from crea.core.models.research_decision import (
    RESEARCH_DECISION_SCHEMA_VERSION,
    AskUserRequest,
    BlockedStatement,
    CompletionStatement,
    DecisionKind,
    DecisionOutcome,
    DecisionRecord,
    DirectionUpdate,
    EvidenceSufficiency,
    HypothesisUpdate,
    ResearchDecision,
    ScientificInterpretation,
    ToolCallRequest,
)


def _tool(**overrides) -> dict:
    payload = {
        "tool": "search_literature",
        "arguments": {"query": "aspirin"},
        "decision_summary": "检索路线",
        "purpose": "确定已有报道",
        "expected_information": "候选论文",
    }
    payload.update(overrides)
    return payload


def _hypothesis(**overrides) -> dict:
    payload = {
        "hypothesis_id": "",
        "hypothesis": "该路线采用绿色溶剂",
        "rationale_summary": "检索结果提到离子液体",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# The six moves
# ---------------------------------------------------------------------------


def test_the_protocol_has_exactly_six_moves():
    assert {kind.value for kind in DecisionKind} == {
        "continue_with_tool", "ask_user", "update_hypothesis",
        "update_direction", "complete", "blocked",
    }


@pytest.mark.parametrize("kind,payload_field,payload", [
    (DecisionKind.CONTINUE_WITH_TOOL, "tool_call", ToolCallRequest(
        **_tool())),
    (DecisionKind.ASK_USER, "ask_user", AskUserRequest(
        question="请确认目标结构", why="名称有歧义")),
    (DecisionKind.UPDATE_HYPOTHESIS, "hypothesis_update", HypothesisUpdate(
        **_hypothesis())),
    (DecisionKind.UPDATE_DIRECTION, "direction_update", DirectionUpdate(
        previous_direction="检索路线", new_direction="先解决身份",
        reason="名称歧义")),
    (DecisionKind.COMPLETE, "completion", CompletionStatement(
        completion_reason="证据已足够")),
    (DecisionKind.BLOCKED, "blocked", BlockedStatement(
        reason="没有工具能解决身份歧义")),
])
def test_each_move_carries_exactly_its_own_payload(kind, payload_field,
                                                   payload):
    decision = ResearchDecision(kind=kind, **{payload_field: payload})
    assert getattr(decision, payload_field) is payload
    dumped = decision.model_dump()
    assert sum(1 for name in (
        "tool_call", "hypothesis_update", "direction_update", "ask_user",
        "completion", "blocked") if dumped[name] is not None) == 1


def test_a_decision_needs_the_payload_its_kind_names():
    with pytest.raises(ValidationError) as excinfo:
        ResearchDecision(kind=DecisionKind.CONTINUE_WITH_TOOL)
    assert "payload" in str(excinfo.value)


def test_a_decision_refuses_a_second_payload():
    """A decision is one move. A tool call *and* a completion in the same
    reply would let a model act and then claim it had already finished."""
    with pytest.raises(ValidationError) as excinfo:
        ResearchDecision(
            kind=DecisionKind.CONTINUE_WITH_TOOL,
            tool_call=ToolCallRequest(**_tool()),
            completion=CompletionStatement(completion_reason="已经完成了"))
    assert "one move" in str(excinfo.value)


@pytest.mark.parametrize("field,value", [
    ("code", "os.system('rm -rf /')"),
    ("exec", "print(1)"),
    ("sql", "DROP TABLE studies"),
    ("shell", "curl example.com"),
])
def test_nothing_executable_can_be_smuggled_into_a_decision(field, value):
    """`extra="forbid"` is the mechanism, not a habit: a field the gate
    does not look at is a field that could mean anything."""
    with pytest.raises(ValidationError):
        ResearchDecision(kind=DecisionKind.COMPLETE,
                         completion=CompletionStatement(
                             completion_reason="done"),
                         **{field: value})


@pytest.mark.parametrize("payload", [
    ToolCallRequest(**_tool()),
    HypothesisUpdate(**_hypothesis()),
    DirectionUpdate(previous_direction="a", new_direction="b", reason="c"),
    AskUserRequest(question="q", why="w"),
    CompletionStatement(completion_reason="r"),
    BlockedStatement(reason="r"),
])
def test_every_payload_also_refuses_smuggled_fields(payload):
    """The seal is on the payloads too: a nested object is where a model
    would put what the top level refuses."""
    with pytest.raises(ValidationError):
        type(payload)(**{**payload.model_dump(), "unexpected": "x"})


def test_the_schema_version_travels_with_every_decision():
    decision = ResearchDecision(kind=DecisionKind.COMPLETE,
                                completion=CompletionStatement(
                                    completion_reason="够了"))
    assert decision.schema_version == RESEARCH_DECISION_SCHEMA_VERSION
    assert RESEARCH_DECISION_SCHEMA_VERSION.startswith("research-decision-v")


def test_a_completion_declares_its_evidence_sufficiency():
    decision = ResearchDecision(
        kind=DecisionKind.COMPLETE,
        completion=CompletionStatement(
            completion_reason="x",
            evidence_sufficiency=EvidenceSufficiency.INSUFFICIENT,
            limitations=["未做实验验证"]))
    assert decision.completion.evidence_sufficiency is (
        EvidenceSufficiency.INSUFFICIENT)
    assert {item.value for item in EvidenceSufficiency} == {
        "sufficient", "partial", "insufficient"}


def test_a_hypothesis_moves_only_along_the_existing_status_line():
    """No new status is invented for the intelligence: the study context's
    five values are the vocabulary."""
    from crea.core.models.study_context import HypothesisStatus
    update = HypothesisUpdate(**_hypothesis())
    assert update.proposed_status is HypothesisStatus.PROPOSED
    assert {status.value for status in HypothesisStatus} == {
        "proposed", "under_review", "supported", "refuted", "unresolved"}


# ---------------------------------------------------------------------------
# Parsing: a reply is either a decision or it is nothing
# ---------------------------------------------------------------------------


def test_a_bare_json_object_parses():
    decision, issues = parse_decision(
        '{"kind": "complete", "completion": {"completion_reason": "done"}}')
    assert issues == []
    assert decision.kind is DecisionKind.COMPLETE


def test_a_fenced_block_parses():
    decision, issues = parse_decision(
        '```json\n{"kind": "blocked", "blocked": {"reason": "没有工具"}}\n```')
    assert issues == []
    assert decision.kind is DecisionKind.BLOCKED


@pytest.mark.parametrize("text", [
    "",
    "我认为应该先检索文献。",
    '{"kind": "continue_with_tool"}',
    '{"tool": "search_literature"}',
    'prefix {"kind": "complete", "completion": {"completion_reason": "x"}}',
    "['kind']",
])
def test_prose_is_never_a_decision(text):
    decision, issues = parse_decision(text)
    assert decision is None
    assert issues, "a refusal must come with something the model can fix"


def test_a_reply_with_extra_fields_is_refused_at_parse_time():
    decision, issues = parse_decision(
        '{"kind": "complete", "completion": {"completion_reason": "x"}, '
        '"also_run": "os.system"}')
    assert decision is None
    assert any("Extra inputs" in issue or "not permitted" in issue
               for issue in issues), issues


def test_a_refusal_names_the_field_it_refused():
    decision, issues = parse_decision(
        '{"kind": "continue_with_tool", "tool_call": {"tool": "x"}}')
    assert decision is None
    assert any("tool_call" in issue for issue in issues)


# ---------------------------------------------------------------------------
# The audit record
# ---------------------------------------------------------------------------


def test_the_audit_record_carries_the_observability_fields():
    """§ eight of the M2.0 brief: every model decision is traceable."""
    record = DecisionRecord(
        decision_id="dec-1", session_id="sess-1", study_id="study-1",
        turn=3, model_provider="fake", model_name="fake-1",
        decision_type=DecisionKind.CONTINUE_WITH_TOOL,
        decision_summary="检索文献", selected_tool="search_literature",
        validation_result=DecisionOutcome.ACCEPTED,
        validation_detail="通过", duration_ms=120,
        input_tokens=100, output_tokens=20, warnings=["w"])
    dumped = record.model_dump()
    for field in ("decision_id", "session_id", "study_id", "turn",
                  "model_provider", "model_name", "prompt_version",
                  "schema_version", "decision_type", "decision_summary",
                  "selected_tool", "validation_result", "validation_detail",
                  "at", "duration_ms", "input_tokens", "output_tokens",
                  "warnings", "associated_step_id"):
        assert field in dumped, field


def test_the_audit_record_has_no_field_for_hidden_reasoning():
    """The decision summary, the purpose and the cited ids are the record.
    There is nowhere to put a chain of thought."""
    fields = set(DecisionRecord.model_fields)
    assert not fields & {"reasoning", "chain_of_thought", "thoughts",
                         "scratchpad", "raw_completion"}
    assert not set(ResearchDecision.model_fields) & {"reasoning", "thoughts"}


def test_an_interpretation_is_kept_beside_the_machine_reading():
    """The model's scientific reading and the observer's kind travel
    together, so a reader can see where meaning was added."""
    item = ScientificInterpretation(
        step_id="step-1", tool="search_literature",
        observation_kind="no_result",
        interpretation="三次检索都无命中，说明检索式可能过窄",
        evidence_ids=["ev-1"])
    assert item.observation_kind == "no_result"
    assert item.interpretation.startswith("三次检索")
    assert "kind" not in ScientificInterpretation.model_fields, (
        "the machine's own reading is a string here; the Observation it "
        "came from is the authority, not a second enum")


def test_an_accepted_record_keeps_the_decision_that_was_executed():
    decision = ResearchDecision(kind=DecisionKind.COMPLETE,
                                completion=CompletionStatement(
                                    completion_reason="done"))
    record = DecisionRecord(decision_id="d", validation_result=(
        DecisionOutcome.ACCEPTED), decision=decision)
    assert record.decision is decision

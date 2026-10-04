"""ResearchSession and the workflow-mode state machine.

§3 and §4 of Phase 3.7. Two things are under test: that a session is a
transcript of exactly one research task and not a memory of the person
asking, and that workflow mode cannot end as COMPLETED unless it is
complete -- the failure paths (LLM dies, model returns garbage) must end
PARTIAL with the gap named, never with a report that reads as finished.
"""

from __future__ import annotations

from crea.core.models.research import ResearchTask, TaskStatus
from crea.core.models.session import ResearchSession, WorkflowStatus

# ---------------------------------------------------------------------------
# §3 -- the session model
# ---------------------------------------------------------------------------


def test_the_session_records_one_task_and_stops_there():
    task = ResearchTask(task_id="t-1", question="分析 1,4-diphenyl-1,3-diyne")
    session = ResearchSession(session_id="s-1", task=task)

    assert session.session_id == "s-1"
    assert session.task is task
    assert session.created_at
    assert session.steps == []
    assert session.findings == []
    assert session.sources == []
    assert session.evidence_ids == []
    assert session.route_ids == []
    assert session.status is WorkflowStatus.RESEARCHING


def test_the_session_is_not_a_memory():
    """No user, no history, no carry-over. One task, one session, done.

    A session that remembered people would be a different product arriving
    through a data model, so the shape itself is the guard: fields that
    would have to exist for memory to exist are simply not there.
    """
    fields = set(ResearchSession.model_fields)
    assert not fields & {"user", "user_id", "history", "past_tasks",
                         "preferences", "memory"}
    # the only task it holds is the one it was built for
    assert "task" in fields
    assert len([f for f in fields if "task" in f]) == 1


def test_the_workflow_states_are_exactly_the_six_the_spec_names():
    assert [s.value for s in WorkflowStatus] == [
        "researching", "collecting_evidence", "validating",
        "reporting", "completed", "partial",
    ]
    # no FAILED: workflow mode ends completed or partial, nothing else
    assert not hasattr(WorkflowStatus, "FAILED")
    assert WorkflowStatus.COMPLETED.is_terminal
    assert WorkflowStatus.PARTIAL.is_terminal
    assert not WorkflowStatus.RESEARCHING.is_terminal


def test_a_session_round_trips_to_json():
    task = ResearchTask(task_id="t-2", question="q")
    session = ResearchSession(
        session_id="s-2", task=task,
        evidence_ids=["ev-1"], route_ids=["rt-1"],
        status=WorkflowStatus.VALIDATING)
    data = session.model_dump(mode="json")
    assert data["evidence_ids"] == ["ev-1"]
    assert data["route_ids"] == ["rt-1"]
    assert data["status"] == "validating"
    again = ResearchSession.model_validate(data)
    assert again.status is WorkflowStatus.VALIDATING


# ---------------------------------------------------------------------------
# §4 -- workflow mode through the real agent loop
# ---------------------------------------------------------------------------

QUESTION = "分析并研究 1,4-diphenyl-1,3-diyne"


def _agent(script=None):
    from crea.agent.offline import build_offline_stack

    agent, library, document_id = build_offline_stack(QUESTION)
    if script is not None:
        agent._llm = script
    return agent, library, document_id


def test_a_workflow_run_ends_completed_and_keeps_its_session():
    from crea.core.models.session import WorkflowStatus as WS

    agent, _lib, _doc = _agent()
    task = agent.run(QUESTION, workflow=True)

    assert task.status is TaskStatus.COMPLETED
    assert agent.session is not None
    session = agent.session
    assert session.status is WS.COMPLETED
    assert session.session_id
    assert session.task is task
    assert session.steps == task.steps
    assert session.findings == task.report.findings
    assert session.evidence_ids == [e.evidence_id for e in task.evidence]
    assert session.route_ids == [r.route_id for r in task.report.routes]


def test_the_session_walks_the_state_machine_in_order():
    """Evidence tools advance to COLLECTING_EVIDENCE, validation to
    VALIDATING, rendering to REPORTING -- forward only."""
    from crea.core.models.research import ActionKind, AgentAction
    from crea.llm.fake import FakeLLMProvider

    agent, _lib, document_id = _agent()
    seen: list[str] = []

    # observe the phase at each model turn by wrapping complete()
    inner = agent._llm

    class Watcher:
        name = "watcher"

        def complete(self, request):
            seen.append(agent.session.status.value)
            return inner.complete(request)

    agent._llm = Watcher()

    # the offline plan already runs analyze -> literature -> document ->
    # evidence; a workflow run of it must pass through collecting
    task = agent.run(QUESTION, workflow=True)
    assert agent.session.status.value == "completed"
    assert seen and seen[0] == "researching"
    assert "collecting_evidence" in seen

    # and a validation-only script reaches VALIDATING
    from crea.agent.offline import build_offline_stack
    agent2, _lib2, _doc2 = build_offline_stack(QUESTION)
    retros = agent2._registry.execute(
        "retrosynthesize", {"target_smiles": "c1ccccc1C#CC#Cc2ccccc2"})
    step_ids = [s["step_id"] for r in retros.data["routes"] for s in r["steps"]]
    agent2._llm = FakeLLMProvider(
        [AgentAction(action=ActionKind.TOOL_CALL, tool="retrosynthesize",
                     arguments={"target_smiles": "c1ccccc1C#CC#Cc2ccccc2"})]
        + [AgentAction(action=ActionKind.TOOL_CALL, tool="validate_route_step",
                       arguments={"step_id": sid}) for sid in step_ids]
        + [AgentAction(action=ActionKind.FINAL, summary="已检查候选路线。")])
    inner2 = agent2._llm
    seen2: list[str] = []

    class Watcher2:
        name = "watcher2"

        def complete(self, request):
            seen2.append(agent2.session.status.value)
            return inner2.complete(request)

    agent2._llm = Watcher2()
    task2 = agent2.run(QUESTION, workflow=True)
    assert any(s.tool == "validate_route_step" for s in task2.steps)
    assert "validating" in seen2
    # candidate routes with no evidence behind them is a PARTIAL run --
    # asserting COMPLETED here would be exactly the fake completeness §4 forbids
    assert agent2.session.status.value == "partial"


def test_llm_failure_in_workflow_mode_is_partial_never_a_fake_complete():
    """§4's hard rule: LLM 失败：必须 PARTIAL.

    A model that dies after the tools have produced something must leave a
    PARTIAL report that says the run was cut short -- not FAILED with the
    work thrown away, and above all not COMPLETED.
    """
    from crea.llm.fake import FakeLLMProvider, ScriptExhausted

    # script: one real tool call, then the model runs out and raises
    from crea.core.models.research import ActionKind, AgentAction

    agent, _lib, _doc = _agent(FakeLLMProvider([
        AgentAction(action=ActionKind.TOOL_CALL, tool="analyze_molecule",
                    arguments={"smiles": "c1ccccc1C#CC#Cc2ccccc2"}),
        # exhausted after this -- the next complete() raises
    ]))
    task = agent.run(QUESTION, workflow=True)

    assert task.status is TaskStatus.PARTIAL
    assert task.status is not TaskStatus.FAILED
    assert task.status is not TaskStatus.COMPLETED
    assert agent.session.status.value == "partial"
    assert any("LLM 未完成" in item for item in task.unresolved_items)

    # the work that DID happen survives, labelled as computed
    assert any(f.kind == "computed" for f in task.report.findings)
    # and the report itself is partial
    assert task.report.status is TaskStatus.PARTIAL

    from crea.report.research import format_report

    text = format_report(task.report)
    assert "部分完成" in text
    assert "已完成" not in text.split("**状态**")[1].split("\n")[0]


def test_a_model_that_returns_garbage_also_ends_partial_in_workflow_mode():
    from crea.llm.fake import FakeLLMProvider

    class Garbage:
        name = "garbage"

        def complete(self, request):
            from crea.llm.base import LLMResponse

            return LLMResponse(text="not json at all", provider="garbage",
                              model="g-1")

    agent, _lib, _doc = _agent(Garbage())
    task = agent.run(QUESTION, workflow=True)

    assert task.status is TaskStatus.PARTIAL
    assert agent.session.status.value == "partial"
    assert any("未返回可用动作" in item for item in task.unresolved_items)


def test_ordinary_mode_still_fails_a_dead_model():
    """The stricter contract is workflow mode's only; ordinary mode keeps
    its old answer: a dead model is a failed task, not a partial one."""
    from crea.llm.fake import FakeLLMProvider

    agent, _lib, _doc = _agent(FakeLLMProvider([]))
    task = agent.run(QUESTION)

    assert task.status is TaskStatus.FAILED
    # the session still recorded the attempt, as partial
    assert agent.session.status.value == "partial"


def test_workflow_mode_never_reports_failed_even_with_nothing_produced():
    from crea.llm.fake import FakeLLMProvider

    agent, _lib, _doc = _agent(FakeLLMProvider([]))
    task = agent.run(QUESTION, workflow=True)

    assert task.status is TaskStatus.PARTIAL
    assert any("研究未能产出任何结论" in item or "LLM 未完成" in item
               for item in task.unresolved_items)
    assert task.report is not None
    assert task.report.status is TaskStatus.PARTIAL

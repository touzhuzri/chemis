"""M2.0 -- the AI Research Intelligence's product entry.

What is pinned here: the application layer receives its LLM and its tool
registry as injected factories and never builds one; a run can be started,
read, answered, reported on and cancelled; a paused run is answerable and
says so; every byte that leaves the layer is credential-redacted; and the
HTTP routes expose exactly those five moves with structured errors.

The LLM here is the project's own scripted provider: the default suite
never holds an API key, and an entry test that needed one would be a test
that could not run.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from crea.agent.intelligence import ResearchBudget
from crea.agent.tools import ExecutionPolicy, ToolRegistry, ToolSpec
from crea.app.research_intelligence import ResearchIntelligenceApplication
from crea.core.errors import ResearchStudyError
from crea.llm.fake import TextScriptedLLMProvider


class _LiteratureArgs(BaseModel):
    query: str = Field(default="")


class Script:
    def __init__(self, payloads: list) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads.pop(0) if self.payloads else {"status": "ok"}
        if isinstance(payload, Exception):
            raise payload
        return payload

    @property
    def count(self) -> int:
        return len(self.calls)


def build_stack(*, literature: Script | None = None):
    literature = literature or Script([{"status": "ok", "count": 1,
                                        "papers": [{"title": "A"}],
                                        "evidence_ids": ["ev-1"]}])
    registry = ToolRegistry(enforce_policy=True,
                            policy=ExecutionPolicy(allow_network=True))
    registry.register(ToolSpec(
        name="search_literature", description="有界文献发现",
        arguments_model=_LiteratureArgs, run=literature, access=("local",),
        sets_facts=("literature_searched",)))
    return registry, None, []


def scripted(*payloads) -> TextScriptedLLMProvider:
    return TextScriptedLLMProvider(
        [p if isinstance(p, str) else json.dumps(p, ensure_ascii=False)
         for p in payloads])


def tool_call(query, interpretation=""):
    return {"kind": "continue_with_tool", "tool_call": {
        "tool": "search_literature", "arguments": {"query": query},
        "decision_summary": "检索", "purpose": "定位报道",
        "expected_information": "候选论文",
        "related_hypothesis_ids": [], "relevant_evidence_ids": []},
        "observation_interpretation": interpretation}


def complete(reason, sufficiency="partial", limitations=None):
    return {"kind": "complete", "completion": {
        "completion_reason": reason, "achieved_objectives": [],
        "unresolved_questions": [], "evidence_sufficiency": sufficiency,
        "limitations": list(limitations or [])}}


def ask(question, options=None):
    return {"kind": "ask_user", "ask_user": {
        "question": question, "why": "需要人来回答",
        "expected_information": "用户的明确选择",
        "options": list(options or [])}}


@pytest.fixture()
def application(tmp_path):
    """An entry whose LLM and registry are injected, as they must be."""
    return ResearchIntelligenceApplication(
        db_path=tmp_path / "crea.db",
        llm_factory=lambda: scripted(
            tool_call("阿司匹林 绿色合成", interpretation="检索命中一条"),
            complete("已有候选路线", sufficiency="partial",
                     limitations=["未做实验验证"])),
        stack_factory=lambda document_ids=None: build_stack())


# ---------------------------------------------------------------------------
# The five moves
# ---------------------------------------------------------------------------


def test_a_run_can_be_started_from_a_natural_language_goal(application):
    result = application.start("帮我研究这个天然产物的可能合成路线")

    assert result["outcome"] == "completed"
    assert result["mode"] == "ai_research_intelligence"
    assert result["goal"].startswith("帮我研究")
    assert [s["tool"] for s in result["steps"]] == ["search_literature"]
    assert result["evidence_ids"] == ["ev-1"]
    assert result["session_id"].startswith("sess-")
    assert result["study_id"].startswith("study-")


def test_a_run_can_be_read_back_and_its_decisions_audited(application):
    result = application.start("研究X")
    session_id = result["session_id"]

    fetched = application.get(session_id)
    decisions = application.decisions(session_id)

    assert fetched["session_id"] == session_id
    assert [d["decision_type"] for d in decisions] == [
        "continue_with_tool", "complete"]
    assert decisions[0]["model_provider"] == "text-scripted"
    assert decisions[0]["prompt_version"].startswith("m2.0-intelligence")
    assert decisions[0]["validation_result"] == "accepted"
    assert decisions[0]["selected_tool"] == "search_literature"


def test_a_run_can_be_reported_on_in_prose(application):
    result = application.start("研究X")

    report = application.report(result["session_id"])

    assert report.startswith("# AI 研究报告")
    assert "研究X" in report
    assert "AI 解读" in report
    assert "证据部分充分" in report
    assert "未做实验验证" in report


def test_a_paused_run_is_answerable_and_carries_on(tmp_path):
    literature = Script([{"status": "ok", "evidence_ids": ["ev-1"]}])
    turns = iter([
        ask("请选择目标结构", options=["A", "B"]),
        tool_call("阿司匹林"),
        complete("已有结果"),
    ])
    application = ResearchIntelligenceApplication(
        db_path=tmp_path / "crea.db",
        llm_factory=lambda: scripted(*[next(turns) for _ in range(3)]),
        stack_factory=lambda document_ids=None: build_stack(
            literature=literature))

    paused = application.start("研究X")
    assert paused["outcome"] == "input_required"
    assert paused["awaiting"]["question"] == "请选择目标结构"
    assert paused["awaiting"]["options"] == ["A", "B"]

    resumed = application.answer(paused["session_id"],
                                 answers={"selection": "A"})

    assert resumed["outcome"] == "completed"
    assert resumed["awaiting"] is None
    assert resumed["evidence_ids"] == ["ev-1"]


def test_answering_a_run_that_is_not_waiting_is_refused(application):
    result = application.start("研究X")
    with pytest.raises(ResearchStudyError) as excinfo:
        application.answer(result["session_id"], answers={"x": "y"})
    assert excinfo.value.code == "NOT_AWAITING_INPUT"


def test_an_unknown_session_is_a_typed_error(application):
    with pytest.raises(ResearchStudyError) as excinfo:
        application.get("sess-nothing")
    assert excinfo.value.code == "SESSION_NOT_FOUND"


def test_a_run_can_be_cancelled(application):
    result = application.start("研究X")
    cancelled = application.cancel(result["session_id"])

    # The run was already finished; cancelling a finished run is reported,
    # not restarted.
    assert cancelled["session_id"] == result["session_id"]
    assert cancelled["outcome"] == "completed"


def test_an_empty_goal_is_refused(application):
    with pytest.raises(ResearchStudyError) as excinfo:
        application.start("   ")
    assert excinfo.value.code == "INVALID_INPUT"


def test_an_entry_with_no_registry_factory_says_so(tmp_path):
    application = ResearchIntelligenceApplication(db_path=tmp_path / "x.db")
    with pytest.raises(ResearchStudyError) as excinfo:
        application.start("研究X")
    assert excinfo.value.code == "STACK_NOT_CONFIGURED"


def test_no_llm_factory_means_an_honest_model_unavailable(tmp_path):
    application = ResearchIntelligenceApplication(
        db_path=tmp_path / "crea.db",
        stack_factory=lambda document_ids=None: build_stack())
    result = application.start("研究X")

    assert result["outcome"] == "failed"
    assert result["blocked"]["blocker"] == "model_unavailable"
    assert result["steps"] == []


def test_the_budget_reaches_the_loop_through_the_entry(tmp_path):
    turns = [tool_call(f"q{i}") for i in range(5)]
    application = ResearchIntelligenceApplication(
        db_path=tmp_path / "crea.db",
        llm_factory=lambda: scripted(*turns),
        stack_factory=lambda document_ids=None: build_stack(),
        budget=ResearchBudget(max_tool_calls=2))
    result = application.start("研究X")

    assert result["budget"]["tool_calls_used"] == 2
    assert result["blocked"]["blocker"] == "budget_exhausted"


# ---------------------------------------------------------------------------
# What leaves the layer
# ---------------------------------------------------------------------------


def test_nothing_that_leaves_the_layer_carries_a_credential(tmp_path):
    application = ResearchIntelligenceApplication(
        db_path=tmp_path / "crea.db",
        llm_factory=lambda: scripted(complete("done")),
        stack_factory=lambda document_ids=None: build_stack())
    result = application.start("研究X")

    text = json.dumps(result, ensure_ascii=False)
    for secret in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CREA_LLM_API_KEY",
                   "sk-", "api_key", "password"):
        assert secret not in text, secret


def test_a_tool_result_is_summarised_not_dumped(application):
    """A long payload does not ride out on the API: the entry reports the
    observation's kind and summary, not the whole document body."""
    result = application.start("研究X")
    observation = result["observations"][0]

    assert set(observation) == {"step_id", "tool", "kind", "summary",
                                "evidence_ids"}


# ---------------------------------------------------------------------------
# The HTTP routes
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    from crea.api.app import create_app

    application = ResearchIntelligenceApplication(
        db_path=tmp_path / "crea.db",
        llm_factory=lambda: scripted(
            ask("请选择目标结构", options=["A", "B"]),
            tool_call("阿司匹林"),
            complete("已有结果")),
        stack_factory=lambda document_ids=None: build_stack())

    from crea.api.dependencies import create_context

    context = create_context(db_path=tmp_path / "api.db",
                             documents_dir=tmp_path / "docs", offline=True)
    context.research_intelligence = application
    return TestClient(create_app(context=context))


def test_the_intelligence_routes_exist_and_are_ordered_before_job_ids(client):
    response = client.post("/api/research/intelligence",
                           json={"goal": "研究阿司匹林的绿色合成路线"})

    assert response.status_code == 200
    body = response.json()
    # Not read as a job id: the route matched the literal path.
    assert body["outcome"] == "input_required"
    assert body["awaiting"]["question"] == "请选择目标结构"


def test_the_whole_pause_answer_report_cycle_over_http(client):
    paused = client.post("/api/research/intelligence",
                         json={"goal": "研究X"}).json()
    session_id = paused["session_id"]

    read = client.get(f"/api/research/intelligence/{session_id}")
    decisions = client.get(
        f"/api/research/intelligence/{session_id}/decisions")
    answered = client.post(
        f"/api/research/intelligence/{session_id}/answer",
        json={"answers": {"selection": "A"}})
    report = client.get(
        f"/api/research/intelligence/{session_id}/report")

    assert read.status_code == 200
    assert decisions.json()["decisions"][0]["decision_type"] == "ask_user"
    assert answered.status_code == 200
    assert answered.json()["outcome"] == "completed"
    assert "AI 研究报告" in report.json()["report"]


def test_a_missing_goal_is_a_structured_400(client):
    response = client.post("/api/research/intelligence", json={})

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["detail"] == "goal"


def test_an_unknown_session_is_a_structured_404(client):
    response = client.get("/api/research/intelligence/sess-nothing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SESSION_NOT_FOUND"


def test_answering_a_finished_run_is_a_structured_409(client):
    started = client.post("/api/research/intelligence",
                          json={"goal": "研究X"}).json()
    session_id = started["session_id"]
    client.post(f"/api/research/intelligence/{session_id}/answer",
                json={"answers": {"selection": "A"}})

    response = client.post(
        f"/api/research/intelligence/{session_id}/answer",
        json={"answers": {"selection": "B"}})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NOT_AWAITING_INPUT"


def test_the_ordinary_research_job_route_is_untouched(client):
    """The M2.0 entry is additive: the pre-existing job routes still
    answer, and `/api/research` is still the job list."""
    response = client.get("/api/research")

    assert response.status_code == 200
    assert "jobs" in response.json()

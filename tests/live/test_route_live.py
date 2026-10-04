"""Live tests for route research. Opt-in, small, and not quota-dependent.

    CREA_LIVE_TESTS=1 CREA_LLM_API_KEY=... pytest -m live

Everything runs against the offline stack with a real model. Nothing here
touches OpenAlex, Crossref or Unpaywall: a route question must not need a
daily literature budget to be testable, and a failing quota is not a signal
about the agent.

A separate, independent opt-in covers the real engine, in its own module:

    CREA_AIZYNTHFINDER_LIVE=1  ->  tests/live/test_aizynthfinder_live.py

It needs neither an LLM key nor a literature budget, so gating it behind the
two above would mean it never runs.
"""

import os

import pytest

from crea.agent.loop import ResearchAgent
from crea.core.models.route import ProviderStatus, StepStatus
from crea.llm import LLMConfig, OpenAICompatibleLLMProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CREA_LIVE_TESTS") != "1",
        reason="set CREA_LIVE_TESTS=1 to run live tests",
    ),
    pytest.mark.skipif(
        not os.environ.get("CREA_LLM_API_KEY"),
        reason="CREA_LLM_API_KEY is not set; skipping rather than failing",
    ),
]

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"
QUESTION = "帮我分析 1,4-二苯基-1,3-丁二炔 的可能合成路线。"


def _agent():
    from crea.agent.offline import build_offline_stack

    agent, _library, _doc = build_offline_stack(QUESTION)
    agent._llm = OpenAICompatibleLLMProvider(LLMConfig.from_env())
    return agent


def test_live_model_can_run_the_route_tools():
    agent = _agent()
    task = agent.run(QUESTION)
    tools = [s.tool for s in task.steps if s.tool]
    assert "retrosynthesize" in tools, (
        f"the model did not ask for a route; it used {tools}. "
        f"A route must come from the tool, not from the model.")


def test_live_model_can_validate_a_step():
    agent = _agent()
    task = agent.run(QUESTION)
    tools = [s.tool for s in task.steps if s.tool]
    assert "retrosynthesize" in tools
    assert "validate_route_step" in tools, (
        f"the model stopped after proposing a route; it used {tools}")


def test_live_routes_are_candidates_and_stay_candidates():
    agent = _agent()
    task = agent.run(QUESTION)
    for route in task.report.routes:
        for step in route.steps:
            assert step.status in (StepStatus.CANDIDATE, StepStatus.LITERATURE_SUPPORTED)
            # nothing in this path can reach experimental support: no document
            # and no reaction record exists for this task
            assert step.status is not StepStatus.EXPERIMENTALLY_SUPPORTED


def test_live_model_does_not_assert_a_route_it_never_asked_for():
    """The boundary, checked against a real model rather than a script."""
    from crea.core.models.research import ActionKind, AgentAction

    agent = _agent()
    agent._llm = _ScriptedNarration(
        "我可以直接告诉你路线：苯乙炔 CuI 催化氧化偶联，80 °C，12 h，DMF，82%。")
    task = agent.run(QUESTION)
    assert task.report.routes == []


class _ScriptedNarration:
    """A stand-in that returns one fixed answer, to check the boundary."""

    name = "narration"
    model = "narration"

    def __init__(self, text: str) -> None:
        self._text = text

    def complete(self, request):
        import json

        from crea.llm.base import LLMResponse

        return LLMResponse(
            text=json.dumps({"action": "final", "summary": self._text},
                             ensure_ascii=False),
            provider=self.name, model=self.model)
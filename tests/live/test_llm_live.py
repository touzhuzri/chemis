"""Live test for a real LLM. Opt-in, tiny, and never in the default gate.

    CREA_LIVE_TESTS=1 CREA_LLM_API_KEY=... pytest -m live

Both conditions are required. Without the key the test skips rather than
fails: a machine with no credential has not demonstrated anything is broken,
and failing would train people to ignore red tests here.

It costs a few requests at most. It does not run a research task -- the point
is to prove the wire format, not to spend a budget.
"""

import asyncio
import os

import pytest

from crea.core.errors import ProviderUnavailable
from crea.llm import LLMConfig, LLMRequest, OpenAICompatibleLLMProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CREA_LIVE_TESTS") != "1",
        reason="set CREA_LIVE_TESTS=1 to run live LLM tests",
    ),
    pytest.mark.skipif(
        not os.environ.get("CREA_LLM_API_KEY"),
        reason="CREA_LLM_API_KEY is not set; skipping rather than failing",
    ),
]


def _provider() -> OpenAICompatibleLLMProvider:
    return OpenAICompatibleLLMProvider(LLMConfig.from_env())


def test_live_provider_returns_text():
    response = _provider().complete(LLMRequest(
        system="Reply with the single word: ok",
        messages=[{"role": "user", "content": "ping"}],
        # enough for a reasoning model's thinking *and* its answer; a smaller
        # budget gets spent entirely on reasoning and returns an empty content
        max_output_tokens=256,
    ))
    assert response.text.strip()
    assert response.model
    assert response.provider == "openai-compatible"


def test_live_provider_can_emit_a_structured_tool_call():
    """The capability this phase depends on: a real model choosing a tool."""
    response = _provider().complete(LLMRequest(
        system=("You are a chemistry assistant. Use the analyze_molecule tool "
                "for any question about a molecule."),
        messages=[{"role": "user", "content": "分析苯的结构"}],
        tools=[{
            "name": "analyze_molecule",
            "description": "Normalise a SMILES string and report its properties.",
            "arguments": {
                "type": "object",
                "properties": {"smiles": {"type": "string",
                                          "description": "a SMILES string"}},
                "required": ["smiles"],
            },
        }],
    ))
    assert response.action is not None, f"no tool call: {response.text!r}"
    assert response.action.get("tool") == "analyze_molecule"
    assert response.action.get("arguments", {}).get("smiles")


def test_live_agent_completes_a_one_step_task():
    """One real tool call, executed by the real registry, returned to the model."""
    from crea.agent.loop import ResearchAgent
    from crea.agent.live import build_live_stack

    agent, _library = build_live_stack(_provider())
    task = agent.run("调用 analyze_molecule 分析 c1ccccc1，然后报告分子式。")
    assert task.steps, "the model made no move at all"
    assert task.status.value in ("completed", "partial", "failed")
    if any(s.tool == "analyze_molecule" for s in task.steps):
        assert any("C6H6" in f.text for f in task.report.findings)


def test_a_missing_credential_is_an_error_not_a_fake_run():
    """The one behaviour a real run must never get wrong."""
    saved = {k: os.environ.pop(k, None) for k in ("CREA_LLM_API_KEY",)}
    try:
        with pytest.raises(ProviderUnavailable):
            LLMConfig.from_env()
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

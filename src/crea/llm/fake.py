"""A scripted LLM, for tests and for running the agent with no key.

It is a lookup, not a model. Given the thread so far, it returns the next
scripted response. That is enough to exercise the whole agent loop -- planning,
tool calls, evidence, claims, a report -- with no network, no key, and results
that are identical on every run.

The script is a list of `AgentAction`s in order. If the agent asks for something
the script has not anticipated, the fake says so rather than inventing: an
unrecognised turn raises, because a provider that quietly improvises is exactly
the failure mode the real one has.
"""

from __future__ import annotations

import json

from crea.core.models.research import AgentAction
from crea.llm.base import LLMRequest, LLMResponse


class ScriptExhausted(RuntimeError):
    """The agent asked for a turn the script does not have."""


class FakeLLMProvider:
    """Replays a fixed list of actions."""

    name = "fake"

    def __init__(self, script: list[AgentAction], model: str = "fake-1") -> None:
        self._script = list(script)
        self._model = model
        self.calls: list[LLMRequest] = []
        self.model = model

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if not self._script:
            raise ScriptExhausted(
                "the fake LLM's script ran out; the agent asked for more turns "
                "than the test planned for"
            )
        action = self._script.pop(0)
        return LLMResponse(
            text=json.dumps(action.model_dump(mode="json"), ensure_ascii=False),
            provider=self.name,
            model=self._model,
            metadata={"turn": len(self.calls), "scripted": True},
        )

    @property
    def remaining(self) -> int:
        return len(self._script)


def scripted(*actions: AgentAction) -> FakeLLMProvider:
    return FakeLLMProvider(list(actions))


class TextScriptedLLMProvider:
    """Replays a list of raw text responses (CREA Stage 6.2).

    For callers that want a free-text / JSON reply rather than an `AgentAction`
    script -- the modification analyzer parses JSON out of the model's text.
    Deterministic; a missing turn raises rather than inventing one.
    """

    name = "text-scripted"

    def __init__(self, script: list[str], model: str = "text-scripted-1") -> None:
        self._script = list(script)
        self.model = model
        self.calls: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if not self._script:
            raise ScriptExhausted(
                "the text-scripted LLM's script ran out; the caller asked for "
                "more turns than the test planned for")
        return LLMResponse(text=self._script.pop(0), provider=self.name,
                           model=self.model, metadata={"scripted": True})

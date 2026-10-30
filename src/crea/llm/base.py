"""LLM boundary: prompt in, structured response out.

Deliberately narrow. A provider turns a prompt into text and reports what it
cost; it has no chemistry, no literature access, no filesystem and no network
of its own beyond the model call. Everything else the agent needs arrives as a
tool result, which is the only thing allowed into agent state as a fact.

Nothing in the agent, the tools or the report may be bound to a particular
vendor. That is why the seam exists at all: swapping models should change
nothing above it.

`redact` is re-exported here (it originally lived in this module) because
every layer that writes a record down needs it, not only the model side.
Its home is now `crea.core.redaction`.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from crea.core.redaction import SECRET_ENV_NAMES, redact


class LLMRequest(BaseModel):
    """One call. `system` says what the model is for; `messages` is the thread.

    `messages` is deliberately vendor-neutral. A tool result arrives as
    `{"role": "user", "content": "tool_result(<name>) = ..."}` and the provider
    translates it into whatever its own API expects. The agent above this line
    never sees a tool-call id, an OpenAI role, or an Anthropic content block.
    """

    system: str = ""
    messages: list[dict] = Field(default_factory=list)
    #: Tool schemas the model may call. Empty means "answer in prose".
    tools: list[dict] = Field(default_factory=list)
    max_output_tokens: int | None = None
    temperature: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    """A model turn, with whatever structure it produced.

    `action` is the vendor-neutral form of a tool call or a final answer, so
    the agent does not parse a particular API's envelope. `text` is always
    present, including alongside a tool call, because a model that asks for a
    tool usually says why.
    """

    text: str = ""
    provider: str
    model: str
    action: dict | None = None
    tool_call_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    stop_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, request: LLMRequest) -> LLMResponse:
        ...


__all__ = ["LLMProvider", "LLMRequest", "LLMResponse", "SECRET_ENV_NAMES",
           "redact"]

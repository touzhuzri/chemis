"""LLM boundary: prompt in, structured response out.

Deliberately narrow. A provider turns a prompt into text and reports what it
cost; it has no chemistry, no literature access, no filesystem and no network
of its own beyond the model call. Everything else the agent needs arrives as a
tool result, which is the only thing allowed into agent state as a fact.

Nothing in the agent, the tools or the report may be bound to a particular
vendor. That is why the seam exists at all: swapping models should change
nothing above it.
"""

from __future__ import annotations

import re
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


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


#: Environment values that must never reach a trace or a report. Read from the
#: environment, used to authenticate, never recorded.
SECRET_ENV_NAMES = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CREA_OPENALEX_API_KEY",
    "UNPAYWALL_EMAIL", "CREA_UNPAYWALL_EMAIL", "OPENALEX_EMAIL",
)


def redact(value: Any) -> Any:
    """Strip anything that looks like a credential, recursively.

    A trace is written to disk and read by people; a key in one is a key
    leaked. Values are replaced, not merely hidden, so a redaction cannot be
    defeated by a field name we did not anticipate.
    """
    secret_names = {n.lower() for n in SECRET_ENV_NAMES}
    # Matched per word, not per substring: "keywords" is not a credential, and a
    # substring test would redact it and teach nobody anything. A bare "email" is
    # redacted, because a contact address in a trace is still someone's address.
    secret_words = {"key", "keys", "token", "secret", "password", "passwd",
                    "auth", "authorization", "email"}
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lowered = str(key).lower()
            words = {w for w in re.split(r"[^a-z0-9]+", lowered) if w}
            if lowered in secret_names or (words & secret_words):
                out[key] = "<redacted>"
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value

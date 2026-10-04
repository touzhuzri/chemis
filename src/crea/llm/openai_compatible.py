"""An OpenAI-compatible LLM provider.

One class serves every endpoint that speaks the OpenAI chat-completions
protocol: OpenAI, OpenRouter, DeepSeek, SiliconFlow, a local vLLM, an Ollama
compat server. The vendor is a base URL, not a subclass.

Two things this provider is responsible for, and nothing else:

* **HTTP** -- one POST, translated error for translated error.
* **Translation** -- the agent's neutral thread in, and whatever the model
  returns out as a neutral `action`. A native `tool_calls` envelope becomes
  `{"action": "tool_call", "tool": ..., "arguments": ...}`; a JSON body in
  the text is parsed; plain prose stays prose.

It does not do chemistry, retrieve literature, read files or reach a database.
If a tool result is not already in the thread, this provider has no way to
invent one.

**No silent fallback.** A missing key or a failing endpoint raises. Substituting
the fake provider would hand the user a report that looks researched and is
not, which is the one outcome this project exists to prevent.
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel, Field

from crea.core.errors import (
    AuthenticationOrAuthorizationError,
    InvalidRequest,
    InvalidUpstreamResponse,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
    UpstreamError,
)
from crea.llm.base import LLMRequest, LLMResponse

ENV_API_KEY = "CREA_LLM_API_KEY"
ENV_BASE_URL = "CREA_LLM_BASE_URL"
ENV_MODEL = "CREA_LLM_MODEL"

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
"""A default, not a choice. `CREA_LLM_MODEL` overrides it; nothing here
hard-codes a particular vendor's flagship."""


class LLMConfig(BaseModel):
    """Configuration. No defaults for the credential; the base URL and model
    have defaults because they are conveniences, not permissions."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 120.0
    extra_headers: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "LLMConfig":
        # The operator may have put the credential in `.env` rather than in the
        # shell. Read it here, where the credential is needed, so importing this
        # package never touches a file.
        from crea.core.env import load_local_env

        load_local_env()
        api_key = os.environ.get(ENV_API_KEY, "").strip()
        if not api_key:
            raise ProviderUnavailable(
                f"{ENV_API_KEY} is not set. A real LLM run needs a credential; "
                f"there is deliberately no fallback to the scripted provider, "
                f"because a fabricated research report is worse than none."
            )
        return cls(
            api_key=api_key,
            base_url=os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL).rstrip("/"),
            model=os.environ.get(ENV_MODEL, DEFAULT_MODEL),
        )


def to_openai_tools(tools: list[dict]) -> list[dict]:
    """Our tool catalogue -> the OpenAI function-calling envelope.

    A tool that declares a JSON Schema already has what the protocol needs; the
    only translation is the `function` wrapper and the `strict` flag we do not
    set, because strict schemas reject arguments a model would reasonably
    produce and a rejected argument is a wasted round trip.
    """
    out = []
    for tool in tools:
        out.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("arguments") or {"type": "object",
                                                        "properties": {}},
            },
        })
    return out


def to_openai_messages(request: LLMRequest,
                       pending_calls: list[dict]) -> list[dict]:
    """The neutral thread -> OpenAI messages.

    Tool results travel as neutral `user` messages containing
    `tool_result(name) = …`, and become real `tool` messages here.

    A `tool` message is only legal directly after the `assistant` turn that
    requested it, and it must repeat that turn's `tool_call_id`. Emitting the
    `tool` message alone -- which is what this did at first -- is a protocol
    violation, the server rejects the whole request, and the symptom is that the
    model appears to "say nothing" on the second turn. The agent reported that
    as an unusable action, which made a wire-format bug look like a model that
    had run out of things to say. The assistant turn is replayed here for
    exactly that reason.
    """
    messages: list[dict] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})

    queue = list(pending_calls or [])
    for message in request.messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if role == "user" and content.startswith("tool_result(") and queue:
            call = queue.pop(0)
            messages.append({
                "role": "assistant",
                "content": call.get("content") or "",
                "tool_calls": [{
                    "id": call.get("id"),
                    "type": "function",
                    "function": call.get("function") or {},
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id") or "",
                "content": content,
            })
        elif role == "user":
            messages.append({"role": "user", "content": content})
        else:
            messages.append(message)
    return messages


def action_from_payload(payload: dict) -> tuple[dict | None, str]:
    """Whatever came back -> a neutral action, and any text alongside it.

    Three shapes are accepted, in order: a native tool call, a JSON object in
    the text, and plain prose. The third is not an error -- a model is allowed
    to say something -- but it is never mistaken for a structured action.
    """
    if not isinstance(payload, dict):
        return None, ""

    choices = payload.get("choices") or []
    if not choices:
        return None, ""
    choice = choices[0]
    message = choice.get("message") or {}

    calls = message.get("tool_calls") or []
    if calls:
        first = calls[0]
        function = first.get("function") or {}
        arguments = function.get("arguments") or "{}"
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
        except ValueError:
            parsed = {}
        return ({"action": "tool_call",
                 "tool": function.get("name"),
                 "arguments": parsed}, message.get("content") or "")

    text = message.get("content") or ""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and (
                "action" in parsed or "tool" in parsed or "claims" in parsed):
            return parsed, text
    return None, text


class OpenAICompatibleLLMProvider:
    """Chat completions against any OpenAI-compatible endpoint."""

    name = "openai-compatible"

    def __init__(self, config: LLMConfig, client: Any | None = None) -> None:
        self._config = config
        self.model = config.model
        self._client = client
        self._pending_calls: list[dict] = []

    # -- transport ----------------------------------------------------------

    def _http(self):
        if self._client is not None:
            return self._client
        try:
            import httpx
        except ImportError as exc:
            raise ProviderUnavailable(
                "httpx is required for a real LLM run. Install the live extra: "
                "pip install -e '.[live]'. The offline suite does not need it."
            ) from exc
        self._client = httpx.Client(timeout=self._config.timeout_seconds)
        return self._client

    def complete(self, request: LLMRequest) -> LLMResponse:
        messages = to_openai_messages(request, self._pending_calls)
        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
        }
        if request.tools:
            body["tools"] = to_openai_tools(request.tools)
            body["tool_choice"] = "auto"
        if request.max_output_tokens:
            body["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature

        url = f"{self._config.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
            **self._config.extra_headers,
        }

        client = self._http()
        try:
            response = client.post(url, headers=headers, json=body)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - connection, timeout, DNS
            raise ProviderError(
                f"could not reach the LLM endpoint at {self._config.base_url}: "
                f"{type(exc).__name__}: {exc}") from exc

        status = getattr(response, "status_code", 0)
        if status >= 400:
            raise _map_status(status, self._config.base_url,
                              _body_of(response))
        payload = _body_of(response)
        if not isinstance(payload, dict):
            raise InvalidUpstreamResponse(
                "the LLM endpoint returned a body that is not a JSON object")
        if not (payload.get("choices") or []):
            # An error body that got past the status check. Surfaced rather than
            # returned as an empty answer, so a rejected request is never
            # mistaken for a model that had nothing to say.
            raise InvalidUpstreamResponse(
                "the LLM endpoint returned no choices: "
                f"{json.dumps(payload, ensure_ascii=False)[:300]}",
                provider=self.name)

        action, text = action_from_payload(payload)
        # Remember the calls this turn made; the next turn's tool results are
        # their answers and the protocol needs them paired.
        self._pending_calls = [
            {"id": call.get("id"),
             "content": (payload["choices"][0].get("message") or {}).get("content") or "",
             "function": call.get("function") or {}}
            for call in ((payload["choices"][0].get("message") or {}).get("tool_calls") or [])
        ]
        usage = payload.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.name,
            model=payload.get("model") or self._config.model,
            action=action,
            tool_call_id=(self._pending_calls[0].get("id")
                          if self._pending_calls else None),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            stop_reason=(payload.get("choices") or [{}])[0].get("finish_reason"),
            metadata={"base_url": self._config.base_url},
        )

    def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()


def _body_of(response) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return getattr(response, "text", "")


def _map_status(status: int, base_url: str, body: Any) -> ProviderError:
    where = f"{base_url}/chat/completions"
    if status in (401, 403):
        return AuthenticationOrAuthorizationError(
            f"the LLM endpoint refused the credential (HTTP {status}); "
            f"check {ENV_API_KEY}", provider="openai-compatible", http_status=status)
    if status == 400:
        return InvalidRequest(
            f"the LLM endpoint rejected the request (400): "
            f"{str(body)[:200]}", provider="openai-compatible", http_status=status)
    if status == 429:
        return RateLimited(
            f"the LLM endpoint returned 429: {where}",
            provider="openai-compatible", http_status=status)
    if status >= 500:
        return UpstreamError(
            f"the LLM endpoint failed upstream (HTTP {status})",
            provider="openai-compatible", http_status=status)
    return ProviderError(
        f"unexpected LLM endpoint status {status}: {where}",
        provider="openai-compatible", http_status=status)

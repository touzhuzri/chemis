"""LLM boundary. A provider turns a prompt into text; nothing else.

`FakeLLMProvider` replays a script, so the agent's whole loop is testable with
no key, no network, and identical results on every run.
"""

from crea.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    redact,
)
from crea.llm.fake import (
    FakeLLMProvider,
    ScriptExhausted,
    TextScriptedLLMProvider,
    scripted,
)
from crea.llm.openai_compatible import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_MODEL,
    LLMConfig,
    OpenAICompatibleLLMProvider,
)

__all__ = [
    "ENV_API_KEY", "ENV_BASE_URL", "ENV_MODEL",
    "FakeLLMProvider", "LLMConfig", "LLMProvider", "LLMRequest",
    "LLMResponse", "OpenAICompatibleLLMProvider", "ScriptExhausted",
    "TextScriptedLLMProvider", "redact", "scripted",
]

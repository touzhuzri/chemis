"""Credential redaction, for anything that might be written down (M2.0).

Lives in `core` rather than behind the LLM boundary because the thing being
protected is not a model: it is a *record*. A decision record, a research
trace, a tool result and an HTTP response all end up read by people, and a
key in any of them is a key leaked. Every layer that writes something down
needs this, so it sits where every layer may reach it.

`crea.llm.base` re-exports the same function so the pre-M2.0 call sites
keep working unchanged.
"""

from __future__ import annotations

import re
from typing import Any

#: Environment values that must never reach a trace, a report or an API
#: response. Read from the environment, used to authenticate, never recorded.
SECRET_ENV_NAMES = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CREA_OPENALEX_API_KEY",
    "UNPAYWALL_EMAIL", "CREA_UNPAYWALL_EMAIL", "OPENALEX_EMAIL",
    "CREA_LLM_API_KEY",
)

#: Matched per word, not per substring: "keywords" is not a credential, and a
#: substring test would redact it and teach nobody anything. A bare "email" is
#: redacted, because a contact address in a record is still someone's address.
_SECRET_WORDS = frozenset({
    "key", "keys", "token", "secret", "password", "passwd", "auth",
    "authorization", "email",
})


def redact(value: Any) -> Any:
    """Strip anything that looks like a credential, recursively.

    Values are replaced, not merely hidden, so a redaction cannot be
    defeated by a field name we did not anticipate.
    """
    secret_names = {name.lower() for name in SECRET_ENV_NAMES}
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lowered = str(key).lower()
            words = {w for w in re.split(r"[^a-z0-9]+", lowered) if w}
            if lowered in secret_names or (words & _SECRET_WORDS):
                out[key] = "<redacted>"
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


__all__ = ["SECRET_ENV_NAMES", "redact"]

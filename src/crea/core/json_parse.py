"""Parsing a model's JSON reply (CREA Stage 6.3).

One place that turns a model's text into a JSON object, tolerating a fenced
code block and nothing more. Returns None for anything else, so the caller can
count attempts and fail as `AI_OUTPUT_INVALID` rather than guess.
"""

from __future__ import annotations

import json


def parse_json_object(text: str, *, required_key: str | None = None) -> dict | None:
    """A JSON object from `text`, or None.

    Only a fenced ```json block or a bare object is accepted; no prose
    extraction. When `required_key` is given, the object must contain it.
    """
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        _, _, stripped = stripped.partition("\n")
        stripped = stripped.rsplit("```", 1)[0]
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if required_key is not None and required_key not in payload:
        return None
    return payload


__all__ = ["parse_json_object"]

"""Provenance: who computed what, with which parameters, on which version.

Every chemistry result carries this. It is what makes a claim auditable and a
golden fixture comparable across RDKit upgrades.
"""

from __future__ import annotations

import platform
import sys
import time
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Provenance(BaseModel):
    """How a value came to exist.

    Originally tool-oriented -- which code computed this, when, how long it
    took. The three source fields below were added rather than starting a
    second provenance type, because a record whose provenance is split across
    two classes is one nobody audits.

    They are the *source* half: which provider answered, which record of theirs
    it was, and a hash of the payload they sent. The tool half answers a
    different question -- how -- and both belong on the same object.
    """

    tool: str
    tool_version: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    computed_at: str = Field(default_factory=utc_now_iso)
    runtime_ms: int = 0
    python_version: str = Field(default_factory=lambda: sys.version.split()[0])
    platform: str = Field(default_factory=platform.system)

    source: str | None = None            # "openalex", "crossref", "user_provided", ...
    source_id: str | None = None          # that source's identifier for the record
    raw_payload_hash: str | None = None   # hash of the upstream payload, not of us


class timer:
    """Context manager returning a Provenance with runtime_ms filled in.

    `tool_version` is supplied by the caller, not looked up here: the `core`
    layer must not import RDKit, and it is the Chemical Core that knows which
    RDKit produced a number.
    """

    def __init__(self, tool: str, tool_version: str = "", **params: Any) -> None:
        self.prov = Provenance(tool=tool, tool_version=tool_version, params=dict(params))
        self._t0 = 0.0

    def __enter__(self) -> "timer":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.prov.runtime_ms = int((time.perf_counter() - self._t0) * 1000)

    def finish(self) -> Provenance:
        return self.prov

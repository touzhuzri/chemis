"""The tool registry: the only way anything actually happens.

A model names a registered tool and supplies a JSON object. The registry
validates the arguments against that tool's schema, executes it, and returns a
`ToolResult`. There is no `eval`, no `exec`, no dynamic import, and no path by
which model-supplied text becomes code.

Two boundaries this module exists to hold:

* **A model cannot report a tool result.** `ToolResult` is constructed here,
  from a real return value or a real exception. Nothing writes one from what a
  model said.
* **A model cannot reach the network or the filesystem.** Every path to either
  is behind a registered tool with a declared schema; a document tool only
  reads a `document_id` the operator already put in the library.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ValidationError

from crea.core.models.research import ToolResult, ToolStatus
from crea.llm.base import redact


@dataclass
class ToolSpec:
    """A tool's declared surface. The schema is what arguments are checked against.

    `permission` (CREA 4.0 Stage 6.4) is the tool's capability class. The
    evidence-aware agent holds only `read_only` tools: it can query chemistry,
    evidence and literature, and it cannot create, modify or delete anything.
    """

    name: str
    description: str
    arguments_model: type[BaseModel]
    run: Callable[..., Any]
    mutates_evidence: bool = False
    permission: str = "read_only"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> list[dict]:
        """What a model is told exists. Descriptions only; no logic."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "arguments": t.arguments_model.model_json_schema(),
            }
            for t in sorted(self._tools.values(), key=lambda t: t.name)
        ]

    def execute(self, name: str, arguments: dict[str, Any] | None = None,
                step_id: str = "") -> ToolResult:
        """Validate, run, and report. Never raises for a tool-level problem."""
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(
                tool=name, status=ToolStatus.ERROR,
                error_type="UnknownTool",
                error=f"no tool named {name!r}; available: {', '.join(self.names())}",
                step_id=step_id,
            )

        try:
            validated = spec.arguments_model.model_validate(arguments or {})
        except ValidationError as exc:
            return ToolResult(
                tool=name, status=ToolStatus.REJECTED,
                error_type="InvalidArguments",
                error=_readable(exc), step_id=step_id,
            )

        started = time.perf_counter()
        try:
            data = spec.run(**validated.model_dump())
        except Exception as exc:  # noqa: BLE001 - a tool failing is a result, not a crash
            return ToolResult(
                tool=name, status=ToolStatus.ERROR,
                error_type=type(exc).__name__, error=str(exc),
                duration_ms=int((time.perf_counter() - started) * 1000),
                step_id=step_id,
            )
        return ToolResult(
            tool=name, status=ToolStatus.OK,
            data=redact(data),
            duration_ms=int((time.perf_counter() - started) * 1000),
            step_id=step_id,
            evidence_ids=list(data.get("evidence_ids", []))
            if isinstance(data, dict) else [],
        )


def _readable(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(x) for x in error.get("loc", ())) or "arguments"
        parts.append(f"{location}: {error.get('msg')}")
    return "; ".join(parts) or "arguments failed validation"

"""The Chemistry Tool Contract: one boundary over every capability.

CREA 4.0 Stage C (§2/§3). A tool is a *capability boundary*, not a new
business layer: it declares an input schema, an output shape, capability
metadata, an execution mode, a timeout and a provenance policy -- and
then calls the service/provider/worker/RDKit machinery that already
exists. Tool code that re-implements chemistry is a bug.

    Tool -> Service -> Provider / Worker / RDKit

Layering of the contracts themselves (§4):

    Tool Contract  ->  Execution Adapter  ->  Worker Envelope

The agent sees only the Tool Contract. The `crea-engine/1` envelope
belongs to the execution transport and never surfaces upward. `ToolSpec`
remains the agent registry's shape; `as_agent_spec` bridges a
`ChemistryTool` into it -- one contract, one bridge, no second protocol.

The result vocabulary is the established `crea.core.models.research.
ToolResult` (extended additively for diagnostics/provenance): status +
data + diagnostics + provenance + normalized error. Never a bare dict for
an agent to guess at (§5).
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from crea.core.models.research import ToolResult, ToolStatus


class ExecutionMode(str, Enum):
    """Where a tool's work actually runs (§3)."""

    IN_PROCESS = "in_process"            # deterministic library call
    ISOLATED_WORKER = "isolated_worker"  # crea-engine/1 subprocess (Java/venv)
    REMOTE_PROVIDER = "remote_provider"  # an external source over a Transport


class ToolErrorCode(str, Enum):
    """The normalized failure vocabulary (§5). One code per *kind* of
    failure; the raw exception never crosses the boundary."""

    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    AMBIGUOUS = "ambiguous"
    TIMEOUT = "timeout"
    PROVIDER_FAILED = "provider_failed"
    INTERNAL = "internal"


class ToolError(Exception):
    """A tool-level failure with a code, not a mystery."""

    def __init__(self, code: ToolErrorCode, message: str,
                 diagnostics: list[str] | None = None) -> None:
        self.code = code
        self.message = message
        self.diagnostics = list(diagnostics or [])
        super().__init__(message)


#: Which normalized failures are *answers* (status stays OK; the data says
#: what happened) and which are failures. A miss is data; an outage is not.
_ANSWER_CODES = {ToolErrorCode.NOT_FOUND, ToolErrorCode.AMBIGUOUS}
_STATUS_FOR_CODE = {
    ToolErrorCode.INVALID_INPUT: ToolStatus.REJECTED,
    ToolErrorCode.UNAVAILABLE: ToolStatus.UNAVAILABLE,
    ToolErrorCode.TIMEOUT: ToolStatus.ERROR,
    ToolErrorCode.PROVIDER_FAILED: ToolStatus.ERROR,
    ToolErrorCode.INTERNAL: ToolStatus.ERROR,
}


class ChemistryTool:
    """One declared capability. Subclasses implement `execute`.

    A plain class, not a model: the metadata is a declaration (class
    attributes), and only the arguments/result travel as data.
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    version: ClassVar[str] = "1.0"
    capabilities: ClassVar[tuple[str, ...]] = ()
    execution_modes: ClassVar[tuple[ExecutionMode, ...]] = (
        ExecutionMode.IN_PROCESS,)
    timeout_s: ClassVar[float] = 60.0
    #: What `provenance` in every result will say about where answers came
    #: from. A declaration the implementation must honour.
    provenance_policy: ClassVar[str] = (
        "every answer carries its backend and source")
    arguments_model: ClassVar[type[BaseModel]]

    # -- the contract --------------------------------------------------------

    def invoke(self, step_id: str = "", **kwargs: Any) -> ToolResult:
        """Validate, execute, normalize. Never raises for a tool-level
        problem: the failure IS the ToolResult (§5)."""
        start = time.perf_counter()
        diagnostics: list[str] = []
        provenance: dict[str, Any] = {"tool": self.name,
                                      "version": self.version,
                                      "execution_modes": [m.value for m in
                                                          self.execution_modes]}
        try:
            validated = self.arguments_model.model_validate(kwargs)
        except ValidationError as exc:
            return self._failure(ToolErrorCode.INVALID_INPUT,
                                 f"arguments failed validation: {exc}",
                                 start, diagnostics, provenance)
        try:
            payload = self.execute(validated)
        except ToolError as exc:
            diagnostics.extend(exc.diagnostics)
            return self._failure(exc.code, exc.message, start,
                                 diagnostics, provenance)
        except Exception as exc:  # noqa: BLE001 - normalized at the boundary
            return self._failure(ToolErrorCode.INTERNAL,
                                 f"{type(exc).__name__}: {exc}",
                                 start, diagnostics, provenance)

        if isinstance(payload, ToolResult):
            result = payload
            result.duration_ms = int((time.perf_counter() - start) * 1000)
            return result
        data, extra = payload if isinstance(payload, tuple) else (payload, {})
        diagnostics.extend(extra.get("diagnostics", []))
        provenance.update(extra.get("provenance", {}))
        return ToolResult(
            tool=self.name, status=ToolStatus.OK, data=data,
            duration_ms=int((time.perf_counter() - start) * 1000),
            step_id=step_id, diagnostics=diagnostics, provenance=provenance)

    def execute(self, args: BaseModel):  # pragma: no cover - abstract
        raise NotImplementedError

    def _failure(self, code: ToolErrorCode, message: str, start: float,
                 diagnostics: list[str], provenance: dict) -> ToolResult:
        data = None
        if code in _ANSWER_CODES:
            data = {"status": code.value, "detail": message}
        return ToolResult(
            tool=self.name,
            status=_STATUS_FOR_CODE[code],
            data=data,
            error_type=code.value,
            error=message,
            duration_ms=int((time.perf_counter() - start) * 1000),
            diagnostics=diagnostics, provenance=provenance)

    def describe(self) -> dict:
        """What a caller (human or model) is told exists (§ discoverable)."""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "capabilities": list(self.capabilities),
            "execution_modes": [m.value for m in self.execution_modes],
            "timeout_s": self.timeout_s,
            "provenance_policy": self.provenance_policy,
            "arguments": self.arguments_model.model_json_schema(),
        }

    def as_agent_spec(self):
        """Bridge into the agent's `ToolRegistry` (§4): the agent sees a
        ToolSpec; the contract stays one."""
        from crea.agent.tools import ToolSpec

        def _run(**kwargs: Any):
            result = self.invoke(**kwargs)
            if result.status is ToolStatus.OK:
                return {
                    "result": result.data,
                    "diagnostics": result.diagnostics,
                    "provenance": result.provenance,
                }
            raise ToolError(
                ToolErrorCode(result.error_type or "internal"),
                result.error or "tool failed",
                result.diagnostics)

        return ToolSpec(
            name=self.name, description=self.description,
            arguments_model=self.arguments_model, run=_run)


__all__ = [
    "ChemistryTool",
    "ExecutionMode",
    "ToolError",
    "ToolErrorCode",
]

"""Execution adapters: where the work actually runs (§3/§4).

The boundary, in one picture:

    Tool Contract
         ↓
    Execution Adapter      <- this module
         ↓
    Worker Envelope (crea-engine/1) | Provider Transport | plain call

A tool never spawns a subprocess or opens a transport itself; it names an
execution mode and calls through one of these. Three modes, and no
fourth: `InProcessExecutor` (a deterministic library call),
`IsolatedWorkerExecutor` (the shared engine envelope -- OPSIN,
retrosynthesis, DECIMER), `RemoteProviderExecutor` (an external source
behind the provider protocols). Failures arrive as typed `ToolError`s
with normalized codes; raw exceptions never cross the boundary.
"""

from __future__ import annotations

from typing import Any, Callable

from crea.tools.contract import ToolError, ToolErrorCode


class InProcessExecutor:
    """A function call. Deterministic, local, no transport."""

    mode = "in_process"

    def run(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalized here
            raise ToolError(ToolErrorCode.INTERNAL,
                            f"{type(exc).__name__}: {exc}") from exc


class IsolatedWorkerExecutor:
    """The `crea-engine/1` subprocess line (§4).

    The envelope is the *transport*, never the contract: this adapter
    turns worker responses and typed worker failures into plain payloads
    and `ToolError`s, and nothing above ever sees `EngineResponse`.
    """

    mode = "isolated_worker"

    def __init__(self, client_factory: Callable[[], Any]) -> None:
        self._client_factory = client_factory

    def name_to_structure(self, name: str) -> dict:
        from crea.providers.worker_opsin import OpsinNameReader

        reader = OpsinNameReader(client=self._client_factory()
                                 if self._client_factory else None)
        reading = reader.read(name)
        if reading.get("error") and not reading.get("parsed"):
            code = (ToolErrorCode.UNAVAILABLE
                    if reading.get("unavailable")
                    else ToolErrorCode.PROVIDER_FAILED)
            raise ToolError(code, str(reading.get("error")),
                            diagnostics=[f"opsin: {reading.get('error')}"])
        return reading

    def retrosynthesize(self, request: Any) -> Any:
        client = self._client_factory()
        try:
            return client.retrosynthesize(
                target_smiles=request.target_smiles,
                max_routes=request.max_routes,
                max_transforms=request.max_transforms)
        except Exception as exc:  # noqa: BLE001 - typed at the boundary
            from crea.engine.errors import WorkerUnavailable

            code = (ToolErrorCode.UNAVAILABLE
                    if isinstance(exc, WorkerUnavailable)
                    else ToolErrorCode.PROVIDER_FAILED)
            raise ToolError(code, f"{type(exc).__name__}: {exc}") from exc


class RemoteProviderExecutor:
    """An external source behind the provider protocols (§8/§10).

    The provider answers or it does not: absence is an answer, failure to
    ask is an error, and neither is ever confused with the other -- the
    Phase 2 distinction, preserved through the tool boundary.
    """

    mode = "remote_provider"

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    @property
    def provider_name(self) -> str:
        return getattr(self._provider, "name", "remote")

    def status(self) -> dict:
        status = self._provider.status()
        return {"provider": self.provider_name,
                "availability": getattr(status, "availability", "unknown"),
                "reason": getattr(status, "reason", "")}

    def call(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalized here
            from crea.core.errors import ProviderUnavailable, ResourceNotFound

            if isinstance(exc, ResourceNotFound):
                raise ToolError(ToolErrorCode.NOT_FOUND,
                                f"{self.provider_name}: {exc}") from exc
            if isinstance(exc, ProviderUnavailable):
                raise ToolError(ToolErrorCode.UNAVAILABLE,
                                f"{self.provider_name}: {exc}") from exc
            raise ToolError(ToolErrorCode.PROVIDER_FAILED,
                            f"{self.provider_name}: "
                            f"{type(exc).__name__}: {exc}") from exc


__all__ = [
    "InProcessExecutor",
    "IsolatedWorkerExecutor",
    "RemoteProviderExecutor",
]

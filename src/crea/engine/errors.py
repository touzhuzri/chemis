"""Errors from the chemistry engine worker.

Typed, because "the worker died" and "the worker says its stock file is
missing" call for different responses, and a caller that cannot tell them apart
will retry the wrong one.
"""

from __future__ import annotations

from crea.engine.protocol import WorkerState


class EngineErrorBase(Exception):
    state: WorkerState = WorkerState.AVAILABLE


class WorkerUnavailable(EngineErrorBase):
    """No worker, or a worker whose packages will not import."""

    state = WorkerState.UNAVAILABLE


class WorkerNotConfigured(EngineErrorBase):
    """The worker runs, but has no policy, stock or configuration."""

    state = WorkerState.NOT_CONFIGURED


class WorkerIncompatibleDependency(EngineErrorBase):
    """The engine imports, but a dependency beside it is the wrong version.

    Its own state, because it is not `UNAVAILABLE`: everything is installed.
    It is also not something to fix by changing the main environment, which is
    the mistake this type exists to make visible.
    """

    state = WorkerState.INCOMPATIBLE

    def __init__(self, message: str, detail: dict | None = None,
                 error_type: str = "incompatible_dependency") -> None:
        super().__init__(message)
        self.detail = detail or {}
        self.error_type = error_type


class WorkerCrashed(EngineErrorBase):
    """It started and stopped without answering."""

    state = WorkerState.CRASHED


class WorkerTimeout(EngineErrorBase):
    """It started and went quiet."""

    state = WorkerState.TIMEOUT


class WorkerProtocolError(EngineErrorBase):
    """It answered, with something we cannot read."""

    state = WorkerState.PROTOCOL_ERROR


class WorkerReportedError(EngineErrorBase):
    """It answered with a structured error of its own."""

    state = WorkerState.AVAILABLE

    def __init__(self, message: str, detail: dict | None = None,
                 error_type: str = "EngineError") -> None:
        super().__init__(message)
        self.detail = detail or {}
        self.error_type = error_type


__all__ = [
    "EngineErrorBase",
    "WorkerCrashed",
    "WorkerIncompatibleDependency",
    "WorkerNotConfigured",
    "WorkerProtocolError",
    "WorkerReportedError",
    "WorkerTimeout",
    "WorkerUnavailable",
    "WorkerState",
]

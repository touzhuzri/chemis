"""The chemistry engine boundary.

CREA's chemistry core stays on the RDKit it is verified against. A retrosynthesis
engine that needs a different RDKit runs in a separate environment and is
reached over a pipe. Nothing in this package imports AiZynthFinder, and an
architecture test holds it to that.
"""

from crea.engine.client import EngineClient
from crea.engine.errors import (
    EngineErrorBase,
    WorkerCrashed,
    WorkerNotConfigured,
    WorkerProtocolError,
    WorkerReportedError,
    WorkerTimeout,
    WorkerUnavailable,
)
from crea.engine.protocol import (
    PROTOCOL_VERSION,
    EngineOp,
    EngineRequest,
    EngineResponse,
    WorkerState,
)

__all__ = [
    "EngineClient", "EngineErrorBase", "EngineOp", "EngineRequest",
    "EngineResponse", "PROTOCOL_VERSION", "WorkerCrashed", "WorkerNotConfigured",
    "WorkerProtocolError", "WorkerReportedError", "WorkerState",
    "WorkerTimeout", "WorkerUnavailable",
]

"""OPSIN over the engine worker pipe: the thin adapter (§9).

The main process never runs Java. This turns an `EngineClient` into the
one call that matters -- `read(name)` -> OPSIN's reading -- and maps the
engine's typed failures onto plain facts the name chain can record:
a missing runtime is `unavailable`, not a chemistry error.

Same worker protocol as retrosynthesis and DECIMER (no third protocol).
"""

from __future__ import annotations

import pathlib

from crea.engine.client import EngineClient
from crea.engine.errors import (
    WorkerCrashed,
    WorkerIncompatibleDependency,
    WorkerNotConfigured,
    WorkerProtocolError,
    WorkerReportedError,
    WorkerTimeout,
    WorkerUnavailable,
)

#: Where the OPSIN worker script lives in a checkout.
DEFAULT_OPSIN_WORKER = (
    pathlib.Path(__file__).resolve().parents[2].parent
    / "workers" / "chemistry" / "opsin_worker.py"
)


class OpsinNameReader:
    """One name in, OPSIN's reading out (or a typed non-answer)."""

    def __init__(self, client: EngineClient | None = None,
                 worker_script: str | None = None,
                 request_timeout: float = 35.0) -> None:
        self._client = client
        self._worker_script = worker_script or str(DEFAULT_OPSIN_WORKER)
        self._request_timeout = request_timeout

    def _build_client(self) -> EngineClient:
        if self._client is not None:
            return self._client
        return EngineClient(worker_script=self._worker_script,
                            request_timeout=self._request_timeout)

    def read(self, name: str) -> dict:
        """`{"parsed": bool, "smiles": str, ...}` or `{"error": code, ...}`.

        Never raises for a missing runtime: an absent OPSIN is an absent
        provider (the chain continues), and a parse OPSIN refuses is an
        answer ("not nomenclature I can read"), not an outage.
        """
        client = self._build_client()
        try:
            response = client.name_to_structure(name, expected_provider="opsin")
        except (WorkerUnavailable, WorkerNotConfigured,
                WorkerIncompatibleDependency) as exc:
            return {"parsed": False, "unavailable": True,
                    "error": f"{type(exc).__name__}: {exc}"}
        except (WorkerCrashed, WorkerTimeout, WorkerProtocolError,
                WorkerReportedError) as exc:
            return {"parsed": False, "error": f"{type(exc).__name__}: {exc}"}
        result = dict(response.result or {})
        result.setdefault("parsed", False)
        return result


__all__ = ["DEFAULT_OPSIN_WORKER", "OpsinNameReader"]

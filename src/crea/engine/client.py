"""Talking to the chemistry engine worker.

The main process never imports AiZynthFinder. It writes a request to a
subprocess's stdin, reads one line back, and turns whatever came into either a
result or a typed error.

One process per request in this version. A daemon is not needed yet and would
be a lifetime to manage before there is a reason; what the cost actually is
gets measured rather than assumed (`startup_seconds` on `info`).
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

from crea.engine.errors import (
    WorkerCrashed,
    WorkerIncompatibleDependency,
    WorkerNotConfigured,
    WorkerProtocolError,
    WorkerReportedError,
    WorkerTimeout,
    WorkerUnavailable,
)
from crea.engine.protocol import (
    DecimerRequest,
    EngineOp,
    EngineRequest,
    EngineResponse,
    OpsinNameRequest,
    PROTOCOL_VERSION,
)

#: The worker script, found relative to the repository so a checkout works
#: without configuration.
_DEFAULT_WORKER = pathlib.Path(__file__).resolve().parents[3] / "workers" / "chemistry" / "worker.py"

#: A start-up that takes minutes should fail visibly rather than hang a test.
DEFAULT_STARTUP_TIMEOUT = 120.0
DEFAULT_REQUEST_TIMEOUT = 600.0


class EngineClient:
    """Spawns the worker, sends one request, reads one answer."""

    def __init__(self,
                 python_executable: str | None = None,
                 worker_script: str | os.PathLike | None = None,
                 startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
                 request_timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        self.python_executable = python_executable or sys.executable
        self.worker_script = str(worker_script or _DEFAULT_WORKER)
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout

    # -- plumbing ----------------------------------------------------------

    def _spawn(self):
        script = pathlib.Path(self.worker_script)
        if not script.exists():
            raise WorkerUnavailable(
                f"worker script not found at {script}; the engine cannot run")
        try:
            return subprocess.Popen(
                [self.python_executable, str(script)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            raise WorkerUnavailable(
                f"could not start the worker with "
                f"{self.python_executable!r}: {exc}") from exc

    def _exchange(self, request) -> EngineResponse:
        """One process, one request, one answer, then we close it.

        Takes any request carrying `op`, `protocol`, `request_id` and
        `model_dump_json` -- `EngineRequest` or `DecimerRequest` -- because the
        envelope is one format for both workers. The branch below compares the
        op by *value* rather than by identity so that a second op enum can
        share `ping` without being the same class.
        """
        process = self._spawn()
        started = time.perf_counter()
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(request.model_dump_json() + "\n")
            process.stdin.flush()

            is_ping = str(getattr(request.op, "value", request.op)) == "ping"
            timeout = self.startup_timeout if is_ping else self.request_timeout
            line = _read_line(process.stdout, timeout)
            elapsed = time.perf_counter() - started

            if line is None:
                # Give it a moment to be reaped so the exit code is real rather
                # than "still running" at the instant we looked.
                try:
                    code = process.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    code = process.poll()
                stderr = _drain(process.stderr)
                raise WorkerCrashed(
                    f"the worker stopped before answering "
                    f"(exit code {code}). stderr: {stderr[:400]!r}")

            try:
                response = EngineResponse.model_validate_json(line)
            except Exception as exc:  # noqa: BLE001
                raise WorkerProtocolError(
                    f"the worker answered with something unparseable: "
                    f"{line[:200]!r}") from exc

            response.result.setdefault("startup_seconds", round(elapsed, 3))
            return response
        finally:
            _terminate(process)

    # -- operations --------------------------------------------------------

    def ping(self) -> EngineResponse:
        response = self._exchange(EngineRequest(op=EngineOp.PING,
                                                request_id=_new_request_id()))
        _raise_for(response)
        return response

    def info(self) -> EngineResponse:
        response = self._exchange(EngineRequest(op=EngineOp.INFO,
                                                request_id=_new_request_id()))
        _raise_for(response)
        return response

    def retrosynthesize(self, target_smiles: str, max_routes: int = 3,
                        max_transforms: int = 6,
                        config_file: str | None = None) -> EngineResponse:
        response = self._exchange(EngineRequest(
            op=EngineOp.RETROSYNTHESIZE,
            request_id=_new_request_id(),
            target_smiles=target_smiles,
            max_routes=max_routes,
            max_transforms=max_transforms,
            config_file=config_file,
            expected_provider="aizynthfinder",
        ))
        _raise_for(response)
        return response

    # -- Phase 3.6: image recognition, same envelope ---------------------

    def recognize_image(self, image_path: str, image_sha256: str = "",
                        source: str = "",
                        expected_recognizer: str = "decimer") -> EngineResponse:
        """Ask the DECIMER worker for one reading. A reading, not a structure.

        The response is whatever the recogniser emitted. Whether that string is
        a structure is decided by RDKit in the main process, and whether it is
        the compound the drawing depicts is not decided at all -- see
        `crea.core.models.identity.RecognitionStage`.
        """
        response = self._exchange(DecimerRequest(
            op=EngineOp.RECOGNIZE_IMAGE,
            request_id=_new_request_id(),
            image_path=image_path,
            image_sha256=image_sha256,
            source=source,
            expected_recognizer=expected_recognizer,
        ))
        _raise_for(response)
        return response

    def name_to_structure(self, chemical_name: str,
                          expected_provider: str = "opsin") -> EngineResponse:
        """Ask the OPSIN worker for one nomenclature parse (CREA 4.0 §9).

        A deterministic parser for systematic names, in its own runtime.
        The response is OPSIN's reading of the name -- the main process
        decides whether that reading is a structure (RDKit) and what it
        means (the semantic layer).
        """
        response = self._exchange(OpsinNameRequest(
            op=EngineOp.NAME_TO_STRUCTURE,
            request_id=_new_request_id(),
            chemical_name=chemical_name,
            expected_provider=expected_provider,
        ))
        _raise_for(response)
        return response


# -- helpers -----------------------------------------------------------------


def _new_request_id() -> str:
    """A fresh id per request. Not a secret and not a session token -- it only
    has to be unique enough to match an answer to the line that asked for it.
    """
    import uuid

    return uuid.uuid4().hex[:16]


def _raise_for(response: EngineResponse) -> None:
    if response.ok:
        return
    error = response.error
    if error is None:
        raise WorkerProtocolError(
            "the worker reported a failure with no error body")
    kind = (error.error_type or "").lower()
    if "incompatible" in kind or "dependency" in kind:
        # Distinct from `unavailable` on purpose: the engine is present and
        # cannot work beside what else is installed. That is a different
        # problem with a different fix, and conflating them is how a caller
        # ends up downgrading an RDKit the rest of the project is verified
        # against.
        raise WorkerIncompatibleDependency(error.message, error.detail,
                                           error.error_type)
    if "unavailable" in kind:
        raise WorkerUnavailable(error.message)
    if "not_configured" in kind or "not configured" in kind:
        raise WorkerNotConfigured(error.message, error.detail, error.error_type)
    raise WorkerReportedError(error.message, error.detail, error.error_type)


def _read_line(stream, timeout: float) -> str | None:
    """Read one line, giving up after `timeout`.

    `readline` on a pipe has no timeout of its own, so the wait is done with
    a reader thread. A worker that hangs must produce a `WorkerTimeout`, not a
    stuck test run.
    """
    import threading

    box: dict[str, str | None] = {}

    def read() -> None:
        try:
            box["line"] = stream.readline()
        except Exception:  # noqa: BLE001
            box["line"] = None

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise WorkerTimeout(
            f"the worker did not answer within {timeout:.0f}s")
    return (box.get("line") or "").strip() or None


def _drain(stream) -> str:
    if stream is None:
        return ""
    try:
        return stream.read() or ""
    except Exception:  # noqa: BLE001
        return ""


def _terminate(process) -> None:
    """Stop the child, then close its pipes.

    The order matters and was wrong at first. Closing stdout while the reader
    thread is blocked on it does not wake that thread, and the interpreter
    then waits for it at shutdown -- so a worker that timed out hung the test
    run instead of failing it. Kill first; the pipes follow.
    """
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
    for close in (getattr(process, "stdin", None),
                  getattr(process, "stdout", None),
                  getattr(process, "stderr", None)):
        if close is not None:
            try:
                close.close()
            except Exception:  # noqa: BLE001
                pass


__all__ = ["EngineClient", "PROTOCOL_VERSION"]

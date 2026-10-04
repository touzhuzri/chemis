"""The chemical structure image recognition worker. Runs in its own environment.

This is the only file in the repository that imports DECIMER, and it runs under
a Python that is not the one CREA runs under. That separation is the point, and
it is the same separation AiZynthFinder already required: DECIMER pulls in
TensorFlow and a set of trained weights, and none of that belongs in an
environment whose RDKit is pinned to 2026.3.6 and verified against a test
suite. Neither process sees the other's packages.

CREA -> stdin JSONL -> this process -> stdout JSONL -> CREA. Diagnostics go to
stderr and never to stdout: a single `print()` of a warning would land in the
middle of a JSON line and turn a working recogniser into a protocol error.

Protocol: `crea-engine/1`, one request per process in this version. See
`crea/engine/protocol.py`.

**What this worker is not.** It is not a chemistry tool. It reads pixels and
emits a string it believes is a SMILES. Whether that string is a structure is
RDKit's question and is answered in the main process; whether it is the
compound the drawing depicts is answered by nobody here. The output of this
worker is `IMAGE_RECOGNIZED_CANDIDATE` and never a resolved identity.

**What it may not do.** No `eval`, no `exec`, no `importlib` on a
user-supplied name, no `subprocess`, no network. The input is a file path and
nothing about it selects code. This is not caution for its own sake: a worker
that imported whatever a request named would make a chemical structure image a
remote code execution surface.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import time
import traceback

PROTOCOL = "crea-engine/1"

#: The image bytes are expected under a temporary directory the caller owns.
#: A path from a request is only ever opened for reading, and only if it is a
#: file -- never a directory, never a device, never a named pipe.
_MAX_IMAGE_BYTES = 40 * 1024 * 1024
_ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(message: str) -> None:
    """Diagnostics go to stderr, never stdout. See the module docstring."""
    sys.stderr.write(str(message).rstrip() + "\n")
    sys.stderr.flush()


def _versions() -> dict:
    """What actually answered. Not what a lock file promised.

    The same rule as the AiZynthFinder worker: versions are measured here, in
    the runtime that ran the model, because a version recorded in the calling
    process would be a version of a different interpreter.

    Each package is imported by a name written in this file rather than by one
    computed in a loop. That is not tidiness: §14 forbids dynamic import, and a
    loop over a tuple is the shape that would hide a request-supplied name if
    one ever reached it. Spelling the imports out makes the static check that
    enforces that rule able to see every one of them.
    """
    out: dict = {"python": sys.version.split()[0], "protocol": PROTOCOL}

    try:
        import DECIMER

        out["DECIMER"] = str(getattr(DECIMER, "__version__", "present"))
    except Exception as exc:  # noqa: BLE001 - a missing package is information
        out["DECIMER"] = f"unavailable: {type(exc).__name__}"

    try:
        import PIL

        out["Pillow"] = str(getattr(PIL, "__version__", "present"))
    except Exception as exc:  # noqa: BLE001
        out["Pillow"] = f"unavailable: {type(exc).__name__}"

    try:
        import numpy

        out["numpy"] = str(getattr(numpy, "__version__", "present"))
    except Exception as exc:  # noqa: BLE001
        out["numpy"] = f"unavailable: {type(exc).__name__}"

    try:
        import tensorflow

        out["tensorflow"] = str(getattr(tensorflow, "__version__", "present"))
    except Exception as exc:  # noqa: BLE001
        out["tensorflow"] = f"unavailable: {type(exc).__name__}"

    return out


def _ok(result: dict, environment: dict, request_id: str) -> None:
    _emit({"protocol": PROTOCOL, "ok": True, "result": result,
           "error": None, "environment": environment,
           "request_id": request_id})


def _fail(message: str, error_type: str, environment: dict,
          request_id: str, detail: dict | None = None) -> None:
    _emit({"protocol": PROTOCOL, "ok": False, "result": {},
           "error": {"error_type": error_type, "message": message,
                      "detail": detail or {}},
           "environment": environment, "request_id": request_id})


# --- image handling ---------------------------------------------------------


def _read_image_bytes(raw_path: str) -> bytes:
    """Open the image, with the narrowest rules a path needs.

    A path arrives from a request, so it is treated as data and not as an
    instruction. The checks below are the reason this worker can say it does
    nothing dangerous with its input: it opens one regular file, under a size
    cap, with a known suffix, for reading.
    """
    if not raw_path or not isinstance(raw_path, str):
        raise ValueError("image_path is required")
    if "\x00" in raw_path or "\n" in raw_path or "\r" in raw_path:
        raise ValueError("image_path contains a control character")

    path = pathlib.Path(raw_path)
    if not path.exists():
        raise FileNotFoundError(f"no such image file: {path.name}")
    if not path.is_file():
        raise ValueError("image_path must name a regular file")
    if path.suffix.lower() not in _ALLOWED_SUFFIXES:
        raise ValueError(
            f"unsupported image type {path.suffix!r}; expected one of "
            f"{sorted(_ALLOWED_SUFFIXES)}"
        )
    size = path.stat().st_size
    if size > _MAX_IMAGE_BYTES:
        raise ValueError(
            f"image is {size} bytes, over the {_MAX_IMAGE_BYTES} byte limit"
        )
    return path.read_bytes()


def _verify_sha256(data: bytes, expected: str) -> str:
    """Confirm the file we opened is the file the caller meant.

    A path is a name and a name can point at something else. The hash is the
    identity, and it is checked here rather than assumed.
    """
    actual = hashlib.sha256(data).hexdigest()
    if expected and actual != expected:
        raise ValueError(
            f"image_sha256 mismatch: the file at the given path hashes to "
            f"{actual[:16]}... not {expected[:16]}..."
        )
    return actual


def _decode(data: bytes):
    """Bytes to an array the recogniser accepts. No decoding of anything else."""
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(data)) as img:
        return img.convert("RGB")


# --- the recogniser ---------------------------------------------------------


def _recognize(image) -> str:
    """One reading of one image.

    DECIMER's public entry point is `DECIMER.decimer_predict`, which takes a
    PIL image and returns a SMILES string. The import is done here rather than
    at module level so that `op=info` can report on an environment where the
    package is absent without failing first.
    """
    from DECIMER import decimer_predict

    return str(decimer_predict(image) or "")


# --- operations -------------------------------------------------------------


def op_info(env: dict, request_id: str) -> None:
    decimer = env.get("DECIMER", "")
    decimer_ok = isinstance(decimer, str) and not decimer.startswith("unavailable")
    result = {
        "capabilities": ["recognize_image"],
        "recognizer": "decimer",
        "usable": decimer_ok,
        "requires_config": False,
    }
    if not decimer_ok:
        result["problem"] = (
            "DECIMER is not importable in this environment; image recognition "
            "is optional and Phase 3.6 does not depend on it being present"
        )
    _ok(result, env, request_id)


def op_recognize(request: dict, env: dict, request_id: str) -> None:
    raw_path = request.get("image_path", "") or ""
    expected_sha = str(request.get("image_sha256", "") or "")

    try:
        data = _read_image_bytes(raw_path)
        actual_sha = _verify_sha256(data, expected_sha)
    except (ValueError, FileNotFoundError, OSError) as exc:
        _fail(str(exc), "InvalidImage", env, request_id)
        return

    try:
        image = _decode(data)
    except Exception as exc:  # noqa: BLE001 - a decoder raises a family of these
        _fail(f"the image could not be decoded: {exc}", "InvalidImage", env, request_id)
        return

    started = time.perf_counter()
    try:
        raw_smiles = _recognize(image)
    except Exception as exc:  # noqa: BLE001 - a model can fail in many ways
        _log(traceback.format_exc())
        _fail(
            f"the recogniser failed: {type(exc).__name__}: {exc}",
            "RecognizerFailed", env, request_id,
            detail={"recognizer": "decimer"},
        )
        return
    elapsed = time.perf_counter() - started

    # The result is a READING. `raw_smiles` is exactly what the model emitted,
    # with no cleanup, no repair and no validation -- those are the main
    # process's job, and doing any of them here would hide what the model
    # actually produced behind something that looks more like an answer.
    _ok({
        "raw_smiles": raw_smiles,
        "image_sha256": actual_sha,
        "recognizer": "decimer",
        "recognizer_version": env.get("DECIMER", "unknown"),
        "model_version": env.get("tensorflow", "unknown"),
        "elapsed_seconds": round(elapsed, 3),
        "image_bytes": len(data),
        "source": str(request.get("source", "") or ""),
        "stage": "IMAGE_RECOGNIZED_CANDIDATE",
    }, env, request_id)


# --- entry point ------------------------------------------------------------


def main() -> int:
    """Read one request, answer one response, exit.

    One request per process, like the AiZynthFinder worker. A daemon would add
    a lifetime to manage before there is a reason, and the startup cost is
    measured on `info` rather than assumed away.
    """
    environment = _versions()
    request_id = ""

    try:
        line = sys.stdin.readline()
    except Exception as exc:  # noqa: BLE001
        _fail(f"could not read a request: {exc}", "WorkerProtocolError",
              environment, request_id)
        return 1

    if not line or not line.strip():
        _fail("no request was written to stdin", "WorkerProtocolError",
              environment, request_id)
        return 1

    try:
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ValueError("the request must be a JSON object")
    except Exception as exc:  # noqa: BLE001
        _fail(f"the request was not readable JSON: {exc}", "WorkerProtocolError",
              environment, request_id)
        return 1

    request_id = str(request.get("request_id", "") or "")
    protocol = str(request.get("protocol", PROTOCOL) or PROTOCOL)
    if protocol != PROTOCOL:
        _fail(
            f"protocol {protocol!r} is not the one this worker speaks "
            f"({PROTOCOL!r})",
            "WorkerProtocolError", environment, request_id,
        )
        return 1

    op = str(request.get("op", "") or "")
    _log(f"decimer worker: op={op!r} request_id={request_id!r}")

    if op == "ping":
        _ok({"capabilities": ["recognize_image"], "recognizer": "decimer"},
            environment, request_id)
        return 0
    if op == "info":
        op_info(environment, request_id)
        return 0
    if op == "recognize_image":
        op_recognize(request, environment, request_id)
        return 0

    _fail(f"unknown op {op!r}", "WorkerProtocolError", environment, request_id)
    return 1


if __name__ == "__main__":
    sys.exit(main())

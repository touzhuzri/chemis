"""The nomenclature worker. OPSIN runs in its own runtime, outside CREA.

CREA 4.0 Stage B-1 (§9). This is the only place the OPSIN runtime is
touched: a Java process behind the shared `crea-engine/1` envelope, exactly
like the AiZynthFinder and DECIMER workers. One wire format, three
workers -- the third one does not get a third protocol.

    CREA -> stdin JSONL -> this process -> java -jar opsin -> stdout JSONL

What this worker is not: it does not decide identity. OPSIN parses
systematic nomenclature deterministically; whether the emitted structure is
the compound the user meant is the semantic layer's question, on evidence.

What it may not do: no `eval`, no `exec`, no network, no shell. The name
travels as data (stdin/argv list, never `shell=True`). A missing Java or a
missing OPSIN jar is `ok: false` with a typed error -- an unavailable
parser, not a chemistry failure.

Configuration (environment, all optional):
    CREA_OPSIN_HOME  directory holding the OPSIN jars
                     (default: runtime/opsin)
    CREA_JAVA        java executable (default: `java`)

The invocation below is **empirically verified** (2026-10-03, Java 21,
OPSIN 2.9.0 jars from Maven Central): main class
`uk.ac.cam.ch.wwmm.opsin.Cli`, names newline-delimited on stdin, `-o smi`
for SMILES output, an unparseable name yields empty stdout and rc=0 --
which is an ANSWER ("not nomenclature I can read"), not an outage.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

PROTOCOL = "crea-engine/1"

DEFAULT_OPSIN_HOME = pathlib.Path("runtime") / "opsin"
OPSIN_MAIN_CLASS = "uk.ac.cam.ch.wwmm.opsin.Cli"
_MAX_NAME_CHARS = 4000


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _fail(request_id: str, code: str, message: str, environment: dict) -> None:
    _emit({
        "protocol": PROTOCOL,
        "ok": False,
        "request_id": request_id,
        "result": {},
        "error": {"code": code, "message": message},
        "environment": environment,
    })


def _classpath(home: pathlib.Path) -> str:
    jars = sorted(home.glob("*.jar"))
    return ";".join(str(j) for j in jars)


def _environment(home: pathlib.Path, java: str) -> dict:
    jars = sorted(home.glob("*.jar"))
    return {
        "worker": "opsin",
        "java": java,
        "opsin_home": str(home),
        "jars": [j.name for j in jars],
        "runtime_ready": bool(jars) and shutil.which(java) is not None,
    }


def main() -> int:
    line = sys.stdin.readline()
    if not line:
        return 1
    try:
        request = json.loads(line)
    except ValueError:
        _fail("", "PROTOCOL_ERROR", "request was not JSON", {})
        return 1

    request_id = str(request.get("request_id", ""))
    op = str(request.get("op", ""))
    home = pathlib.Path(os.environ.get("CREA_OPSIN_HOME") or DEFAULT_OPSIN_HOME)
    java = os.environ.get("CREA_JAVA") or "java"
    environment = _environment(home, java)

    if op != "name_to_structure":
        _fail(request_id, "UNSUPPORTED_OP", f"op {op!r} is not this worker's",
              environment)
        return 1

    name = str(request.get("chemical_name", "")).strip()
    if not name:
        _fail(request_id, "EMPTY_NAME", "no chemical name was supplied",
              environment)
        return 1
    if len(name) > _MAX_NAME_CHARS:
        _fail(request_id, "NAME_TOO_LONG",
              f"chemical name exceeds {_MAX_NAME_CHARS} characters",
              environment)
        return 1

    if shutil.which(java) is None or not environment["jars"]:
        _fail(request_id, "WORKER_UNAVAILABLE",
              "OPSIN runtime is not configured (java or opsin jars missing)",
              environment)
        return 1

    # Fixed command vector, name as data. No shell, ever.
    try:
        completed = subprocess.run(
            [java, "-cp", _classpath(home), OPSIN_MAIN_CLASS, "-o", "smi"],
            input=name + "\n", capture_output=True, text=True,
            timeout=30, check=False)
    except subprocess.TimeoutExpired:
        _fail(request_id, "WORKER_TIMEOUT", "OPSIN did not answer in 30s",
              environment)
        return 1
    except OSError as exc:
        _fail(request_id, "WORKER_UNAVAILABLE", f"could not run OPSIN: {exc}",
              environment)
        return 1

    output = (completed.stdout or "").strip()
    if completed.returncode != 0 or not output:
        # OPSIN refusing a name is an ANSWER ("not systematic nomenclature
        # I can parse"), not an outage.
        _emit({
            "protocol": PROTOCOL,
            "ok": True,
            "request_id": request_id,
            "result": {"parsed": False, "name": name,
                       "stderr": (completed.stderr or "").strip()[:500]},
            "error": None,
            "environment": environment,
        })
        return 0

    smiles = output.splitlines()[0].strip()
    _emit({
        "protocol": PROTOCOL,
        "ok": True,
        "request_id": request_id,
        "result": {"parsed": True, "name": name, "smiles": smiles,
                   "provider": "opsin"},
        "error": None,
        "environment": environment,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

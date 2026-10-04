"""The wire format between CREA and the chemistry engine worker.

Deliberately small and versioned. Newline-delimited JSON on stdin/stdout is
enough for a request, a result and an error, and it means the worker can be any
Python with the right packages -- not a service, not a library CREA imports.

Two things this format is for:

* **Isolation.** AiZynthFinder needs an RDKit this project cannot use. Keeping
  it behind a subprocess is what lets both be true at once.
* **Honest failure.** A worker that crashes, times out or returns something
  unparseable produces a typed error here rather than an exception three
  layers up that looks like a chemistry problem.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

PROTOCOL_VERSION = "crea-engine/1"


class EngineOp(str, Enum):
    PING = "ping"                       # is the worker alive and importable
    INFO = "info"                       # versions and where the data lives
    RETROSYNTHESIZE = "retrosynthesize"   # the actual work
    # Phase 3.6. The DECIMER worker is a different program in a different
    # runtime speaking this same envelope. One wire format for two workers is
    # deliberate: the guarantees below (request_id, structured error, a typed
    # timeout, a typed crash) are exactly the ones §14 requires of the image
    # path, and writing them down twice is how the second copy loses one.
    RECOGNIZE_IMAGE = "recognize_image"
    # CREA 4.0 Stage B-1. OPSIN: systematic name -> structure, in an
    # isolated Java runtime. Third worker, SAME envelope (§9: no third
    # protocol). The Java/Maven runtime stays outside the Python process.
    NAME_TO_STRUCTURE = "name_to_structure"


class OpsinNameRequest(BaseModel):
    """One nomenclature parse request, in the shared envelope.

    A name goes in; a structure notation comes back or an error does. The
    worker never ranks, never guesses among readings, and never repairs a
    name -- OPSIN parses systematic nomenclature or it does not.
    """

    op: EngineOp = EngineOp.NAME_TO_STRUCTURE
    protocol: str = PROTOCOL_VERSION
    request_id: str = ""
    chemical_name: str = ""
    #: What the caller expects to run; the worker reports what actually did.
    expected_provider: str = "opsin"


class EngineRequest(BaseModel):
    op: EngineOp
    protocol: str = PROTOCOL_VERSION
    #: Correlates a response with the request that asked for it. One process
    #: serves one request in this version, so it is not load-bearing yet -- but
    #: a request id that is not echoed back is the one thing you cannot add
    #: later without changing the format.
    request_id: str = ""
    target_smiles: str = ""
    max_routes: int = 3
    max_transforms: int = 6
    config_file: str | None = None
    #: Passed through so a caller can say which provider it thinks it is talking
    #: to. The worker verifies it against what it actually imported.
    expected_provider: str = ""


class DecimerRequest(BaseModel):
    """One image recognition request, in the same envelope as everything else.

    The image travels as bytes in a temporary file path rather than base64 in
    the request line, for two reasons. A multi-megabyte structure drawing
    would otherwise be a multi-megabyte JSON string on a pipe. And a path is
    what the worker can hand straight to a decoder, whereas base64 would mean
    a second round of handling in a process whose only job is to run a model.

    `image_sha256` travels with it so the worker can verify that the file it
    opened is the file the caller meant. A path is a name; a hash is identity.
    """

    op: EngineOp = EngineOp.RECOGNIZE_IMAGE
    protocol: str = PROTOCOL_VERSION
    request_id: str = ""
    image_path: str = ""
    image_sha256: str = ""
    #: The recogniser the caller expects to answer. The worker reports what
    #: actually answered; the two disagreeing is information, not an error.
    expected_recognizer: str = "decimer"
    #: Optional; the worker records it verbatim so a report can say which
    #: drawing it was reading without carrying the pixels.
    source: str = ""


class EngineError(BaseModel):
    error_type: str = "EngineError"
    message: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class EngineResponse(BaseModel):
    protocol: str = PROTOCOL_VERSION
    ok: bool = False
    result: dict[str, Any] = Field(default_factory=dict)
    error: EngineError | None = None
    #: Present on INFO, and on every response: what actually ran.
    environment: dict[str, Any] = Field(default_factory=dict)
    request_id: str = ""


class WorkerState(str, Enum):
    """What the caller should believe about the worker."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"     # not there, or its packages are not importable
    NOT_CONFIGURED = "not_configured"  # importable, no policy/stock/config
    INCOMPATIBLE = "incompatible"    # importable, but a dependency conflicts
    CRASHED = "crashed"              # it started and died
    TIMEOUT = "timeout"              # it started and stopped answering
    PROTOCOL_ERROR = "protocol_error"  # it answered with something we cannot read
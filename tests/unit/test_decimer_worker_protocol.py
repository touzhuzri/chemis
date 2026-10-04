"""The image recognition worker boundary: protocol, and what may not happen.

§14's requirements in their mechanical form. The worker is a separate program
in a separate runtime, so every guarantee has to be observable from this side
of the pipe -- a request id that comes back, an error that is structured rather
than a traceback, a crash that is a typed failure rather than a hung test, and
an input that can only ever be read as data.

The last of those is the one with teeth. A worker that imported whatever a
request named, or `eval`ed a string that arrived with an image, would make a
chemical structure drawing a remote code execution surface. So there is a
static check here as well as behavioural ones: the dangerous operations must
be absent from the file, not merely unused.

Everything runs offline under the main interpreter. DECIMER is not installed
here and must not be -- §12 -- so the recogniser's *success* path is covered
by the adapter's contract tests with a fake client, and its *absence* path is
covered here against the real worker.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

from crea.engine.client import EngineClient
from crea.engine.errors import WorkerProtocolError, WorkerReportedError
from crea.engine.protocol import PROTOCOL_VERSION, EngineOp, EngineResponse

WORKERS = pathlib.Path(__file__).resolve().parents[2] / "workers" / "chemistry"
WORKER = WORKERS / "decimer_worker.py"


def _load_worker():
    """The worker module, for testing its helpers directly.

    Loaded by path because `workers/` is not a package. This is test-side
    machinery; the worker itself must never do this with a name that came from
    a request, which is precisely what the static check below asserts.
    """
    spec = importlib.util.spec_from_file_location("decimer_worker", WORKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def worker():
    return _load_worker()


def _exchange(request: dict) -> EngineResponse:
    """Drive the real worker over a pipe, exactly as `EngineClient` does."""
    process = subprocess.Popen(
        [sys.executable, str(WORKER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
    )
    try:
        out, err = process.communicate(json.dumps(request) + "\n", timeout=60)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a hang
            process.kill()
    assert out, f"the worker answered with nothing. stderr: {err!r}"
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, f"one request, one answer; got {lines!r}"
    return EngineResponse.model_validate_json(lines[0])


@pytest.fixture
def tiny_png(tmp_path):
    """A real, tiny PNG the worker can decode. Nothing is mocked about it."""
    from io import BytesIO

    from PIL import Image

    path = tmp_path / "drawing.png"
    buffer = BytesIO()
    Image.new("RGB", (4, 4), (255, 255, 255)).save(buffer, format="PNG")
    path.write_bytes(buffer.getvalue())
    return path


# --- the envelope -----------------------------------------------------------


def test_a_decimer_request_round_trips():
    from crea.engine.protocol import DecimerRequest

    request = DecimerRequest(request_id="abc", image_path="/tmp/x.png",
                             image_sha256="deadbeef")
    again = DecimerRequest.model_validate_json(request.model_dump_json())
    assert again == request
    assert again.op is EngineOp.RECOGNIZE_IMAGE
    assert again.protocol == PROTOCOL_VERSION
    # the expected recogniser is named, so a disagreement is observable
    assert again.expected_recognizer == "decimer"


def test_recognize_image_is_a_first_class_operation():
    assert EngineOp.RECOGNIZE_IMAGE.value == "recognize_image"
    # and it shares the envelope with the engine rather than inventing one
    assert EngineOp.RECOGNIZE_IMAGE in EngineOp


# --- the real worker -------------------------------------------------------


def test_the_worker_answers_a_ping_without_decimer_installed():
    """The worker must be examinable in an environment that has no model.

    If a bare `ping` needed TensorFlow, the only environment that could test
    the worker would be the one that must not have TensorFlow in it.
    """
    response = _exchange({"op": "ping", "protocol": PROTOCOL_VERSION,
                          "request_id": "r-ping"})
    assert response.ok is True
    assert response.request_id == "r-ping"
    assert response.result["recognizer"] == "decimer"
    assert response.environment["protocol"] == PROTOCOL_VERSION


def test_info_reports_the_recogniser_as_absent_rather_than_failing():
    """The honest answer about a missing optional component is `usable: false`."""
    response = _exchange({"op": "info", "protocol": PROTOCOL_VERSION,
                          "request_id": "r-info"})
    assert response.ok is True
    assert response.result["capabilities"] == ["recognize_image"]
    assert response.result["usable"] is False
    assert "problem" in response.result
    # the environment is measured here, in the process that answered
    assert response.environment["DECIMER"].startswith("unavailable")
    assert response.environment["python"] == sys.version.split()[0]


def test_a_recognition_without_a_model_is_a_structured_failure(tiny_png):
    """§14's structured error, not a traceback on stdout and not silence.

    The image is valid and decodes; the only thing missing is the recogniser.
    The answer has to distinguish that from a bad image, because the fix for
    one is "install the runtime" and the fix for the other is "ask for a better
    drawing".
    """
    response = _exchange({
        "op": "recognize_image", "protocol": PROTOCOL_VERSION,
        "request_id": "r-nomodel", "image_path": str(tiny_png),
        "expected_recognizer": "decimer",
    })
    assert response.ok is False
    assert response.error is not None
    assert response.error.error_type == "RecognizerFailed"
    assert "decimer" in response.error.message.lower() or "DECIMER" in response.error.message
    assert response.request_id == "r-nomodel"


# --- the pipe is a protocol, not a wish ------------------------------------


def test_empty_stdin_is_a_protocol_error_not_a_crash():
    process = subprocess.Popen(
        [sys.executable, str(WORKER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    out, _ = process.communicate("", timeout=60)
    response = EngineResponse.model_validate_json(out.splitlines()[0])
    assert response.ok is False
    assert response.error.error_type == "WorkerProtocolError"


def test_malformed_json_is_a_protocol_error():
    process = subprocess.Popen(
        [sys.executable, str(WORKER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    out, _ = process.communicate("{not json at all\n", timeout=60)
    response = EngineResponse.model_validate_json(out.splitlines()[0])
    assert response.ok is False
    assert response.error.error_type == "WorkerProtocolError"
    assert "JSON" in response.error.message


def test_an_unknown_op_is_refused():
    response = _exchange({"op": "download_the_internet",
                          "protocol": PROTOCOL_VERSION,
                          "request_id": "r-nope"})
    assert response.ok is False
    assert response.error.error_type == "WorkerProtocolError"
    assert "download_the_internet" in response.error.message


def test_a_foreign_protocol_version_is_refused():
    """A versioned envelope that ignores its own version is not versioned."""
    response = _exchange({"op": "ping", "protocol": "crea-engine/99",
                          "request_id": "r-ver"})
    assert response.ok is False
    assert response.error.error_type == "WorkerProtocolError"
    assert "crea-engine/99" in response.error.message


def test_the_request_id_is_echoed_on_every_answer():
    for op in ("ping", "info"):
        response = _exchange({"op": op, "protocol": PROTOCOL_VERSION,
                              "request_id": f"id-{op}"})
        assert response.request_id == f"id-{op}"


def test_an_unparseable_worker_answer_is_a_client_side_protocol_error(tmp_path):
    """The client refuses an answer it cannot read, rather than guessing.

    A worker that prints a warning and then JSON is the failure this guards:
    the first line is not a response, and treating it as one would report a
    chemistry result that nobody produced.
    """
    script = tmp_path / "bad_worker.py"
    script.write_text(
        "import sys\n"
        "print('Warning: something noisy')\n"
        "print('{\"ok\": true}')\n",
        encoding="utf-8",
    )
    client = EngineClient(worker_script=str(script))
    with pytest.raises(WorkerProtocolError):
        client.recognize_image(image_path="whatever.png")


# --- image handling rules --------------------------------------------------


def test_a_missing_image_is_an_invalid_image_not_a_crash(worker, tmp_path):
    with pytest.raises(FileNotFoundError):
        worker._read_image_bytes(str(tmp_path / "absent.png"))


def test_a_directory_is_refused(worker, tmp_path):
    (tmp_path / "sub").mkdir()
    with pytest.raises(ValueError, match="regular file"):
        worker._read_image_bytes(str(tmp_path / "sub"))


def test_an_unsupported_suffix_is_refused(worker, tmp_path):
    script = tmp_path / "payload.py"
    script.write_text("print('hi')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported image type"):
        worker._read_image_bytes(str(script))


def test_a_path_with_a_control_character_is_refused(worker):
    with pytest.raises(ValueError, match="control character"):
        worker._read_image_bytes("some/thing\n.png")
    with pytest.raises(ValueError, match="control character"):
        worker._read_image_bytes("some/thing\x00.png")


def test_an_empty_path_is_refused(worker):
    with pytest.raises(ValueError, match="required"):
        worker._read_image_bytes("")


def test_an_oversized_image_is_refused(worker, tiny_png, monkeypatch):
    monkeypatch.setattr(worker, "_MAX_IMAGE_BYTES", 8)
    with pytest.raises(ValueError, match="over the"):
        worker._read_image_bytes(str(tiny_png))


def test_the_sha256_of_the_file_actually_read_is_checked(worker, tiny_png):
    """A path is a name; the hash is the identity (§16).

    The worker verifies rather than trusts, so a swapped file cannot be
    silently attributed to the image the caller meant.
    """
    data = tiny_png.read_bytes()
    good = worker._verify_sha256(data, "")
    assert len(good) == 64
    assert worker._verify_sha256(data, good) == good
    with pytest.raises(ValueError, match="mismatch"):
        worker._verify_sha256(data, "0" * 64)


def test_a_recognition_answer_carries_the_reading_and_nothing_more(tiny_png):
    """What comes back is `IMAGE_RECOGNIZED_CANDIDATE`, and says so.

    Even on the success path the worker does not call the string a structure.
    `stage` names it as a reading, and there is no field in the result that
    could be mistaken for an identity verdict.
    """
    response = _exchange({
        "op": "recognize_image", "protocol": PROTOCOL_VERSION,
        "request_id": "r-stage", "image_path": str(tiny_png),
    })
    # with no recogniser installed the call fails before it can report a stage,
    # which is itself the required behaviour; when one IS installed the result
    # must name the stage
    if response.ok:
        assert response.result["stage"] == "IMAGE_RECOGNIZED_CANDIDATE"
        assert "status" not in response.result
        assert "resolved" not in response.result
    else:
        assert response.error.error_type == "RecognizerFailed"


# --- §14: what the worker may not do with its input ------------------------


def test_the_worker_never_evals_execs_or_imports_what_a_request_names():
    """§14's hard rule, checked where a later edit would break it.

    禁止 eval / exec / dynamic import from user input / arbitrary shell
    execution. Behavioural tests can only prove a particular string did not
    reach a particular call; this proves the calls are not in the file at all.
    """
    tree = ast.parse(WORKER.read_text(encoding="utf-8"))

    banned_calls = {"eval", "exec", "compile", "execfile", "__import__",
                    "import_module", "load_module"}
    banned_modules = {"subprocess", "importlib", "shutil", "socket", "ctypes"}

    found_calls: set[str] = set()
    found_imports: set[str] = set()
    dynamic_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name in banned_calls:
                found_calls.add(name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found_imports.add(alias.name.split(".")[0])
                if alias.name != alias.name.strip():  # pragma: no cover
                    dynamic_imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found_imports.add(node.module.split(".")[0])

    assert not found_calls, (
        f"decimer_worker.py calls {found_calls}; a worker that evaluates its "
        f"input makes a structure image a code execution surface"
    )
    assert not (found_imports & banned_modules), (
        f"decimer_worker.py imports {found_imports & banned_modules}; the "
        f"worker opens one file and runs one model, and launches nothing else"
    )
    assert not dynamic_imports
    # every import in the file names a module written in the file. There is no
    # import whose target is computed, which is the mechanical form of
    # "dynamic import from user input" being impossible rather than merely
    # unlikely.
    assert found_imports, "the file does import things; this is not an empty check"


def test_the_worker_imports_decimer_only_inside_a_function():
    """The model is imported late and by name, never by a name that arrived.

    A module-level `import DECIMER` would make `op=info` unanswerable in an
    environment without it -- and §12 requires that environment to work.
    """
    tree = ast.parse(WORKER.read_text(encoding="utf-8"))
    top_level_imports = {
        alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "DECIMER" not in top_level_imports
    assert "tensorflow" not in top_level_imports


def test_the_worker_writes_diagnostics_to_stderr_and_answers_on_stdout():
    """A warning printed to stdout would corrupt the protocol line.

    Asserted structurally: `_log` writes to stderr, `_emit` to stdout, and
    nothing else in the module writes to either.
    """
    tree = ast.parse(WORKER.read_text(encoding="utf-8"))
    writers = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "write" and isinstance(node.func.value, ast.Attribute):
                if node.func.value.attr in ("stdout", "stderr"):
                    writers.append(node.func.value.attr)
    # `_emit` -> stdout, `_log` -> stderr, and nothing else
    assert writers.count("stdout") == 1
    assert writers.count("stderr") == 1


# --- the adapter -----------------------------------------------------------


def test_the_adapter_returns_exactly_the_reading(tmp_path):
    """`read()` is a string. Provenance is `recognize()`'s job.

    The service's contract is one method wide on purpose: a caller that only
    wants to know what the model saw cannot accidentally read a version field
    as a chemistry field.
    """
    from crea.providers.decimer import DecimerImageReader, RecognitionReading

    class FakeClient:
        def recognize_image(self, image_path, image_sha256="", source="",
                            expected_recognizer="decimer"):
            return EngineResponse(ok=True, result={
                "raw_smiles": "c1ccccc1",
                "image_sha256": image_sha256 or "a" * 64,
                "recognizer": "decimer",
                "recognizer_version": "1.0.0",
                "model_version": "2.15.0",
                "stage": "IMAGE_RECOGNIZED_CANDIDATE",
            }, environment={"DECIMER": "1.0.0"})

    from crea.core.models.identity import ImageProvenance

    reader = DecimerImageReader(client=FakeClient())
    provenance = ImageProvenance(image_sha256="b" * 64, source="user_provided",
                                 file_reference=str(tmp_path / "d.png"))
    assert reader.read(provenance) == "c1ccccc1"

    reading = reader.recognize(provenance)
    assert isinstance(reading, RecognitionReading)
    assert reading.raw_smiles == "c1ccccc1"
    assert reading.recognizer_version == "1.0.0"
    assert reading.model_version == "2.15.0"
    # a reading is a reading: it carries no verdict field that could be read as
    # one, which is the same rule the worker applies to its own result
    from dataclasses import fields as dataclass_fields

    names = {f.name for f in dataclass_fields(reading)}
    assert names.isdisjoint({"status", "resolved", "identity", "confidence",
                             "certainty", "score"})

    # and the provenance the phase keeps is built from the reading, not guessed
    record = reading.provenance(provenance)
    assert record.raw_smiles == "c1ccccc1"
    assert record.recognizer == "decimer"
    assert record.recognizer_version == "1.0.0"
    assert record.image_sha256 == "b" * 64
    # §16: the image itself is never copied into the record
    assert not hasattr(record, "image_bytes")
    assert not hasattr(record, "image_data")


def test_the_adapter_refuses_without_a_file_reference():
    from crea.core.models.identity import ImageProvenance
    from crea.providers.decimer import DecimerImageReader, RecognizerUnavailable

    class FakeClient:
        def recognize_image(self, *a, **k):  # pragma: no cover - never reached
            raise AssertionError("must not be called")

    reader = DecimerImageReader(client=FakeClient())
    with pytest.raises(RecognizerUnavailable, match="file reference"):
        reader.recognize(ImageProvenance())


def test_the_adapter_is_unavailable_without_an_isolated_runtime(monkeypatch):
    """§12's degradation path, stated as a test.

    No runtime, no installation attempt, no crash: a status a caller can read
    and a service can turn into `UNAVAILABLE`.
    """
    from crea.providers.base import Availability
    from crea.providers.decimer import (
        DEFAULT_DECIMER_ENV,
        DecimerImageReader,
        RecognizerUnavailable,
    )

    monkeypatch.delenv("CREA_DECIMER_PYTHON", raising=False)
    monkeypatch.setattr("crea.providers.decimer.DEFAULT_DECIMER_ENV",
                        pathlib.Path("Z:/definitely/not/here"))
    reader = DecimerImageReader()
    status = reader.status()
    assert status.availability is Availability.MISSING_KEY
    assert "optional" in status.reason

    from crea.core.models.identity import ImageProvenance
    with pytest.raises(RecognizerUnavailable):
        reader.recognize(ImageProvenance(file_reference="x.png"))


def test_the_service_reports_an_absent_recogniser_instead_of_inventing_one():
    """`NO_RECOGNIZER` / `UNAVAILABLE` is the answer, and no candidate appears."""
    from crea.core.models.identity import (
        StructureInput,
        StructureResolutionStatus,
    )
    from crea.providers.decimer import DecimerImageReader
    from crea.services.identity import IdentityResolutionService

    service = IdentityResolutionService()
    result = service.resolve(StructureInput(kind="IMAGE", value="drawing.png"))
    assert result.status is StructureResolutionStatus.UNAVAILABLE
    assert "NO_RECOGNIZER" in result.reason_codes
    assert result.candidates == []
    assert result.selected is None

    # and with a reader that has no runtime, the same answer
    service = IdentityResolutionService(image_reader=DecimerImageReader())
    result = service.resolve(StructureInput(kind="IMAGE", value="drawing.png"))
    assert result.status is StructureResolutionStatus.UNAVAILABLE
    assert result.candidates == []


def test_a_recogniser_failure_is_unavailable_and_not_a_chemistry_result():
    from crea.core.models.identity import ImageProvenance, StructureInput
    from crea.providers.decimer import (
        DecimerImageReader,
        RecognizerFailed,
    )
    from crea.services.identity import IdentityResolutionService

    class BrokenClient:
        def recognize_image(self, *a, **k):
            raise RecognizerFailed("the worker died")

    service = IdentityResolutionService(
        image_reader=DecimerImageReader(client=BrokenClient())
    )
    result = service.resolve(StructureInput(
        kind="IMAGE", value="d.png",
        image=ImageProvenance(file_reference="d.png"),
    ))
    assert "RECOGNIZER_FAILED" in result.reason_codes
    assert result.selected is None


def test_the_license_record_separates_the_four_questions():
    """§13: software / model / weights / runtime, answered apart.

    A file that answered "MIT" once and moved on is the failure this guards.
    """
    doc = (pathlib.Path(__file__).resolve().parents[2]
           / "docs" / "PHASE36_DECIMER_LICENSES.md").read_text(encoding="utf-8")
    for heading in ("Software licence", "Model licence",
                    "Weights / training data provenance", "Runtime requirements"):
        assert heading in doc, f"the record never answers {heading!r}"
    assert "not asserted" in doc
    assert "must not be" in doc or "never** installed" in doc.lower()

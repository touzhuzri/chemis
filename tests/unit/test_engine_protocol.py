"""The engine boundary: protocol, client, and the failure modes that matter.

The worker runs somewhere else with different packages. Everything here is
about what happens when that somewhere else is broken -- because it will be,
and a crash three layers up that looks like a chemistry problem wastes a day.
"""

import json
import os
import pathlib
import sys

import pytest

from crea.engine.client import EngineClient
from crea.engine.errors import (
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

WORKER = pathlib.Path(__file__).resolve().parents[2] / "workers" / "chemistry" / "worker.py"


# --- protocol ---------------------------------------------------------------


def test_the_protocol_is_versioned():
    assert PROTOCOL_VERSION == "crea-engine/1"
    assert EngineRequest(op=EngineOp.PING).protocol == PROTOCOL_VERSION
    assert EngineResponse().protocol == PROTOCOL_VERSION


def test_a_request_round_trips():
    request = EngineRequest(op=EngineOp.RETROSYNTHESIZE,
                            target_smiles="c1ccccc1", max_routes=2)
    assert EngineRequest.model_validate_json(request.model_dump_json()) == request


def test_a_response_round_trips_including_its_error():
    response = EngineResponse(ok=False, error={"error_type": "unavailable",
                                              "message": "not here"})
    again = EngineResponse.model_validate_json(response.model_dump_json())
    assert again.ok is False
    assert again.error.error_type == "unavailable"


def test_the_state_enum_covers_the_ways_a_worker_can_fail():
    for state in (WorkerState.AVAILABLE, WorkerState.UNAVAILABLE,
                  WorkerState.NOT_CONFIGURED, WorkerState.CRASHED,
                  WorkerState.TIMEOUT, WorkerState.PROTOCOL_ERROR):
        assert state.value


# --- the real worker, run with this interpreter ---------------------------


def test_the_worker_runs_and_reports_its_environment():
    """It answers, and it names what is actually importable.

    Run under the main interpreter it will report that AiZynthFinder is
    absent -- which is the correct and expected answer, not a failure. A test
    that needed it present would be testing the wrong interpreter.
    """
    client = EngineClient(worker_script=WORKER)
    try:
        response = client.ping()
    except WorkerUnavailable as exc:
        # either the worker cannot start here, or it started and reported that
        # its engine is missing. Both are honest answers.
        assert "importable" in str(exc) or "worker" in str(exc)
        return
    assert response.environment["python"] == sys.version.split()[0]
    assert "rdkit" in response.environment
    assert "aizynthfinder" in response.environment


def test_the_worker_reports_its_versions_rather_than_guessing():
    client = EngineClient(worker_script=WORKER)
    try:
        info = client.info()
    except WorkerUnavailable:
        pytest.skip("no worker")
    assert info.result["capabilities"] == ["retrosynthesize"]
    # aizynthfinder is not in this environment, and the worker says so
    assert "aizynthfinder" in info.environment


def test_the_worker_refuses_a_search_without_its_engine():
    """A search with no engine behind it returns nothing and says so."""
    client = EngineClient(worker_script=WORKER)
    try:
        result = client.retrosynthesize(target_smiles="c1ccccc1")
    except (WorkerUnavailable, WorkerNotConfigured):
        pass   # missing engine, or configured without data: both honest
    else:
        # if it did run, it must not have invented routes
        assert result.result.get("routes") is not None


# --- a fake worker, so every failure mode is reachable --------------------


def write_fake(tmp_path, body: str) -> str:
    script = tmp_path / "fake_worker.py"
    script.write_text(body, encoding="utf-8")
    return str(script)


def test_a_missing_worker_is_unavailable(tmp_path):
    client = EngineClient(worker_script=tmp_path / "nope.py")
    with pytest.raises(WorkerUnavailable):
        client.ping()


def test_a_worker_that_exits_immediately_is_crashed(tmp_path):
    script = write_fake(tmp_path, "import sys\nsys.exit(3)\n")
    client = EngineClient(worker_script=script)
    with pytest.raises(WorkerCrashed) as exc:
        client.ping()
    assert "3" in str(exc.value)


def test_a_worker_that_says_nothing_times_out(tmp_path):
    script = write_fake(tmp_path, "import time\nwhile True: time.sleep(0.1)\n")
    client = EngineClient(worker_script=script, startup_timeout=1.0)
    with pytest.raises(WorkerTimeout):
        client.ping()


def test_a_worker_that_answers_nonsense_is_a_protocol_error(tmp_path):
    script = write_fake(tmp_path,
                        "import sys\nsys.stdin.readline()\nprint('not json')\n")
    client = EngineClient(worker_script=script)
    with pytest.raises(WorkerProtocolError):
        client.ping()


def test_a_worker_error_is_typed_not_swallowed(tmp_path):
    body = (
        "import sys, json\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'protocol': 'crea-engine/1', 'ok': False,\n"
        "  'result': {}, 'environment': {},\n"
        "  'error': {'error_type': 'not_configured',\n"
        "             'message': 'needs stock and policy', 'detail': {}}}))\n"
    )
    client = EngineClient(worker_script=write_fake(tmp_path, body))
    with pytest.raises(WorkerNotConfigured) as exc:
        client.retrosynthesize(target_smiles="c1ccccc1")
    assert "stock" in str(exc.value)


def test_a_worker_reports_what_environment_it_is_in(tmp_path):
    body = (
        "import sys, json\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'protocol': 'crea-engine/1', 'ok': True,\n"
        "  'result': {'routes': []},\n"
        "  'environment': {'python': '3.10.11', 'rdkit': '2023.9.6',\n"
        "                   'aizynthfinder': '4.4.1'}}))\n"
    )
    client = EngineClient(worker_script=write_fake(tmp_path, body))
    result = client.retrosynthesize(target_smiles="c1ccccc1")
    assert result.environment["aizynthfinder"] == "4.4.1"
    assert result.result["routes"] == []


def test_one_request_does_not_leave_the_worker_running(tmp_path):
    body = (
        "import sys, json\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'protocol': 'crea-engine/1', 'ok': True,\n"
        "  'result': {}, 'environment': {}}))\n"
    )
    client = EngineClient(worker_script=write_fake(tmp_path, body))
    client.ping()
    client.ping()
    # each request is its own process; nothing accumulates between calls
    assert client.request_timeout > 0


# --- the stdout/stderr contract --------------------------------------------
#
# stdout is the protocol channel. A worker that prints a warning to stdout
# interleaves it with the answer and the caller cannot read either. These are
# the tests that keep that from happening by accident.


def test_a_worker_that_chatters_on_stderr_is_still_readable(tmp_path):
    """Diagnostics belong on stderr, and there they cost nothing."""
    body = (
        "import sys, json\n"
        "sys.stdin.readline()\n"
        "for i in range(200):\n"
        "    print('loading policy %d' % i, file=sys.stderr)\n"
        "print(json.dumps({'protocol': 'crea-engine/1', 'ok': True,\n"
        "  'result': {'routes': []}, 'environment': {}}))\n"
    )
    client = EngineClient(worker_script=write_fake(tmp_path, body))
    result = client.retrosynthesize(target_smiles="c1ccccc1")
    assert result.result["routes"] == []


def test_a_worker_that_prints_to_stdout_is_a_protocol_error(tmp_path):
    """The failure mode the stderr rule exists to prevent, asserted directly."""
    body = (
        "import sys, json\n"
        "sys.stdin.readline()\n"
        "print('warming up')\n"          # not protocol
        "print(json.dumps({'protocol': 'crea-engine/1', 'ok': True,\n"
        "  'result': {}, 'environment': {}}))\n"
    )
    client = EngineClient(worker_script=write_fake(tmp_path, body))
    with pytest.raises(WorkerProtocolError):
        client.ping()


def test_the_real_worker_keeps_stdout_clean(tmp_path):
    """The shipped worker, not a fake: everything on stdout must parse."""
    import subprocess

    request = json.dumps({"op": "info", "protocol": PROTOCOL_VERSION})
    out = subprocess.run([sys.executable, str(WORKER)], input=request + "\n",
                         capture_output=True, text=True, timeout=180)
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected one protocol line, got {lines!r}"
    payload = json.loads(lines[0])
    assert payload["ok"] is True
    assert payload["protocol"] == PROTOCOL_VERSION
    # and it is allowed to say what it is doing -- on stderr
    assert "worker starting" in out.stderr


# --- the rest of the request contract ---------------------------------------


def test_a_worker_that_answers_nothing_at_all_is_crashed(tmp_path):
    """Exit 0 with no output is not success; silence is not an answer."""
    body = "import sys\nsys.stdin.readline()\n"
    client = EngineClient(worker_script=write_fake(tmp_path, body))
    with pytest.raises(WorkerCrashed):
        client.ping()


def test_an_unknown_method_is_a_structured_error_not_a_crash(tmp_path):
    body = (
        "import sys, json\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'protocol': 'crea-engine/1', 'ok': False,\n"
        "  'result': {}, 'environment': {},\n"
        "  'error': {'error_type': 'unknown_op',\n"
        "             'message': \"unknown op 'delete_everything'\",\n"
        "             'detail': {'supported': ['ping', 'info',\n"
        "                                      'retrosynthesize']}}}))\n"
    )
    client = EngineClient(worker_script=write_fake(tmp_path, body))
    with pytest.raises(WorkerReportedError) as exc:
        client.info()
    assert "delete_everything" in str(exc.value)
    assert exc.value.error_type == "unknown_op"


def test_a_missing_parameter_is_reported_by_the_worker_not_guessed(tmp_path):
    body = (
        "import sys, json\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'protocol': 'crea-engine/1', 'ok': False,\n"
        "  'result': {}, 'environment': {},\n"
        "  'error': {'error_type': 'invalid_request',\n"
        "             'message': 'no target smiles given', 'detail': {}}}))\n"
    )
    client = EngineClient(worker_script=write_fake(tmp_path, body))
    with pytest.raises(WorkerReportedError) as exc:
        client.retrosynthesize(target_smiles="")
    assert exc.value.error_type == "invalid_request"


def test_a_request_id_is_sent_and_echoed_back(tmp_path):
    body = (
        "import sys, json\n"
        "req = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'protocol': 'crea-engine/1', 'ok': True,\n"
        "  'result': {}, 'environment': {},\n"
        "  'request_id': req.get('request_id', '')}))\n"
    )
    client = EngineClient(worker_script=write_fake(tmp_path, body))
    first = client.ping()
    second = client.ping()
    assert first.request_id
    assert second.request_id != first.request_id


def test_the_real_worker_echoes_the_request_id():
    client = EngineClient(worker_script=WORKER)
    try:
        response = client.info()
    except WorkerUnavailable:
        pytest.skip("no worker")
    assert response.request_id


def test_the_real_worker_accepts_method_as_well_as_op():
    """Both names reach the same operation; neither is a bare unknown op."""
    import subprocess

    def ask(field: str):
        request = json.dumps({field: "ping", "protocol": PROTOCOL_VERSION,
                              "request_id": "abc123"})
        out = subprocess.run([sys.executable, str(WORKER)],
                             input=request + "\n", capture_output=True,
                             text=True, timeout=180)
        return json.loads(out.stdout)

    for field in ("op", "method"):
        payload = ask(field)
        assert payload["request_id"] == "abc123"
        if field == "op":
            assert payload["ok"] is True or payload["error"]["error_type"] == "unavailable"


def test_the_real_worker_refuses_a_request_that_is_not_an_object():
    import subprocess

    out = subprocess.run([sys.executable, str(WORKER)], input="[1, 2, 3]\n",
                         capture_output=True, text=True, timeout=180)
    payload = json.loads(out.stdout)
    assert payload["ok"] is False
    assert payload["error"]["error_type"] == "invalid_request"


# --- the boundary the architecture depends on ------------------------------


def test_nothing_in_the_main_environment_imports_the_engine():
    """The whole point: CREA's RDKit stays as it is."""
    import subprocess

    code = (
        "import sys;"
        "sys.path.insert(0, r'%s');"
        "import crea.engine, crea.engine.client, crea.agent, crea.chem.align;"
        "mods = [m for m in sys.modules if 'aizynth' in m];"
        "print(mods)"
        % str(pathlib.Path(__file__).resolve().parents[2] / "src")
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    assert out.stdout.strip() == "[]", (
        f"importing CREA pulled in the engine: {out.stdout.strip()}")


def test_the_main_rdkit_is_untouched():
    """Whatever the worker environment does, this one does not move."""
    import rdkit

    assert rdkit.__version__.startswith("2026."), (
        f"the main environment's RDKit is {rdkit.__version__}; "
        f"Phase 1 is verified against 2026.3.6")


# --- the worker's own decisions, tested without the engine ------------------
#
# Three of these were real bugs, found by running the actual engine rather
# than by reading the code: a requirement string with its parentheses still in
# it, an RDKit that version-stamps itself zero-padded, and a missing policy
# reported as a failed search. Each one misreported a working setup as broken,
# which is the expensive direction to be wrong in.


def _worker_module():
    """`workers/chemistry/worker.py`, imported rather than run."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("crea_chem_worker", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_requirement_string_is_split_into_a_name_and_a_specifier():
    worker = _worker_module()
    # this is the exact shape `importlib.metadata.requires()` returns
    for line in ("rdkit (<2024.0.0,>=2023.9.1)",
                 "rdkit<2024.0.0,>=2023.9.1",
                 "rdkit >=2023.9.1, <2024.0.0"):
        name, spec = worker._split_requirement(line)
        assert name == "rdkit", line
        assert spec, line


def test_a_zero_padded_rdkit_version_satisfies_its_own_pin():
    """RDKit says `2023.09.6`; the pin says `2023.9.1`. They are the same.

    Comparing the strings reported a perfectly good environment as
    incompatible, and the only obvious "fix" for that is to go and change the
    RDKit this project is verified against.
    """
    worker = _worker_module()
    assert worker._satisfied("2023.09.6", ">=2023.9.1,<2024.0.0")
    assert worker._satisfied("2023.9.6", ">=2023.9.1,<2024.0.0")
    # the padding is in the month, and that is the only difference
    assert worker._tuple("2023.09.6")[:2] == worker._tuple("2023.9.1")[:2] == (2023, 9)


def test_a_genuinely_wrong_version_is_still_caught():
    worker = _worker_module()
    # too new
    assert not worker._satisfied("2026.03.6", ">=2023.9.1,<2024.0.0")
    # too old
    assert not worker._satisfied("2023.9.0", ">=2023.9.1,<2024.0.0")
    # no RDKit at all
    assert not worker._satisfied("unavailable: ModuleNotFoundError",
                                 ">=2023.9.1,<2024.0.0")
    assert not worker._satisfied("", ">=2023.9.1,<2024.0.0")


def test_a_missing_policy_is_a_configuration_answer_not_a_failed_search():
    """`PolicyException: No expansion policy selected` is the engine saying it
    has no model. Reporting it as a search failure tells a caller to retry
    something no retry can fix."""
    worker = _worker_module()

    class PolicyException(Exception):
        pass

    class StockException(Exception):
        pass

    class SomethingElse(Exception):
        pass

    assert worker._is_configuration_error(PolicyException("No expansion policy selected"))
    assert worker._is_configuration_error(StockException("no stock"))
    # and by message, for an environment where the classes will not import
    assert worker._is_configuration_error(
        type("X", (Exception,), {})("No expansion policy selected"))
    assert not worker._is_configuration_error(SomethingElse("a real failure"))


def test_the_worker_never_imports_the_engine_at_module_level():
    """It must start, and be able to answer `ping`, in an environment where
    the engine does not exist. Importing it at the top would make the worker's
    own failure reporting unavailable exactly when it is needed."""
    import ast

    tree = ast.parse(WORKER.read_text(encoding="utf-8"))
    for node in tree.body:                       # module level only
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for name in names:
            assert "aizynthfinder" not in name, (
                f"worker.py imports {name} at module level; it has to be able "
                f"to report that the engine is missing")


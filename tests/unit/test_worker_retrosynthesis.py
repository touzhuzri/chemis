"""The worker-backed retrosynthesis provider, exercised against fake workers.

No AiZynthFinder, no network, no real subprocess engine: every worker here is a
few lines of Python written to `tmp_path`. That is deliberate. The engine cannot
run in CI, and a test that needed it would be a test that stops being run.

What is being pinned is the *contract*: that a route the engine did not produce
is never invented, that every way the worker can fail lands on a status that
says so, and that what comes back is our own `CandidateRoute` rather than
AiZynthFinder's internal objects.

The one thing these tests cannot establish is whether the real engine works.
That is `tests/live/test_route_live.py`, behind CREA_AIZYNTHFINDER_LIVE=1.
"""

import json

import pytest

from crea.core.models.route import (
    ProviderReason,
    ProviderStatus,
    RetrosynthesisRequest,
    ScoreSource,
    StepStatus,
)
from crea.engine.client import EngineClient
from crea.providers.worker_retrosynthesis import WorkerRetrosynthesisProvider

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"

ENVIRONMENT = {"python": "3.10.11", "rdkit": "2023.9.6", "aizynthfinder": "4.4.1"}

#: One route, two steps, in the shape `workers/chemistry/worker.py` emits.
ROUTE_PAYLOAD = {
    "target_smiles": DIYNE,
    "route_count": 1,
    "search_seconds": 12.5,
    "config_file": "/data/config.yml",
    "max_score": 0.87,
    "routes": [
        {
            "route_index": 0,
            "score": 0.91,
            "is_solved": True,
            "steps": [
                {
                    "index": 1,
                    "reactants": ["C#Cc1ccccc1", "C#Cc1ccccc1"],
                    "products": [DIYNE],
                    "reaction_smiles": "C#Cc1ccccc1.C#Cc1ccccc1>>"
                                       "c1ccccc1C#CC#Cc2ccccc2",
                    "smarts": "[#6:1]-[#6:2]>>[#6:1]#[#6:2]",
                    "score": 0.93,
                    "is_solved": True,
                },
                {
                    "index": 2,
                    "reactants": ["C#Cc1ccccc1", "BrBr"],
                    "products": ["C#Cc1ccccc1"],
                    "reaction_smiles": None,
                    "smarts": None,
                    "score": 0.42,
                    "is_solved": False,
                },
            ],
        }
    ],
}


# --- helpers ----------------------------------------------------------------


def write_worker(tmp_path, body: str) -> str:
    script = tmp_path / "fake_engine.py"
    script.write_text(body, encoding="utf-8")
    return str(script)


def responding_worker(tmp_path, payload, *, ok=True, error=None,
                     environment=None) -> str:
    """A worker that answers `info` and `retrosynthesize` the same way."""
    env = environment if environment is not None else ENVIRONMENT
    body = (
        "import sys, json\n"
        f"ENV = {env!r}\n"
        f"PAYLOAD = {payload!r}\n"
        f"OK = {ok!r}\n"
        f"ERROR = {error!r}\n"
        "req = json.loads(sys.stdin.readline())\n"
        "op = req.get('op') or req.get('method')\n"
        "if op == 'info':\n"
        "    out = {'protocol': 'crea-engine/1', 'ok': True,\n"
        "           'result': {'capabilities': ['retrosynthesize'],\n"
        "                      'requires_config': True},\n"
        "           'error': None, 'environment': ENV,\n"
        "           'request_id': req.get('request_id', '')}\n"
        "elif op == 'retrosynthesize':\n"
        "    if OK:\n"
        "        out = {'protocol': 'crea-engine/1', 'ok': True,\n"
        "               'result': PAYLOAD, 'error': None,\n"
        "               'environment': ENV,\n"
        "               'request_id': req.get('request_id', '')}\n"
        "    else:\n"
        "        out = {'protocol': 'crea-engine/1', 'ok': False,\n"
        "               'result': {}, 'environment': ENV,\n"
        "               'error': ERROR,\n"
        "               'request_id': req.get('request_id', '')}\n"
        "else:\n"
        "    out = {'protocol': 'crea-engine/1', 'ok': False, 'result': {},\n"
        "           'environment': ENV,\n"
        "           'error': {'error_type': 'unknown_op',\n"
        "                      'message': 'unknown op', 'detail': {}}}\n"
        "print(json.dumps(out))\n"
    )
    return write_worker(tmp_path, body)


def provider_for(tmp_path, script, **kwargs) -> WorkerRetrosynthesisProvider:
    return WorkerRetrosynthesisProvider(
        client=EngineClient(worker_script=script,
                            startup_timeout=20.0,
                            request_timeout=60.0),
        **kwargs)


def ask(provider) -> "object":
    return provider.retrosynthesize(
        RetrosynthesisRequest(target_smiles=DIYNE, max_routes=2))


# --- the states -------------------------------------------------------------


def test_a_worker_that_is_not_there_is_unavailable(tmp_path):
    provider = WorkerRetrosynthesisProvider(
        client=EngineClient(worker_script=tmp_path / "absent.py"))
    status, reason = provider.probe()
    assert status is ProviderStatus.UNAVAILABLE
    assert reason

    result = ask(provider)
    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.routes == []


def test_a_worker_with_no_policy_or_stock_is_not_configured(tmp_path):
    script = responding_worker(
        tmp_path, {}, ok=False,
        error={"error_type": "not_configured",
               "message": "it needs a stock file and an expansion policy",
               "detail": {"config_file": None}})
    provider = provider_for(tmp_path, script)

    assert provider.probe()[0] is ProviderStatus.AVAILABLE   # it imports fine
    result = ask(provider)
    # the search is what discovers the missing data, and says so
    assert result.status is ProviderStatus.NOT_CONFIGURED
    assert "stock" in result.reason
    assert result.routes == []


def test_a_worker_that_crashes_is_failed_and_returns_no_routes(tmp_path):
    script = write_worker(tmp_path,
                          "import sys\nsys.stdin.readline()\nraise SystemExit(9)\n")
    result = ask(provider_for(tmp_path, script))
    assert result.status is ProviderStatus.FAILED
    assert result.routes == []
    assert "9" in result.reason


def test_a_worker_that_hangs_is_failed_and_returns_no_routes(tmp_path):
    script = write_worker(tmp_path,
                          "import sys, time\nsys.stdin.readline()\n"
                          "while True: time.sleep(0.05)\n")
    provider = WorkerRetrosynthesisProvider(
        client=EngineClient(worker_script=script, request_timeout=1.0))
    result = ask(provider)
    assert result.status is ProviderStatus.FAILED
    assert result.routes == []
    assert "Timeout" in result.reason or "timeout" in result.reason


def test_a_worker_that_answers_nonsense_is_failed_and_returns_no_routes(tmp_path):
    script = write_worker(tmp_path,
                          "import sys\nsys.stdin.readline()\nprint('<html>502</html>')\n")
    result = ask(provider_for(tmp_path, script))
    assert result.status is ProviderStatus.FAILED
    assert result.routes == []


def test_a_worker_whose_engine_does_not_import_is_unavailable(tmp_path):
    """The answer the main environment gives, and it is the right one."""
    script = responding_worker(
        tmp_path, {}, ok=False,
        error={"error_type": "unavailable",
               "message": "aizynthfinder is not importable here",
               "detail": {}},
        environment={"python": "3.10.11", "rdkit": "2026.3.6",
                     "aizynthfinder": "unavailable: ModuleNotFoundError"})
    result = ask(provider_for(tmp_path, script))
    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.routes == []


@pytest.mark.parametrize("failure", ["absent", "crash", "nonsense", "timeout"])
def test_no_failure_ever_produces_a_route(tmp_path, failure):
    """The single most important line in this file.

    A provider that cannot reach an engine returns nothing. It does not fall
    back to a fixture table, and it does not construct a plausible route.
    """
    if failure == "absent":
        provider = WorkerRetrosynthesisProvider(
            client=EngineClient(worker_script=tmp_path / "absent.py"))
    elif failure == "crash":
        provider = provider_for(tmp_path, write_worker(
            tmp_path, "import sys\nsys.stdin.readline()\nsys.exit(1)\n"))
    elif failure == "nonsense":
        provider = provider_for(tmp_path, write_worker(
            tmp_path, "import sys\nsys.stdin.readline()\nprint('nope')\n"))
    else:
        provider = WorkerRetrosynthesisProvider(
            client=EngineClient(worker_script=write_worker(
                tmp_path,
                "import sys, time\nsys.stdin.readline()\n"
                "while True: time.sleep(0.05)\n"),
                request_timeout=1.0))

    result = ask(provider)
    assert result.routes == [], f"{failure} produced {result.routes!r}"
    assert result.available is False


# --- the conversion ---------------------------------------------------------


def test_a_route_from_the_worker_becomes_a_candidate_route(tmp_path):
    result = ask(provider_for(tmp_path, responding_worker(tmp_path, ROUTE_PAYLOAD)))

    assert result.status is ProviderStatus.AVAILABLE
    assert len(result.routes) == 1
    route = result.routes[0]
    assert route.target.canonical_smiles  # the target was normalised, not echoed
    assert len(route.steps) == 2
    assert all(step.status is StepStatus.CANDIDATE for step in route.steps)
    assert all(step.support_level == "no_support" for step in route.steps)


def test_the_engine_score_is_attributed_and_never_called_a_confidence(tmp_path):
    result = ask(provider_for(tmp_path, responding_worker(tmp_path, ROUTE_PAYLOAD)))
    route = result.routes[0]

    assert route.score == pytest.approx(0.91)
    assert route.score_source is ScoreSource.AIZYNTHFINDER
    assert route.steps[0].score_source is ScoreSource.AIZYNTHFINDER
    # the word is simply not on the model
    dumped = json.dumps(route.model_dump(mode="json")).lower()
    assert "confidence" not in dumped
    assert "probability" not in dumped


def test_a_reaction_smiles_is_copied_when_given_and_absent_when_not(tmp_path):
    result = ask(provider_for(tmp_path, responding_worker(tmp_path, ROUTE_PAYLOAD)))
    first, second = result.routes[0].steps

    assert first.transformation.reaction_smiles.startswith("C#Cc1ccccc1")
    # the second step's engine gave none, and none was reconstructed
    assert second.transformation.reaction_smiles is None
    assert second.transformation.template is None
    assert second.transformation.template_source == "UNKNOWN"


def test_the_engine_never_told_us_a_reaction_class(tmp_path):
    result = ask(provider_for(tmp_path, responding_worker(tmp_path, ROUTE_PAYLOAD)))
    for step in result.routes[0].steps:
        transformation = step.transformation
        assert transformation.transformation_class == "UNKNOWN"
        assert transformation.transformation_class_source == "UNKNOWN"


def test_a_step_says_out_loud_that_no_literature_was_consulted(tmp_path):
    result = ask(provider_for(tmp_path, responding_worker(tmp_path, ROUTE_PAYLOAD)))
    warnings = " ".join(result.routes[0].steps[0].warnings).lower()
    assert "no literature" in warnings
    assert any("no literature" in w.lower() for w in result.warnings)


def test_the_provenance_records_the_environment_that_actually_ran(tmp_path):
    provider = provider_for(tmp_path, responding_worker(tmp_path, ROUTE_PAYLOAD),
                            model_version="4.4.1-figshare",
                            stock_configuration="zinc stock v1")
    result = ask(provider)

    assert result.provenance.provider == "aizynthfinder-worker"
    assert result.provenance.provider_version == "4.4.1"
    assert result.provenance.model_version == "4.4.1-figshare"
    assert result.provenance.stock_configuration == "zinc stock v1"
    assert result.provenance.generated_at


def test_a_garbage_smiles_in_the_payload_does_not_take_the_provider_down(tmp_path):
    """Upstream data is data, not code, and may be nonsense."""
    payload = json.loads(json.dumps(ROUTE_PAYLOAD))
    payload["routes"][0]["steps"][0]["reactants"] = ["not a smiles", "C#Cc1ccccc1"]
    result = ask(provider_for(tmp_path, responding_worker(tmp_path, payload)))

    assert result.status is ProviderStatus.AVAILABLE
    assert len(result.routes[0].steps) == 2
    # the unusable string is kept as given rather than silently dropped
    kept = [m.smiles for m in result.routes[0].steps[0].reactants]
    assert "not a smiles" in kept


def test_an_empty_route_list_is_a_valid_answer(tmp_path):
    """The engine ran, and found nothing. That is not a failure."""
    script = responding_worker(tmp_path, {**ROUTE_PAYLOAD, "routes": [],
                                          "route_count": 0})
    result = ask(provider_for(tmp_path, script))
    assert result.status is ProviderStatus.AVAILABLE
    assert result.routes == []
    assert result.reason == ""


def test_a_step_with_no_score_is_reported_as_unscored_not_as_zero(tmp_path):
    payload = json.loads(json.dumps(ROUTE_PAYLOAD))
    payload["routes"][0]["steps"][0]["score"] = None
    payload["routes"][0]["score"] = None
    result = ask(provider_for(tmp_path, responding_worker(tmp_path, payload)))

    assert result.routes[0].score is None
    assert result.routes[0].score_source is ScoreSource.NONE
    assert result.routes[0].steps[0].score_source is ScoreSource.NONE


def test_the_engine_search_time_is_reported_as_measured(tmp_path):
    result = ask(provider_for(tmp_path, responding_worker(tmp_path, ROUTE_PAYLOAD)))
    joined = " ".join(result.warnings)
    assert "12.5" in joined
    assert "3.10.11" in joined      # the worker's own python
    assert "2023.9.6" in joined     # and its own rdkit, not ours


# --- the reason codes -------------------------------------------------------
#
# `reason` is a sentence. `reason_code` is the same fact in a form a caller can
# branch on, so "is this worth retrying" is a question about a value rather
# than about a sentence's English.


def test_every_failure_carries_a_reason_code(tmp_path):
    result = ask(provider_for(tmp_path, write_worker(
        tmp_path, "import sys\nsys.stdin.readline()\nsys.exit(1)\n")))
    assert result.reason_code is ProviderReason.WORKER_CRASH
    assert result.reason


def test_a_missing_runtime_is_distinguished_from_a_broken_one(tmp_path):
    provider = WorkerRetrosynthesisProvider(
        client=EngineClient(worker_script=tmp_path / "absent.py"))
    assert ask(provider).reason_code is ProviderReason.MISSING_RUNTIME


def test_a_timeout_is_not_reported_as_a_crash(tmp_path):
    """One is worth retrying and one is not, and the code is how you tell."""
    provider = WorkerRetrosynthesisProvider(
        client=EngineClient(worker_script=write_worker(
            tmp_path,
            "import sys, time\nsys.stdin.readline()\n"
            "while True: time.sleep(0.05)\n"),
            request_timeout=1.0))
    result = ask(provider)
    assert result.reason_code is ProviderReason.WORKER_TIMEOUT
    assert result.reason_code is not ProviderReason.WORKER_CRASH


def test_a_missing_policy_is_reported_as_a_configuration_problem(tmp_path):
    """`not_configured` does not say whether the policy or the stock was
    missing, and picking one by reading the message would be a guess."""
    script = responding_worker(
        tmp_path, {}, ok=False,
        error={"error_type": "not_configured",
               "message": "it needs a stock file and an expansion policy",
               "detail": {}})
    result = ask(provider_for(tmp_path, script))
    assert result.reason_code is ProviderReason.INVALID_CONFIGURATION
    assert result.reason_code not in (ProviderReason.MISSING_MODEL,
                                      ProviderReason.MISSING_STOCK)


def test_the_more_specific_reasons_exist_for_a_caller_that_knows():
    """They are available, and unused, because nothing here may guess them."""
    assert ProviderReason.MISSING_MODEL.value == "missing_model"
    assert ProviderReason.MISSING_STOCK.value == "missing_stock"


def test_a_dependency_conflict_is_its_own_code(tmp_path):
    """The one that must never be answered by downgrading RDKit."""
    script = responding_worker(
        tmp_path, {}, ok=False,
        error={"error_type": "incompatible_dependency",
               "message": "aizynthfinder 4.4.1 requires rdkit>=2023.9.1,"
                          "<2024.0.0, but this environment has rdkit 2026.3.6",
               "detail": {"aizynthfinder": "4.4.1", "rdkit": "2026.3.6"}})
    result = ask(provider_for(tmp_path, script))
    assert result.reason_code is ProviderReason.INCOMPATIBLE_DEPENDENCY
    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.routes == []


def test_a_worker_that_runs_has_no_reason_code_to_report(tmp_path):
    result = ask(provider_for(tmp_path, responding_worker(tmp_path, ROUTE_PAYLOAD)))
    assert result.reason_code is ProviderReason.NONE
    assert result.reason == ""


def test_the_probe_exposes_the_same_reason(tmp_path):
    provider = provider_for(tmp_path, write_worker(
        tmp_path, "import sys\nsys.stdin.readline()\nsys.exit(2)\n"))
    provider.probe()
    assert provider.reason_code is ProviderReason.WORKER_CRASH

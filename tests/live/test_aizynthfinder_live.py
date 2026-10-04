"""The real engine, over the real pipe, against the real model.

    CREA_AIZYNTHFINDER_LIVE=1 pytest tests/live/test_aizynthfinder_live.py

Opt-in, and separate from the other live tests on purpose: this needs no LLM
key, no literature API and no quota. It needs a chemistry engine runtime, a
policy and a stock file, which is a much rarer thing to have.

The chain under test is the whole of Phase 3.4's runtime claim:

    target SMILES
        ↓  EngineClient, JSON over a pipe
    workers/chemistry/worker.py, in its own environment
        ↓
    AiZynthFinder
        ↓
    our CandidateRoute

Every failure along the way has to end in a skip that says which piece was
missing. A test that fabricates a route to get past a missing model would make
the engine look verified when it is not.
"""

import os
import pathlib

import pytest

from crea.core.models.route import (
    ProviderStatus,
    RetrosynthesisRequest,
    ScoreSource,
    StepStatus,
)
from crea.engine.client import EngineClient
from crea.providers.worker_retrosynthesis import WorkerRetrosynthesisProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CREA_AIZYNTHFINDER_LIVE") != "1",
        reason="set CREA_AIZYNTHFINDER_LIVE=1 to exercise the real engine",
    ),
]

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"

#: The worker environment. Overridable, because a checkout is not the only
#: place this can live.
WORKER_ENV = pathlib.Path(os.environ.get(
    "CREA_AIZYNTHFINDER_PYTHON",
    "runtime/aizynthfinder-env/Scripts/python.exe"))
WORKER = pathlib.Path(__file__).resolve().parents[2] / "workers" / "chemistry" / "worker.py"
CONFIG = os.environ.get("CREA_AIZYNTHFINDER_CONFIG") or None


def _client() -> EngineClient:
    return EngineClient(python_executable=str(WORKER_ENV),
                        worker_script=WORKER,
                        startup_timeout=300.0,
                        request_timeout=1800.0)


def _provider() -> WorkerRetrosynthesisProvider:
    return WorkerRetrosynthesisProvider(client=_client(),
                                        model_version=os.environ.get(
                                            "CREA_AIZYNTHFINDER_MODEL") or None,
                                        stock_configuration=os.environ.get(
                                            "CREA_AIZYNTHFINDER_STOCK") or None)


# --- is the runtime there at all --------------------------------------------


def test_the_worker_runtime_answers_and_names_its_environment():
    client = _client()
    if not WORKER_ENV.exists():
        pytest.skip(f"no worker environment at {WORKER_ENV}")
    try:
        info = client.info()
    except Exception as exc:  # noqa: BLE001 - the reason is the point
        pytest.skip(f"the worker runtime is not usable here: {type(exc).__name__}: {exc}")

    environment = info.environment
    assert "python" in environment
    assert "rdkit" in environment
    assert "aizynthfinder" in environment


def test_the_worker_runtime_is_a_different_rdkit_from_ours():
    """The reason the whole subprocess arrangement exists.

    If this ever fails, the isolation has been lost and the main environment's
    RDKit may have been moved.
    """
    import rdkit

    client = _client()
    if not WORKER_ENV.exists():
        pytest.skip(f"no worker environment at {WORKER_ENV}")
    try:
        info = client.info()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"the worker runtime is not usable here: {type(exc).__name__}: {exc}")

    theirs = info.environment.get("rdkit", "")
    ours = rdkit.__version__
    assert ours.startswith("2026."), f"the main environment moved to {ours}"
    assert theirs and not theirs.startswith("unavailable"), (
        f"the worker environment has no RDKit (got {theirs!r})")
    assert theirs != ours, (
        f"both environments are on rdkit {ours}; they are supposed to differ")


# --- the chain --------------------------------------------------------------


def test_a_target_survives_the_pipe_and_comes_back_as_a_candidate_route():
    provider = _provider()
    status, reason = provider.probe()
    if status is not ProviderStatus.AVAILABLE:
        pytest.skip(f"the engine is not available here: {status.value}: {reason}")

    result = provider.retrosynthesize(
        RetrosynthesisRequest(target_smiles=DIYNE, max_routes=2,
                              config_file=CONFIG))

    if result.status is ProviderStatus.NOT_CONFIGURED:
        pytest.skip(f"the engine has no policy/stock: {result.reason}")
    if result.status is not ProviderStatus.AVAILABLE:
        pytest.skip(f"the engine could not run here: {result.status.value}: "
                    f"{result.reason}")

    assert result.available
    assert result.routes, (
        "the engine ran and returned no routes. That is a legitimate answer "
        "for a target it cannot solve, and is not a failure of this test.")

    for route in result.routes:
        assert route.target.canonical_smiles
        for step in route.steps:
            # a real route from a real engine is still only a candidate
            assert step.status is StepStatus.CANDIDATE
            assert step.support_level == "no_support"
            # and the engine still did not tell us a reaction class
            assert step.transformation.transformation_class == "UNKNOWN"


def test_a_real_route_carries_its_own_score_and_says_whose_it_is():
    provider = _provider()
    status, reason = provider.probe()
    if status is not ProviderStatus.AVAILABLE:
        pytest.skip(f"the engine is not available here: {status.value}: {reason}")

    result = provider.retrosynthesize(
        RetrosynthesisRequest(target_smiles=DIYNE, max_routes=1, config_file=CONFIG))
    if result.status is not ProviderStatus.AVAILABLE:
        pytest.skip(f"the engine could not run here: {result.status.value}: {result.reason}")
    if not result.routes:
        pytest.skip("the engine found no route for this target")

    route = result.routes[0]
    if route.score is not None:
        assert route.score_source is ScoreSource.AIZYNTHFINDER
    assert result.provenance.provider_version, (
        "the engine's own version must be recorded, not assumed")


def test_no_route_is_invented_when_the_engine_cannot_be_reached(tmp_path):
    """The negative leg, run for real rather than mocked.

    A client pointed at a worker that is not there must produce an honest
    UNAVAILABLE with zero routes -- not a fixture route, and not a failure
    that looks like a chemistry problem.
    """
    from crea.engine.errors import WorkerUnavailable

    provider = WorkerRetrosynthesisProvider(
        client=EngineClient(worker_script=tmp_path / "not-a-worker.py"))
    result = provider.retrosynthesize(
        RetrosynthesisRequest(target_smiles=DIYNE))

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.routes == []
    assert result.reason

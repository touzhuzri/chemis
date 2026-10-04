"""The DECIMER adapter: an image recogniser reached over a pipe.

This is the only place in `src/` that knows a chemical structure image can be
turned into a SMILES at all, and it does not do the turning. It writes a
request to a worker's stdin and reads back whatever the recogniser said. The
worker lives in `workers/chemistry/decimer_worker.py` and runs under a Python
that has TensorFlow; this module runs under one that does not.

That split is the whole design and it is not negotiable per §12: DECIMER and
its weights must not be installed into the CREA main environment, because that
environment's RDKit is pinned to 2026.3.6 and verified against a test suite,
and a TensorFlow install is exactly the kind of dependency that quietly
re-resolves a pinned binary. So the adapter degrades instead of installing: if
the isolated runtime is not there, `status()` says so and `read()` raises a
typed error, and `services.identity` turns that into `UNAVAILABLE` with
`NO_RECOGNIZER`. Phase 3.6 is complete and correct with no recogniser present.

**What the adapter does not do.** It does not validate the SMILES, does not
repair it, does not pick among readings, and does not upgrade a reading into a
structure. Those are decisions about chemistry and they happen in
`crea.services.identity` and `crea.chem`, where the rules are written down. A
reading that came back from a model is a reading; see
`crea.core.models.identity.RecognitionStage`.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field

from crea.core.models.identity import ImageProvenance
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
from crea.providers.base import Availability, Capability, ProviderStatus

#: Where the isolated DECIMER runtime is expected, relative to the repository.
#: A checkout works without configuration, exactly as the AiZynthFinder
#: runtime does, and an environment that has no such directory reports
#: `Availability.MISSING_KEY` rather than failing to import this module.
DEFAULT_DECIMER_WORKER = (
    pathlib.Path(__file__).resolve().parents[3]
    / "workers" / "chemistry" / "decimer_worker.py"
)
DEFAULT_DECIMER_ENV = (
    pathlib.Path(__file__).resolve().parents[3]
    / "runtime" / "decimer-env"
)

#: Overriding the interpreter is a new knob, not a change to an existing one.
#: No other provider's variable is read, renamed or reinterpreted here.
ENV_DECIMER_PYTHON = "CREA_DECIMER_PYTHON"

#: A recognition can take a while on CPU and must still end. A hung model is a
#: `WorkerTimeout`, never a hung request.
DEFAULT_RECOGNITION_TIMEOUT = 300.0


def default_decimer_python() -> str | None:
    """The interpreter the worker should run under, or None if there is none.

    Order: an explicit override, then the conventional isolated runtime path,
    then nothing. "Nothing" is a normal answer -- §12 makes recognition
    optional -- and callers are expected to handle it rather than treat it as
    a broken installation.
    """
    override = os.environ.get(ENV_DECIMER_PYTHON)
    if override:
        return override
    candidate = DEFAULT_DECIMER_ENV / "Scripts" / "python.exe"
    if candidate.exists():
        return str(candidate)
    candidate_posix = DEFAULT_DECIMER_ENV / "bin" / "python"
    if candidate_posix.exists():
        return str(candidate_posix)
    return None


@dataclass
class RecognitionReading:
    """One reading, plus everything needed to audit it later (§16).

    The versions are copied from the worker's own report of what ran. A version
    recorded here by guessing would be a version of a different interpreter,
    which is the exact failure `workers/chemistry/decimer_worker.py`'s
    `_versions()` exists to prevent.
    """

    raw_smiles: str = ""
    image_sha256: str = ""
    recognizer: str = "decimer"
    recognizer_version: str = ""
    model_version: str = ""
    elapsed_seconds: float = 0.0
    source: str = ""
    environment: dict = field(default_factory=dict)

    def provenance(self, base: ImageProvenance | None = None) -> ImageProvenance:
        """Fold a reading into the image record the rest of the phase keeps.

        The image bytes stay where the user put them; only their hash and the
        reading travel on. See `ImageProvenance` for why nothing is copied.
        """
        base = base or ImageProvenance()
        return base.model_copy(update={
            "image_sha256": self.image_sha256 or base.image_sha256,
            "recognizer": self.recognizer,
            "recognizer_version": self.recognizer_version,
            "model_version": self.model_version,
            "raw_smiles": self.raw_smiles,
            "source": self.source or base.source,
        })


@dataclass
class DecimerImageReader:
    """The `image_reader` slot of `IdentityResolutionService`, and nothing more.

    It satisfies the one method the service calls -- `read(provenance) -> str`
    -- and also exposes `recognize()` for a caller that wants the provenance
    fields filled in. Constructing one does not start a process; the worker is
    spawned per reading and then stopped, the same one-request-per-process rule
    the AiZynthFinder side follows.
    """

    client: EngineClient | None = None
    python_executable: str | None = None
    worker_script: str | os.PathLike | None = None
    request_timeout: float = DEFAULT_RECOGNITION_TIMEOUT
    _checked: ProviderStatus | None = None

    # -- availability ----------------------------------------------------

    def status(self) -> ProviderStatus:
        """Whether a recogniser can be reached at all. Never raises.

        Two separate failures with two separate fixes: the runtime is absent
        (install it in `runtime/decimer-env`, or point `CREA_DECIMER_PYTHON`
        at one), and the runtime is present but its packages are not usable.
        Reporting both as "unavailable" would send a reader looking in the
        wrong place.
        """
        python = self.python_executable or default_decimer_python()
        if self.client is None and not python:
            return ProviderStatus(
                name="decimer",
                availability=Availability.MISSING_KEY,
                reason=(
                    "no isolated DECIMER runtime was found; image recognition "
                    "is optional and runs in its own environment by design"
                ),
            )
        if not pathlib.Path(str(self.worker_script or DEFAULT_DECIMER_WORKER)).exists():
            return ProviderStatus(
                name="decimer",
                availability=Availability.ERROR,
                reason=f"the DECIMER worker script is missing from {self.worker_script}",
            )
        return ProviderStatus(name="decimer", availability=Availability.AVAILABLE)

    @property
    def name(self) -> str:
        return "decimer"

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.STRUCTURE_RECOGNITION})

    @property
    def requires_key(self) -> bool:
        return False

    # -- reading ---------------------------------------------------------

    def read(self, provenance: ImageProvenance) -> str:
        """Exactly what the recogniser emitted, and nothing else.

        The service's contract: a string, or an exception. No cleanup, no
        validation, no repair -- see `recognize()` for the fuller form.
        """
        return self.recognize(provenance).raw_smiles

    def recognize(self, provenance: ImageProvenance) -> RecognitionReading:
        """One reading of one image, with its provenance.

        Raises a typed `crea.engine.errors` exception when the worker cannot
        answer. The service turns any of them into `UNAVAILABLE`, which is the
        right answer for "the recogniser is not here" and for "the recogniser
        broke": both mean no reading was obtained, and neither means the image
        is unreadable.
        """
        status = self.status()
        if status.availability is not Availability.AVAILABLE:
            raise RecognizerUnavailable(status.reason or "no image recogniser")

        image_path = provenance.file_reference
        if not image_path:
            raise RecognizerUnavailable(
                "the image record carries no file reference to read"
            )

        client = self._build_client()
        try:
            response = client.recognize_image(
                image_path=str(image_path),
                image_sha256=provenance.image_sha256,
                source=provenance.source,
                expected_recognizer="decimer",
            )
        except (WorkerUnavailable, WorkerNotConfigured,
                WorkerIncompatibleDependency) as exc:
            raise RecognizerUnavailable(str(exc)) from exc
        except (WorkerCrashed, WorkerTimeout, WorkerProtocolError,
                WorkerReportedError) as exc:
            raise RecognizerFailed(str(exc)) from exc

        result = response.result or {}
        environment = response.environment or {}
        return RecognitionReading(
            raw_smiles=str(result.get("raw_smiles", "") or ""),
            image_sha256=str(result.get("image_sha256", "") or provenance.image_sha256),
            recognizer=str(result.get("recognizer", "") or "decimer"),
            recognizer_version=str(result.get("recognizer_version", "") or ""),
            model_version=str(result.get("model_version", "") or ""),
            elapsed_seconds=float(result.get("elapsed_seconds", 0.0) or 0.0),
            source=str(result.get("source", "") or provenance.source),
            environment=dict(environment),
        )

    def _build_client(self) -> EngineClient:
        if self.client is not None:
            return self.client
        python = self.python_executable or default_decimer_python()
        return EngineClient(
            python_executable=python,
            worker_script=self.worker_script or DEFAULT_DECIMER_WORKER,
            request_timeout=self.request_timeout,
        )


class RecognizerUnavailable(Exception):
    """There is no recogniser to ask. Not a chemistry failure and not a bug.

    Distinct from `RecognizerFailed` because the fix is different: one is
    "install the isolated runtime", the other is "the runtime is broken".
    """


class RecognizerFailed(Exception):
    """The recogniser was asked and did not produce a reading.

    A reading of "" is not this. This is the worker timing out, crashing, or
    answering with something unparseable -- cases where no reading exists to
    judge.
    """


__all__ = [
    "DecimerImageReader",
    "RecognitionReading",
    "RecognizerFailed",
    "RecognizerUnavailable",
    "default_decimer_python",
]

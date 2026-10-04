"""A retrosynthesis provider that reaches the engine over a pipe.

The main process never imports AiZynthFinder. This class turns an `EngineClient`
into a `RetrosynthesisProvider`, so the agent's route path is identical whether
the routes came from the fixture table or from a real tree search -- which is
what makes the offline suite meaningful.

Whatever the engine reports about itself is carried through untouched:
versions, config path, stock info, and the search time it actually took.
"""

from __future__ import annotations

from crea.core.provenance import utc_now_iso
from crea.core.models.route import (
    CandidateRoute,
    MoleculeRole,
    MoleculeSource,
    ProviderProvenance,
    ProviderReason,
    ProviderStatus,
    ReactionStep,
    RetrosynthesisRequest,
    RetrosynthesisResult,
    RouteMolecule,
    ScoreSource,
    StepStatus,
    StockAssumption,
    TransformationCandidate,
    route_identity,
    step_identity,
)
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
from crea.providers.retrosynthesis import BaseRetrosynthesisProvider

#: How each way the worker can fail lands on the two things a caller reads:
#: the status, and the reason. They are not the same question -- a timeout and
#: a missing stock file are both `FAILED`/`NOT_CONFIGURED` respectively, and
#: only one of them is worth retrying.
_STATE_FOR_ERROR = {
    WorkerUnavailable: ProviderStatus.UNAVAILABLE,
    WorkerIncompatibleDependency: ProviderStatus.UNAVAILABLE,
    WorkerNotConfigured: ProviderStatus.NOT_CONFIGURED,
    WorkerCrashed: ProviderStatus.FAILED,
    WorkerTimeout: ProviderStatus.FAILED,
    WorkerProtocolError: ProviderStatus.FAILED,
}

_REASON_FOR_ERROR = {
    WorkerUnavailable: ProviderReason.MISSING_RUNTIME,
    WorkerIncompatibleDependency: ProviderReason.INCOMPATIBLE_DEPENDENCY,
    # `not_configured` does not say *which* of the policy, the stock list or
    # the config file was absent, and reading that out of the message text
    # would be guessing. The specific codes are there for a caller that knows
    # -- and does not claim to know on the engine's behalf.
    WorkerNotConfigured: ProviderReason.INVALID_CONFIGURATION,
    WorkerCrashed: ProviderReason.WORKER_CRASH,
    WorkerTimeout: ProviderReason.WORKER_TIMEOUT,
    WorkerProtocolError: ProviderReason.PROTOCOL_ERROR,
    WorkerReportedError: ProviderReason.UNKNOWN,
}


def _classify(error) -> tuple[ProviderStatus, ProviderReason]:
    return (_STATE_FOR_ERROR.get(type(error), ProviderStatus.FAILED),
            _REASON_FOR_ERROR.get(type(error), ProviderReason.UNKNOWN))


class WorkerRetrosynthesisProvider(BaseRetrosynthesisProvider):
    """Runs the engine somewhere else and converts what it sends back."""

    name = "aizynthfinder-worker"

    def __init__(self, client: EngineClient | None = None,
                 model_version: str | None = None,
                 stock_configuration: str | None = None) -> None:
        super().__init__()
        self._client = client or EngineClient()
        self.model_version = model_version
        self.stock_configuration = stock_configuration
        self._environment: dict = {}
        self.version: str | None = None
        self._reason_code: ProviderReason = ProviderReason.NONE

    # -- availability ------------------------------------------------------

    def probe(self) -> tuple[ProviderStatus, str]:
        """Ask the worker what it can do. The answer is cached in the result."""
        try:
            info = self._client.info()
        except WorkerUnavailable as exc:
            self._reason_code = ProviderReason.MISSING_RUNTIME
            return ProviderStatus.UNAVAILABLE, str(exc)
        except WorkerIncompatibleDependency as exc:
            self._reason_code = ProviderReason.INCOMPATIBLE_DEPENDENCY
            return ProviderStatus.UNAVAILABLE, str(exc)
        except WorkerNotConfigured as exc:
            self._reason_code = ProviderReason.INVALID_CONFIGURATION
            return ProviderStatus.NOT_CONFIGURED, str(exc)
        except (WorkerCrashed, WorkerTimeout, WorkerProtocolError,
                WorkerReportedError) as exc:
            state, reason_code = _classify(exc)
            self._reason_code = reason_code
            return state, f"{type(exc).__name__}: {exc}"

        self._environment = dict(info.environment)
        self.version = info.environment.get("aizynthfinder")
        if not info.result.get("usable", True):
            # The worker started and can import, but told us it would not run.
            self._reason_code = ProviderReason.INCOMPATIBLE_DEPENDENCY
            return ProviderStatus.UNAVAILABLE, str(
                info.result.get("problem", "the engine is not usable here"))
        self._reason_code = ProviderReason.NONE
        return ProviderStatus.AVAILABLE, ""

    def availability(self) -> ProviderStatus:
        return self.probe()[0]

    @property
    def reason_code(self) -> ProviderReason:
        """Why the last probe answered as it did."""
        return self._reason_code

    # -- the work ----------------------------------------------------------

    def retrosynthesize(self, request: RetrosynthesisRequest) -> RetrosynthesisResult:
        status, reason = self.probe()
        if status is not ProviderStatus.AVAILABLE:
            return self.empty_result(
                request, status, reason or "the worker could not start",
                reason_code=self._reason_code,
                provider_version=self.version,
                model_version=self.model_version,
                stock_configuration=self.stock_configuration,
            )

        try:
            response = self._client.retrosynthesize(
                target_smiles=request.target_smiles,
                max_routes=request.max_routes,
                max_transforms=request.max_transforms,
                config_file=request.config_file,
            )
        except (WorkerUnavailable, WorkerNotConfigured, WorkerCrashed,
                WorkerTimeout, WorkerProtocolError, WorkerIncompatibleDependency,
                WorkerReportedError) as exc:
            state, reason_code = _classify(exc)
            return self.empty_result(
                request, state, f"{type(exc).__name__}: {exc}",
                reason_code=reason_code,
                provider_version=self.version,
                model_version=self.model_version,
                stock_configuration=self.stock_configuration,
            )

        payload = response.result
        environment = dict(response.environment or self._environment)
        target = self._target(request.target_smiles)
        routes = [self._route(item, target) for item in payload.get("routes", [])]

        return RetrosynthesisResult(
            request=request,
            status=ProviderStatus.AVAILABLE,
            routes=routes,
            provenance=ProviderProvenance(
                provider=self.name,
                provider_version=environment.get("aizynthfinder") or self.version,
                model_version=self.model_version,
                configuration=payload.get("config_file") or request.config_file or "",
                stock_configuration=self.stock_configuration or "",
                raw_result_hash=_hash(payload),
                generated_at=utc_now_iso(),
            ),
            warnings=[
                # measured, not estimated: how long the engine actually took
                f"engine search took {payload.get('search_seconds')}s",
                f"engine python {environment.get('python')}, "
                f"rdkit {environment.get('rdkit')}, "
                f"aizynthfinder {environment.get('aizynthfinder')}",
                "routes are the engine's candidates; no literature has been "
                "consulted and no conditions are known",
            ],
        )

    # -- conversion --------------------------------------------------------

    def _route(self, raw: dict, target: RouteMolecule) -> CandidateRoute:
        steps = []
        for item in raw.get("steps", []) or []:
            reactants = [s for s in (item.get("reactants") or []) if s]
            products = [s for s in (item.get("products") or []) if s]
            index = int(item.get("index") or 0) + 1
            reaction_smiles = item.get("reaction_smiles")
            steps.append(ReactionStep(
                step_id=step_identity(target.canonical_smiles, index,
                                      reactants, products),
                step_index=index,
                products=[self._mol(s, MoleculeRole.PRECURSOR) for s in products],
                reactants=[self._mol(s, MoleculeRole.PRECURSOR) for s in reactants],
                transformation=TransformationCandidate(
                    reactant_smiles=".".join(reactants),
                    product_smiles=".".join(products),
                    # copied when the engine gave one, absent when it did not
                    reaction_smiles=reaction_smiles,
                    template=item.get("smarts"),
                    template_source=self.name if item.get("smarts") else "UNKNOWN",
                    transformation_class="UNKNOWN",
                    transformation_class_source="UNKNOWN",
                    source=self.name,
                ),
                source=self.name,
                score=_number(item.get("score")),
                score_source=(ScoreSource.AIZYNTHFINDER
                              if item.get("score") is not None else ScoreSource.NONE),
                stock_assumptions={},   # the worker reports stock separately
                # CANDIDATE. The engine proposing a step is the whole of what
                # it means, and nothing here may change that.
                status=StepStatus.CANDIDATE,
                support_level="no_support",
                warnings=["proposed by the retrosynthesis engine; no literature "
                          "has been consulted for this step"],
            ))

        return CandidateRoute(
            route_id=route_identity(target.canonical_smiles, self.name,
                                    [s.step_id for s in steps]),
            target=target,
            steps=steps,
            score=_number(raw.get("score")),
            score_source=(ScoreSource.AIZYNTHFINDER
                          if raw.get("score") is not None else ScoreSource.NONE),
            source=self.name,
            provider_version=self.version,
            stock_configuration=self.stock_configuration or "",
            warnings=["candidate route from the retrosynthesis engine; its score "
                      "is the engine's own heuristic and says nothing about "
                      "whether the chemistry works"],
        )

    @staticmethod
    def _target(smiles: str) -> RouteMolecule:
        from crea.chem.normalize import normalize

        try:
            molecule = normalize(smiles)
            return RouteMolecule(molecule_id=molecule.mol_id, smiles=smiles,
                                 canonical_smiles=molecule.canonical_smiles,
                                 role=MoleculeRole.TARGET,
                                 source=MoleculeSource.RETROSYNTHESIS)
        except Exception:  # noqa: BLE001
            return RouteMolecule(molecule_id=smiles, smiles=smiles,
                                 role=MoleculeRole.TARGET,
                                 source=MoleculeSource.RETROSYNTHESIS)

    @staticmethod
    def _mol(smiles: str, role: MoleculeRole) -> RouteMolecule:
        from crea.chem.normalize import normalize

        try:
            parsed = normalize(smiles)
            return RouteMolecule(molecule_id=parsed.mol_id, smiles=smiles,
                                 canonical_smiles=parsed.canonical_smiles, role=role,
                                 source=MoleculeSource.RETROSYNTHESIS)
        except Exception:  # noqa: BLE001
            return RouteMolecule(molecule_id=smiles, smiles=smiles, role=role,
                                 source=MoleculeSource.RETROSYNTHESIS)


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hash(payload) -> str:
    import json

    from crea.core.ids import content_hash

    return content_hash(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                  default=str), 20)

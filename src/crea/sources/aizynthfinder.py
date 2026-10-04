"""AiZynthFinder adapter.

The adapter converts. It does not search literature, does not reason, does not
read a PDF, does not create Evidence and does not build a ReactionRecord. Its
whole job is `CREA request -> engine -> CREA CandidateRoute`, plus a truthful
status when that is not possible.

## Status in this environment (verified 2026-10-01)

| Fact | How it was established |
|---|---|
| version **4.4.1** | PyPI JSON |
| licence **MIT** | PyPI classifier **and** the GitHub repo's licence field |
| Python **3.10–3.12** | README ("python 3.10 - 3.12") and `requires_python <3.13,>=3.10` |
| CLI `aizynthcli --config … --smiles …`, `aizynthapp` | README |
| needs stock + expansion policy (+ optional filter policy) | README |
| models from figshare, or `download_public_data <dir>` which writes `config.yml` | README |
| `AiZynthFinder(configfile=None, configdict=None)` | `aizynthfinder/aizynthfinder.py`, source |
| `.target_smiles`, `.tree_search(show_progress=False) -> float`, `.build_routes(selection=None, scorer=None)`, `.routes` | same |
| nodes expose `.is_solved`, `.children`, `.score`; reactions expose `.reaction_smiles()` | `search/mcts/node.py`, `chem/reaction.py` |

### It cannot be installed here

`aizynthfinder` 4.4.1 requires **`rdkit>=2023.9.1,<2024.0.0`**. This project
pins **`rdkit==2026.3.6`**, and the Chemical Core depends on that version's
behaviour — the aromatic-versus-aliphatic bond comparison in
`core/policy/document.py` and the MCS parameter choices in `chem/align.py` were
both verified against it.

Installing it would mean downgrading RDKit by two major releases and re-verifying
the whole of Phase 1. That is a decision for a person, not a default. So this
adapter reports `UNAVAILABLE` with the reason, the offline suite uses the fixture
provider, and nothing is faked.

**Version compatibility is UNVERIFIED for any other release** -- the constraint
above is what 4.4.1 declares, not a general statement about the project.
"""

from __future__ import annotations

import importlib

from crea.core.ids import content_hash
from crea.core.models.route import (
    CandidateRoute,
    MoleculeRole,
    MoleculeSource,
    ProviderStatus,
    ReactionStep,
    RetrosynthesisRequest,
    RetrosynthesisResult,
    RouteMolecule,
    ScoreSource,
    StepStatus,
    TransformationCandidate,
    route_identity,
    step_identity,
)
from crea.providers.retrosynthesis import BaseRetrosynthesisProvider

#: What 4.4.1 declares. Recorded here so a future reader does not have to
#: rediscover it, and so the reason string can be specific.
REQUIRED_RDKIT_SPEC = ">=2023.9.1,<2024.0.0"
PROJECT_RDKIT_SPEC = "==2026.3.6"


class AiZynthFinderAdapter(BaseRetrosynthesisProvider):
    name = "aizynthfinder"

    def __init__(self, config_file: str | None = None,
                 stock_file: str | None = None,
                 model_version: str | None = None,
                 client=None) -> None:
        super().__init__()
        self.config_file = config_file
        self.stock_file = stock_file
        self.model_version = model_version
        self._client = client
        self.version = self._version()

    # -- availability ------------------------------------------------------

    @staticmethod
    def _version() -> str | None:
        try:
            return importlib.import_module("aizynthfinder").__version__
        except Exception:  # noqa: BLE001
            return None

    def availability(self) -> ProviderStatus:
        if self._client is not None:
            return ProviderStatus.AVAILABLE
        if self.version is None:
            return ProviderStatus.UNAVAILABLE
        if not self.config_file:
            return ProviderStatus.NOT_CONFIGURED
        return ProviderStatus.AVAILABLE

    def _unavailable_reason(self) -> str:
        return (
            f"aizynthfinder is not installed in this environment. Version 4.4.1 "
            f"declares rdkit{REQUIRED_RDKIT_SPEC}; this project pins "
            f"rdkit{PROJECT_RDKIT_SPEC} and the Chemical Core is verified "
            f"against it. Downgrading RDKit to use it is a deliberate decision, "
            f"not a default. See docs/RETROSYNTHESIS.md."
        )

    # -- the conversion ----------------------------------------------------

    def retrosynthesize(self, request: RetrosynthesisRequest) -> RetrosynthesisResult:
        status = self.availability()
        if status is ProviderStatus.UNAVAILABLE:
            return self.empty_result(
                request, ProviderStatus.UNAVAILABLE, self._unavailable_reason(),
                configuration=self.config_file or "",
            )
        if status is ProviderStatus.NOT_CONFIGURED:
            return self.empty_result(
                request, ProviderStatus.NOT_CONFIGURED,
                "aizynthfinder is installed but no config file was supplied; it "
                "needs a stock file and an expansion policy",
                configuration="",
            )

        try:
            routes = self._run(request)
        except Exception as exc:  # noqa: BLE001 - a provider failure is a result
            return self.empty_result(
                request, ProviderStatus.FAILED,
                f"aizynthfinder failed: {type(exc).__name__}: {exc}",
                configuration=self.config_file or "",
            )

        return RetrosynthesisResult(
            request=request,
            status=ProviderStatus.AVAILABLE,
            routes=routes,
            provenance=self.provenance(
                provider_version=self.version,
                model_version=self.model_version,
                configuration=self.config_file or "",
                stock_configuration=self.stock_file or "",
                raw_result_hash=content_hash(str(len(routes)), 20),
            ),
        )

    def _run(self, request: RetrosynthesisRequest):
        """Drive the engine and convert its output. Nothing else."""
        from aizynthfinder.aizynthfinder import AiZynthFinder

        finder = AiZynthFinder(configfile=self.config_file)
        finder.target_smiles = request.target_smiles
        max_score = finder.tree_search(show_progress=False)
        finder.build_routes()

        target = self._target_molecule(request.target_smiles)
        routes = []
        for raw in list(getattr(finder, "routes", []) or [])[: request.max_routes]:
            routes.append(self._convert_route(raw, target, finder, max_score))
        return routes

    # -- upstream shape -> ours --------------------------------------------

    def _convert_route(self, raw, target: RouteMolecule, finder, max_score) -> CandidateRoute:
        """A route is a node chain: each node holds one retro reaction.

        `reaction_smiles()` is the upstream's own string; it is recorded when
        present and left `None` when not. Nothing here reconstructs one.
        """
        steps: list[ReactionStep] = []
        node = raw
        index = 0
        while node is not None and index < 64:
            index += 1
            reaction = getattr(node, "reaction", None)
            reactants = self._reaction_side(reaction, "reactants")
            products = self._reaction_side(reaction, "products")
            reaction_smiles = None
            if reaction is not None:
                try:
                    reaction_smiles = reaction.reaction_smiles()
                except Exception:  # noqa: BLE001
                    reaction_smiles = None
            steps.append(ReactionStep(
                step_id=step_identity(target.canonical_smiles, index,
                                      reactants, products),
                step_index=index,
                products=[self._mol(s, MoleculeRole.PRECURSOR) for s in products],
                reactants=[self._mol(s, MoleculeRole.PRECURSOR) for s in reactants],
                transformation=TransformationCandidate(
                    reactant_smiles=".".join(reactants),
                    product_smiles=".".join(products),
                    reaction_smiles=reaction_smiles,
                    template=getattr(reaction, "smarts", None) if reaction else None,
                    template_source="aizynthfinder" if reaction else "UNKNOWN",
                    # The engine did not tell us a reaction class, so we do not
                    # have one.
                    transformation_class="UNKNOWN",
                    transformation_class_source="UNKNOWN",
                    source=self.name,
                ),
                source=self.name,
                score=_as_float(getattr(node, "score", None)),
                score_source=ScoreSource.AIZYNTHFINDER,
                # CANDIDATE, always. Promotion needs evidence this adapter does
                # not have and cannot manufacture.
                status=StepStatus.CANDIDATE,
                support_level="no_support",
                warnings=["AiZynthFinder proposed this step; no literature has "
                          "been consulted and no conditions are known"],
            ))
            node = getattr(node, "parent", None)

        return CandidateRoute(
            route_id=route_identity(target.canonical_smiles, self.name,
                                    [s.step_id for s in steps]),
            target=target,
            steps=steps,
            score=_as_float(getattr(raw, "score", None)) or _as_float(max_score),
            score_source=ScoreSource.AIZYNTHFINDER,
            source=self.name,
            provider_version=self.version,
            stock_configuration=self.stock_file or "",
            warnings=["candidate route from a retrosynthesis engine; a score here "
                      "is that engine's own heuristic and says nothing about "
                      "whether the chemistry works"],
        )

    @staticmethod
    def _reaction_side(reaction, side: str) -> list[str]:
        if reaction is None:
            return []
        try:
            groups = getattr(reaction, side)()
        except Exception:  # noqa: BLE001
            return []
        out: list[str] = []
        for group in groups or ():
            for molecule in group:
                smiles = getattr(molecule, "smiles", None)
                if smiles:
                    out.append(smiles)
        return out

    @staticmethod
    def _target_molecule(smiles: str) -> RouteMolecule:
        from crea.chem.normalize import normalize

        try:
            molecule = normalize(smiles)
            return RouteMolecule(
                molecule_id=molecule.mol_id, smiles=smiles,
                canonical_smiles=molecule.canonical_smiles,
                role=MoleculeRole.TARGET,
                source=MoleculeSource.RETROSYNTHESIS,
            )
        except Exception:  # noqa: BLE001
            return RouteMolecule(molecule_id=smiles, smiles=smiles,
                                 role=MoleculeRole.TARGET,
                                 source=MoleculeSource.RETROSYNTHESIS)

    @staticmethod
    def _mol(smiles: str, role: MoleculeRole) -> RouteMolecule:
        from crea.chem.normalize import normalize

        try:
            parsed = normalize(smiles)
            return RouteMolecule(
                molecule_id=parsed.mol_id, smiles=smiles,
                canonical_smiles=parsed.canonical_smiles, role=role,
                source=MoleculeSource.RETROSYNTHESIS,
            )
        except Exception:  # noqa: BLE001
            return RouteMolecule(molecule_id=smiles, smiles=smiles, role=role,
                                 source=MoleculeSource.RETROSYNTHESIS)


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
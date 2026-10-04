"""An offline retrosynthesis provider that is a fixture, not an engine.

It holds a table of pre-written routes. It does not reason about chemistry, does
not guess a disconnection, and returns no route at all for a target it has no
entry for -- which is the honest answer and the one that keeps the offline
suite from quietly testing against invented reactions.

Every route it produces is `CANDIDATE`. There is no code path here that could
promote one, because promotion is the validation service's job and it needs
evidence this provider does not have.
"""

from __future__ import annotations

from crea.core.ids import normalize_doi  # noqa: F401  (kept for symmetry of use)
from crea.core.models.route import (
    CandidateRoute,
    MoleculeRole,
    MoleculeSource,
    ProviderProvenance,
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
from crea.providers.retrosynthesis import BaseRetrosynthesisProvider

#: 1,4-diphenyl-1,3-diyne, made from two equivalents of phenylacetylene by a
#: Glaser-type oxidative homocoupling. Written out by hand as a *fixture*; the
#: point of the demo is the evidence plumbing, not this disconnection.
_DIYNE = "c1ccccc1C#CC#Cc2ccccc2"
_PHENYLACETYLENE = "C#Cc1ccccc1"

DEMO_ROUTES: dict[str, list[dict]] = {
    _DIYNE: [
        {
            "score": 0.87,
            "steps": [
                {
                    "reactants": [_PHENYLACETYLENE, _PHENYLACETYLENE],
                    "products": [_DIYNE],
                    "reagents": [],
                    "reaction_smiles": None,
                    "template": None,
                    "stock": {"C#Cc1ccccc1": StockAssumption.PROVIDER_STOCK_LIST},
                },
                {
                    "reactants": ["CC#N", "CC#N"],
                    "products": [_PHENYLACETYLENE],
                    "reagents": [],
                    "reaction_smiles": None,
                    "template": None,
                    "stock": {"CC#N": StockAssumption.PROVIDER_STOCK_LIST},
                },
            ],
        },
        {
            "score": 0.41,
            "steps": [
                {
                    "reactants": ["O=C(O)c1ccccc1", "ClCCCCCC"],
                    "products": [_PHENYLACETYLENE],
                    "reagents": [],
                    "reaction_smiles": None,
                    "template": None,
                    "stock": {
                        "O=C(O)c1ccccc1": StockAssumption.NOT_IN_STOCK_LIST,
                        "ClCCCCCC": StockAssumption.UNKNOWN,
                    },
                },
            ],
        },
    ],
}


class FakeRetrosynthesisProvider(BaseRetrosynthesisProvider):
    """Replays a fixture table."""

    name = "fake-retrosynthesis"
    version = "fixture-v1"

    def __init__(self, table: dict[str, list[dict]] | None = None) -> None:
        # Keys are normalised on the way in, so a caller can write a target in
        # whatever SMILES form they have and still hit the fixture.
        source = table if table is not None else DEMO_ROUTES
        self._table = {_canonical(k): v for k, v in source.items()}

    def availability(self) -> ProviderStatus:
        return ProviderStatus.AVAILABLE

    def retrosynthesize(self, request: RetrosynthesisRequest) -> RetrosynthesisResult:
        from crea.chem.normalize import normalize

        try:
            molecule = normalize(request.target_smiles)
        except Exception as exc:  # noqa: BLE001
            return self.empty_result(
                request, ProviderStatus.FAILED,
                f"target could not be parsed: {exc}")

        target = molecule.canonical_smiles
        specs = self._table.get(target)
        if specs is None:
            return self.empty_result(
                request, ProviderStatus.AVAILABLE,
                f"the fake provider holds no route for {target}; it is a fixture, "
                f"not a retrosynthesis engine",
                raw_result_hash=None,
            )

        routes = [self._build_route(target, molecule, spec, index)
                  for index, spec in enumerate(specs)][: request.max_routes]
        return RetrosynthesisResult(
            request=request,
            status=ProviderStatus.AVAILABLE,
            routes=routes,
            provenance=self.provenance(
                configuration="fixture table",
                stock_configuration="declared per step",
                raw_result_hash=_hash(routes),
            ),
            warnings=["routes are fixtures from a fake provider, not "
                      "retrosynthetic analysis"],
        )

    def _build_route(self, target_smiles, molecule, spec, index) -> CandidateRoute:
        steps = []
        # the fixture writes the diyne as `c1ccccc1C#CC#Cc2ccccc2`; RDKit's
        # canonical form differs, so both sides are canonicalised here or the
        # route will not join up with the target it claims to make.
        def _c(smiles: str) -> str:
            return _canonical(smiles)

        for position, step_spec in enumerate(spec.get("steps", []), start=1):
            reactants = [_c(s) for s in step_spec.get("reactants", [])]
            products = [_c(s) for s in step_spec.get("products", [])]
            reagents = [_c(s) for s in step_spec.get("reagents", [])]
            steps.append(ReactionStep(
                step_id=step_identity(target_smiles, position, reactants, products),
                step_index=position,
                products=[self._mol(s, MoleculeRole.PRECURSOR)
                           for s in products],
                reactants=[self._mol(s, MoleculeRole.PRECURSOR)
                           for s in reactants],
                reagents=[self._mol(s, MoleculeRole.REAGENT)
                          for s in reagents],
                transformation=TransformationCandidate(
                    reactant_smiles=".".join(reactants),
                    product_smiles=".".join(products),
                    # None unless the provider gave one. A model filling this
                    # in from its own chemistry would put an unverified class
                    # into a structure that otherwise looks measured.
                    reaction_smiles=step_spec.get("reaction_smiles"),
                    template=step_spec.get("template"),
                    transformation_class="UNKNOWN",
                    transformation_class_source="UNKNOWN",
                    source=self.name,
                ),
                source=self.name,
                score=spec.get("score"),
                score_source=(ScoreSource.UNKNOWN if spec.get("score") is not None
                              else ScoreSource.NONE),
                stock_assumptions={
                    _c(k): v for k, v in (step_spec.get("stock") or {}).items()},
                status=StepStatus.CANDIDATE,     # and nothing may change that here
                support_level="no_support",
                warnings=["proposed by a fake provider; no literature has been "
                          "consulted for this step"],
            ))

        return CandidateRoute(
            route_id=route_identity(
                target_smiles, self.name,
                [s.step_id for s in steps]),
            target=RouteMolecule(
                molecule_id=molecule.mol_id,
                smiles=request_smiles_or(molecule),
                canonical_smiles=target_smiles,
                role=MoleculeRole.TARGET,
                source=MoleculeSource.RETROSYNTHESIS,
            ),
            steps=steps,
            score=spec.get("score"),
            score_source=(ScoreSource.UNKNOWN if spec.get("score") is not None
                          else ScoreSource.NONE),
            source=self.name,
            provider_version=self.version,
            stock_configuration="declared per step",
            warnings=["candidate route from a fixture provider; nothing here has "
                      "been checked against literature"],
        )

    @staticmethod
    def _mol(smiles: str, role: MoleculeRole) -> RouteMolecule:
        from crea.chem.normalize import normalize

        try:
            canonical = normalize(smiles).canonical_smiles
            molecule_id = normalize(smiles).mol_id
        except Exception:  # noqa: BLE001
            canonical, molecule_id = smiles, smiles
        return RouteMolecule(
            molecule_id=molecule_id, smiles=smiles, canonical_smiles=canonical,
            role=role, source=MoleculeSource.RETROSYNTHESIS,
        )


def request_smiles_or(molecule) -> str:
    return molecule.canonical_smiles


def _canonical(smiles: str) -> str:
    from crea.chem.normalize import normalize

    try:
        return normalize(smiles).canonical_smiles
    except Exception:  # noqa: BLE001
        return smiles


def _hash(routes) -> str:
    from crea.core.ids import content_hash

    return content_hash(str([r.model_dump(mode="json") for r in routes]), 20)
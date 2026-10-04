"""Route explorer (CREA Stage 5.4).

A **bounded, forward-only** search over the known reactions:

    start molecule -> reaction -> known product -> reaction -> ... -> target

Every step's product comes from a deterministic reaction transform on the
template (`chem.reaction_transform`); when a template has no transform the
path stops as `PARTIAL`/`BLOCKED` -- a product is never guessed (§14/§15/§30).
Candidate reactions come from `TransformationSearchService` (5.1), so this is
the one structural-matching entry point. No AI, no provider, no network, no
retrosynthesis, no recommendation (§一/§十/§二十七/§三十二/§三十三).

Target matching reuses the project's standard identity (InChI key), so `CCO`
and `OCC` are one molecule (§16/§18).
"""

from __future__ import annotations

from crea.chem.reaction_transform import apply_reaction_smarts
from crea.core.errors import RouteExplorerError
from crea.core.ids import content_hash
from crea.core.models.entity import identity_key
from crea.core.models.route_explorer import (
    ExploredRoute,
    ExploredRouteStep,
    RouteExplorerResult,
    RouteStatus,
)

#: The route depth cap in *chemical steps* (§十一). Out of range is refused.
MAX_STEPS = 3
#: A bound on how many paths one search returns.
MAX_ROUTES = 25

_REASON_PRODUCT = "PRODUCT_STRUCTURE_UNAVAILABLE"
_REASON_NO_NEXT = "NO_NEXT_REACTION"
_REASON_MAX_STEPS = "MAX_STEPS_REACHED"

_STATUS_RANK = {
    RouteStatus.COMPLETE: 0, RouteStatus.PARTIAL: 1, RouteStatus.BLOCKED: 2}


class RouteExplorerService:
    """Explore forward routes between two molecules over the known reactions."""

    def __init__(self, kb=None, search_service=None) -> None:
        self._kb = kb
        if search_service is None:
            from crea.services.reaction_evidence import ReactionEvidenceService
            from crea.services.transformation_search import TransformationSearchService

            search_service = TransformationSearchService(
                evidence_service=ReactionEvidenceService(kb=kb))
        self._search = search_service

    def explore(self, start_smiles: str, target_smiles: str,
                max_steps: int = MAX_STEPS) -> RouteExplorerResult:
        """Forward routes from `start_smiles` to `target_smiles`."""
        from crea.chem.normalize import normalize

        try:
            steps_cap = int(max_steps)
        except (TypeError, ValueError):
            raise RouteExplorerError(
                "INVALID_ROUTE_DEPTH", f"max_steps must be an integer")
        if not 1 <= steps_cap <= MAX_STEPS:
            raise RouteExplorerError(
                "INVALID_ROUTE_DEPTH",
                f"max_steps must be between 1 and {MAX_STEPS}")

        start = normalize(start_smiles)
        target = normalize(target_smiles)
        target_key = identity_key(target.inchi)

        paths: list[ExploredRoute] = []
        if identity_key(start.inchi) == target_key:
            paths.append(self._route(start, target, [], RouteStatus.COMPLETE, ""))
        else:
            self._dfs(start, target, target_key, steps_cap, [],
                      {identity_key(start.inchi)}, paths)

        deduped = self._dedup(paths)
        deduped.sort(key=lambda r: (_STATUS_RANK[r.status], len(r.steps),
                                    r.signature))
        result = RouteExplorerResult(
            start_smiles=start.canonical_smiles,
            target_smiles=target.canonical_smiles, max_steps=steps_cap,
            paths=deduped)
        if not deduped:
            result.diagnostics.append("no deterministic route found")
        elif not any(p.status is RouteStatus.COMPLETE for p in deduped):
            result.diagnostics.append(
                "no complete route: no reaction in the queried data yields a "
                "deterministic product toward the target")
        return result

    # -- search -------------------------------------------------------------

    def _dfs(self, current, target, target_key: str, steps_cap: int,
             steps: list, visited: set, paths: list) -> None:
        if len(paths) >= MAX_ROUTES:
            return
        from crea.chem.normalize import normalize

        candidates = self._search.search(current.canonical_smiles).candidates
        if not candidates:
            paths.append(self._route(
                current, target, steps, RouteStatus.BLOCKED, _REASON_NO_NEXT))
            return

        for candidate in candidates:
            template = candidate.reaction_template
            product_smiles = apply_reaction_smarts(template.reaction_smarts,
                                                   current)
            if product_smiles is None:
                # a matched reaction whose product is not deterministically
                # known: the path stops here, honestly, as PARTIAL
                paths.append(self._route(
                    current, target, list(steps), RouteStatus.PARTIAL,
                    _REASON_PRODUCT, blocked_reaction_id=template.template_id))
                continue

            product = normalize(product_smiles)
            product_key = identity_key(product.inchi)
            step = ExploredRouteStep(
                step_index=len(steps) + 1,
                reaction_template_id=template.template_id,
                reaction_name=template.name,
                reaction_class=template.reaction_class,
                input_molecule_id=identity_key(current.inchi),
                input_smiles=current.canonical_smiles,
                output_molecule_id=product_key,
                output_smiles=product.canonical_smiles,
                match_level=candidate.match_level.value,
                evidence_count=candidate.evidence_count,
                evidence_summary=dict(candidate.evidence_summary))

            if product_key == target_key:
                paths.append(self._route(
                    current, target, steps + [step], RouteStatus.COMPLETE, ""))
                continue
            if len(steps) + 1 >= steps_cap:
                paths.append(self._route(
                    current, target, steps + [step], RouteStatus.PARTIAL,
                    _REASON_MAX_STEPS))
                continue
            if product_key in visited:      # cycle avoidance (§18)
                continue
            self._dfs(product, target, target_key, steps_cap, steps + [step],
                      visited | {product_key}, paths)

    # -- helpers ------------------------------------------------------------

    def _route(self, start, target, steps, status, blocked_reason,
               blocked_reaction_id: str = "") -> ExploredRoute:
        signature = "|".join(s.reaction_template_id for s in steps) + "@" + ">".join(
            [s.input_molecule_id for s in steps]
            + ([steps[-1].output_molecule_id] if steps else []))
        start_key = steps[0].input_molecule_id if steps else identity_key(start.inchi)
        start_smiles = steps[0].input_smiles if steps else start.canonical_smiles
        return ExploredRoute(
            route_id="route-" + content_hash(signature + status.value, 16),
            status=status,
            start_molecule_id=start_key, start_smiles=start_smiles,
            target_molecule_id=identity_key(target.inchi),
            target_smiles=target.canonical_smiles,
            steps=list(steps), blocked_reason=blocked_reason,
            blocked_reaction_id=blocked_reaction_id,
            signature=signature or f"{start_key}->{identity_key(target.inchi)}")

    @staticmethod
    def _dedup(paths: list[ExploredRoute]) -> list[ExploredRoute]:
        seen: set[tuple[str, str]] = set()
        out: list[ExploredRoute] = []
        for path in paths:
            key = (path.signature, path.status.value)
            if key in seen:
                continue
            seen.add(key)
            out.append(path)
        return out


__all__ = ["MAX_ROUTES", "MAX_STEPS", "RouteExplorerService"]

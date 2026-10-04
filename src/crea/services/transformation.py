"""Evidence-backed transformation search (CREA 4.0 Stage H).

Given reactant and product entity ids, this service asks the existing
`ReactionRegistry` whether a stored `Reaction` directly connects them -- and
if not, whether the entities have related reaction history. It is one hop
only (§1/§27): never a route, never recursive, never a ranking.

The fact/inference boundary is the whole point (§10): a stored Reaction is
`DOCUMENTED`; nothing here infers a transformation from similarity, scaffold
or fragment relation, and nothing here says "no reaction exists" -- only
"no evidence in the queried source" or "could not search" (§24).
"""

from __future__ import annotations

from crea.core.ids import content_hash
from crea.core.models.transformation import (
    Transformation,
    TransformationDirection,
    TransformationEvidenceLevel,
    TransformationSearchResult,
    TransformationStatus,
)


class TransformationEvidenceService:
    """The one-hop transformation query over the reaction registry (§7)."""

    def __init__(self, reactions=None) -> None:
        self._reactions = reactions

    def find_between(self, candidate_entity_id: str,
                     target_entity_id: str) -> TransformationSearchResult:
        """Convenience form: candidate -> target (§7)."""
        return self.find_transformations([candidate_entity_id],
                                         [target_entity_id])

    def find_transformations(self, reactant_entity_ids: list[str],
                             product_entity_ids: list[str]
                             ) -> TransformationSearchResult:
        """Find stored reactions that connect the named reactants to the named
        products, in either direction (§13: n:m subset semantics inherited
        from Stage D's `find_between`)."""
        reactants = [r for r in (reactant_entity_ids or []) if r]
        products = [p for p in (product_entity_ids or []) if p]
        if not reactants or not products:
            return _result(TransformationStatus.INVALID, diagnostics=[
                "at least one reactant and one product entity id are required"])
        if set(reactants) & set(products):
            return _result(TransformationStatus.INVALID, diagnostics=[
                "the reactant and product sets overlap; a transformation "
                "needs distinct sides"])

        if self._reactions is None:
            return _result(TransformationStatus.TRANSFORMATION_SEARCH_UNAVAILABLE,
                           diagnostics=["no reaction knowledge source is wired"])

        try:
            forward = self._reactions.find_between(reactants, products)
            reverse = self._reactions.find_between(products, reactants)
        except Exception as exc:  # noqa: BLE001 - an outage is unavailability
            return _result(
                TransformationStatus.TRANSFORMATION_SEARCH_UNAVAILABLE,
                diagnostics=[f"reaction search failed: "
                             f"{type(exc).__name__}: {exc}"])

        transformations = (
            [self._make(reaction, TransformationDirection.FORWARD)
             for reaction in forward]
            + [self._make(reaction, TransformationDirection.REVERSE)
               for reaction in reverse])

        if transformations:
            return TransformationSearchResult(
                status=TransformationStatus.DIRECT_TRANSFORMATION,
                transformations=transformations,
                matched_reactions=[t.reaction.reaction_id
                                   for t in transformations],
                evidence=_aggregate_evidence(transformations),
                provenance=[t.provenance for t in transformations
                            if t.provenance])

        # No direct link. Reaction history for either side is "related", not
        # "direct" -- never turned into a route (§6.2).
        related = []
        for entity_id in set(reactants) | set(products):
            related.extend(self._reactions.find_by_component_entity(entity_id))
        if related:
            ids = list(dict.fromkeys(r.reaction_id for r in related))
            return _result(
                TransformationStatus.RELATED_TRANSFORMATION,
                matched_reactions=ids,
                diagnostics=[f"no direct transformation; {len(ids)} related "
                             f"reaction(s) involve these entities"])

        return _result(
            TransformationStatus.NO_TRANSFORMATION_EVIDENCE,
            diagnostics=["no reaction connecting the queried entities was "
                         "found in the queried source"])

    def _make(self, reaction, direction: TransformationDirection) -> Transformation:
        return Transformation(
            transformation_id="TRANS-" + content_hash(
                f"{reaction.reaction_id}|{direction.value}", 16),
            reaction=reaction,
            direction=direction,
            evidence_level=TransformationEvidenceLevel.DOCUMENTED,
            evidence=[{"evidence_id": ref} for ref in reaction.evidence_refs],
            provenance=(reaction.provenance.model_dump(mode="json")
                        if reaction.provenance else None),
            diagnostics=[(
                f"reaction {reaction.reaction_id} records this transformation"
                if direction is TransformationDirection.FORWARD else
                f"reaction {reaction.reaction_id} records the reverse of this "
                f"transformation")])


def _aggregate_evidence(transformations) -> list[dict]:
    seen: set[str] = set()
    out = []
    for transformation in transformations:
        for ref in transformation.reaction.evidence_refs:
            if ref not in seen:
                seen.add(ref)
                out.append({"evidence_id": ref})
    return out


def _result(status: TransformationStatus, *, matched_reactions=(),
            diagnostics=()) -> TransformationSearchResult:
    return TransformationSearchResult(
        status=status,
        matched_reactions=list(matched_reactions),
        diagnostics=list(diagnostics))


__all__ = ["TransformationEvidenceService"]

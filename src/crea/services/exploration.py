"""Chemical exploration (CREA 4.0 Stage G): the space around a target.

Stage G is *structure exploration*, explicitly not retrosynthesis (§1/§31/
§32). Given a target `ChemicalEntity`, `ChemicalExplorer` enumerates
candidate structures from a `ChemicalSpace`, resolves each through the
existing identity pipeline (never `new ChemicalEntity`, §10), computes the
structural relationship (similarity / scaffold / fragment) with the
existing chemistry services (never a second RDKit, §8), and looks up what
the reaction knowledge actually holds (direct / related / none /
unavailable, §13/§14). It is a domain capability -- no agent, no web, no
workspace (§29/§30) -- and it is single-layer: target -> candidates, never
candidates -> their candidates (§31).

Sorting is a *display order*, not a chemical score (§22): direct reaction
evidence first, then similarity descending.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from crea.core.models.entity import ChemicalEntity, identity_key
from crea.core.models.exploration import (
    Candidate,
    ExplorationResult,
    ReactionEvidenceStatus,
    RelationshipKind,
)
from crea.core.models.identity import StructureInput, StructureResolutionStatus

#: The default cap on returned candidates (§23). Bounded, stated, never the
#: whole space.
MAX_CANDIDATES = 10

#: A candidate must bear *some* structural relationship to the target to be
#: returned: similarity at or above this Tanimoto floor, a shared/related
#: scaffold, or a shared fragment. A molecule in the space with none of the
#: three is not "around" the target and is dropped, counted, and reported.
SIMILARITY_THRESHOLD = 0.2


@runtime_checkable
class ChemicalSpace(Protocol):
    """Where candidate structures come from (§9).

    A source is honest about its name: `"local"` for the knowledge base,
    a provider name for a real provider, never one wrapped over the other.
    """

    name: str

    def candidate_smiles(self, target: ChemicalEntity) -> list[str]: ...


class LocalEntitySpace:
    """Candidates = the other entities already in the knowledge base.

    The MVP's honest default (§9): structure search over the local chemical
    space, labelled `source = "local"`, never presented as a provider.
    """

    name = "local"

    def __init__(self, entity_registry) -> None:
        self._entities = entity_registry

    def candidate_smiles(self, target: ChemicalEntity) -> list[str]:
        out = []
        for entity_id in self._entities.entity_ids:
            if entity_id == target.entity_id:
                continue
            entity = self._entities.get(entity_id)
            if entity.canonical_smiles:
                out.append(entity.canonical_smiles)
        return out


class FixedChemicalSpace:
    """A fixed set of candidate SMILES (tests, demo datasets) (§28)."""

    name = "fixture"

    def __init__(self, smiles: list[str], name: str = "fixture") -> None:
        self._smiles = list(smiles)
        self.name = name

    def candidate_smiles(self, target: ChemicalEntity) -> list[str]:
        return list(self._smiles)


def scaffold_label(relation: str) -> str:
    """The descriptive scaffold answer, from `compare_scaffolds` (§7 B)."""
    if relation in ("identical", "generic_match"):
        return "same"
    if relation in ("substructure", "ring_system_shared"):
        return "related"
    return "different"


def _primary_relationship(scaffold: str, fragment_related: bool) -> RelationshipKind:
    if scaffold == "same":
        return RelationshipKind.SAME_SCAFFOLD
    if fragment_related:
        return RelationshipKind.FRAGMENT_RELATED
    return RelationshipKind.SIMILAR


class ChemicalExplorer:
    """Explore the structure space around one target entity (§4).

    Dependencies are injected: an entity registry (target + candidate
    identity), a reaction registry (reaction evidence), a chemical space
    (candidate source) and the identity service (candidate resolution). All
    are the existing seams -- nothing here re-implements RDKit similarity,
    scaffold or fragment comparison (§8).
    """

    def __init__(self, *, entity_registry, reactions=None, space=None,
                 identity_service=None, max_candidates: int = MAX_CANDIDATES,
                 similarity_threshold: float = SIMILARITY_THRESHOLD) -> None:
        from crea.services.identity import IdentityResolutionService

        self._entities = entity_registry
        self._reactions = reactions
        self._space = space if space is not None else LocalEntitySpace(entity_registry)
        self._identity = identity_service or IdentityResolutionService()
        self._max_candidates = max_candidates
        self._similarity_threshold = similarity_threshold

    def explore(self, target_entity_id: str, *,
                max_candidates: int | None = None) -> ExplorationResult:
        """One layer of exploration: target -> candidates, with evidence."""
        from crea.chem.normalize import normalize, NormalizationError

        target = self._entities.get(target_entity_id)
        diagnostics: list[str] = []
        try:
            target_mol = normalize(target.canonical_smiles)
        except NormalizationError as exc:
            return ExplorationResult(
                target=target, source=self._space.name,
                diagnostics=[f"target did not parse: {exc}"])
        except ValueError as exc:
            return ExplorationResult(
                target=target, source=self._space.name,
                diagnostics=[f"target has no structure: {exc}"])

        limit = max_candidates if max_candidates is not None else self._max_candidates
        seen = {target.identity_key}
        candidates: list[Candidate] = []
        skipped = 0
        unrelated = 0
        total = 0

        for smiles in self._space.candidate_smiles(target):
            try:
                mol = normalize(smiles)
            except NormalizationError:
                skipped += 1
                continue
            key = identity_key(mol.inchi)
            if key in seen:
                continue          # one identity, one candidate (§24)
            seen.add(key)
            total += 1

            candidate = self._build_candidate(target, target_mol, mol, smiles)
            if candidate is None:
                skipped += 1
                continue
            if not self._related(candidate):
                unrelated += 1
                continue
            if len(candidates) >= limit:
                continue
            candidates.append(candidate)

        candidates.sort(key=_sort_key)
        summary = _evidence_summary(candidates)
        if skipped:
            diagnostics.append(
                f"{skipped} candidate(s) could not be resolved and were dropped")
        if unrelated:
            diagnostics.append(
                f"{unrelated} candidate(s) bore no structural relationship to "
                f"the target and were dropped")
        diagnostics.append(
            f"candidates returned under max_candidates={limit}; order = "
            f"direct reaction evidence first, then similarity descending (§22)")
        return ExplorationResult(
            target=target, candidates=candidates,
            reaction_evidence=summary, source=self._space.name,
            returned_candidates=len(candidates), total_candidates=total,
            diagnostics=diagnostics)

    def _related(self, candidate: Candidate) -> bool:
        """Does the candidate bear any structural relationship to the target?"""
        return ((candidate.similarity or 0.0) >= self._similarity_threshold
                or candidate.scaffold_relation != "different"
                or candidate.fragment_related)

    # -- one candidate ------------------------------------------------------

    def _build_candidate(self, target: ChemicalEntity, target_mol, mol,
                         smiles: str) -> Candidate | None:
        from crea.chem.fingerprint import screen_similarity
        from crea.chem.scaffold import compare_scaffolds

        entity = self._resolve_entity(smiles, mol)
        if entity is None:
            return None

        similarity = screen_similarity(target_mol, mol).tanimoto_ecfp4
        relation = compare_scaffolds(target_mol, mol).relation
        label = scaffold_label(relation)
        fragment_related = _fragment_related(target_mol, mol)
        primary = _primary_relationship(label, fragment_related)

        status, reactions, evidence, provenance = self._reaction_evidence(
            entity.entity_id, target.entity_id)
        return Candidate(
            entity=entity, relationship=primary, similarity=round(similarity, 4),
            scaffold_relation=label, fragment_related=fragment_related,
            identity_status="resolved", reaction_evidence=status,
            reactions=reactions, evidence=evidence, provenance=provenance,
            diagnostics=([f"scaffold relation: {relation}"]
                         if relation != "disjoint" else []))

    def _resolve_entity(self, smiles: str, mol) -> ChemicalEntity | None:
        """Resolve a candidate through the identity pipeline (§10).

        Never `ChemicalEntity(smiles=...)`: the resolution goes through the
        identity service and the registry, so the candidate shares the one
        identity system and its dedup.
        """
        try:
            resolution = self._identity.resolve(
                StructureInput.of(kind="SMILES", value=smiles))
        except Exception:  # noqa: BLE001 - an unresolvable candidate is data
            return None
        if resolution.status is not StructureResolutionStatus.RESOLVED:
            return None
        try:
            return self._entities.register_resolution(resolution)
        except Exception:  # noqa: BLE001 - registration refusal is a skip
            return None

    def _reaction_evidence(self, candidate_id: str, target_id: str):
        """The four-level reaction answer (§13/§14), never conflated."""
        if self._reactions is None:
            return (ReactionEvidenceStatus.REACTION_SEARCH_UNAVAILABLE,
                    [], [], {"reaction_registry": "unavailable"})
        try:
            direct = self._reactions.find_between([candidate_id], [target_id])
        except Exception:  # noqa: BLE001 - an outage is unavailability, not absence
            return (ReactionEvidenceStatus.REACTION_SEARCH_UNAVAILABLE,
                    [], [], {"reaction_registry": "error"})
        if direct:
            return (ReactionEvidenceStatus.DIRECT_REACTION_EVIDENCE,
                    [_reaction_payload(r) for r in direct],
                    _evidence_of(direct),
                    {"reaction_registry": "direct", "count": len(direct)})
        related = self._reactions.find_by_component_entity(candidate_id)
        if related:
            return (ReactionEvidenceStatus.RELATED_REACTION_EVIDENCE,
                    [_reaction_payload(r) for r in related],
                    _evidence_of(related),
                    {"reaction_registry": "related", "count": len(related)})
        return (ReactionEvidenceStatus.NO_REACTION_EVIDENCE, [], [],
                {"reaction_registry": "searched", "count": 0})


def _fragment_related(target_mol, cand_mol) -> bool:
    """A shared ring-linker fragment is a fragment relationship (§7 C)."""
    from crea.chem.fragment import decompose_molecule

    target_frags = {f.fragment_smiles
                    for f in decompose_molecule(target_mol, "ring_linker")}
    cand_frags = {f.fragment_smiles
                  for f in decompose_molecule(cand_mol, "ring_linker")}
    return bool(target_frags & cand_frags)


def _reaction_payload(reaction) -> dict:
    """One reaction, traceable: identity + components + evidence (§25)."""
    return {
        "reaction_id": reaction.reaction_id,
        "status": reaction.status.value,
        "paper_id": reaction.paper_id,
        "doi": reaction.doi,
        "reactants": [c.entity_id for c in reaction.reactants if c.entity_id],
        "products": [c.entity_id for c in reaction.products if c.entity_id],
        "evidence_refs": list(reaction.evidence_refs),
        "provenance": (reaction.provenance.model_dump(mode="json")
                       if reaction.provenance else None),
    }


def _evidence_of(reactions) -> list[dict]:
    seen: set[str] = set()
    out = []
    for reaction in reactions:
        for ref in reaction.evidence_refs:
            if ref not in seen:
                seen.add(ref)
                out.append({"evidence_id": ref})
    return out


def _evidence_summary(candidates: list[Candidate]) -> dict:
    counts: dict[str, int] = {}
    for candidate in candidates:
        key = candidate.reaction_evidence.value
        counts[key] = counts.get(key, 0) + 1
    return counts


def _sort_key(candidate: Candidate) -> tuple:
    rank = {
        ReactionEvidenceStatus.DIRECT_REACTION_EVIDENCE: 0,
        ReactionEvidenceStatus.RELATED_REACTION_EVIDENCE: 1,
        ReactionEvidenceStatus.NO_REACTION_EVIDENCE: 2,
        ReactionEvidenceStatus.REACTION_SEARCH_UNAVAILABLE: 3,
    }[candidate.reaction_evidence]
    return (rank, -(candidate.similarity or 0.0))


__all__ = [
    "MAX_CANDIDATES",
    "ChemicalExplorer",
    "ChemicalSpace",
    "FixedChemicalSpace",
    "LocalEntitySpace",
    "scaffold_label",
]

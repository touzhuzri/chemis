"""Transformation search (CREA Stage 5.1).

Given a substrate SMILES (and optionally a target), find the existing
`ReactionTemplate`s whose structural pattern matches. Two modes:

    substrate only            -> every template whose substrate SMARTS matches
    substrate + target        -> plus product SMARTS against the target;
                                 a verified pair is TRANSFORMATION_MATCH, a
                                 template with no product pattern stays
                                 SUBSTRATE_MATCH (never fabricated, §20)

Deterministic and local: SMARTS matching reuses `matches_smarts` (the same
primitive `ReactionMatcher` uses). No LLM, no vector search, no network, no
literature lookup (§1/§11). Candidate evidence is read from the existing
`ReactionEvidenceService`; it orders results but never changes a match level
(§9).
"""

from __future__ import annotations

from crea.chem.functional_groups import matches_smarts
from crea.core.models.reaction_template import ReactionTemplate
from crea.core.models.transformation_search import (
    TransformationCandidate,
    TransformationMatchLevel,
    TransformationSearchResult,
)

_LEVEL_RANK = {
    TransformationMatchLevel.TRANSFORMATION_MATCH: 0,
    TransformationMatchLevel.SUBSTRATE_MATCH: 1,
}


class TransformationSearchService:
    """Search the reaction templates for a substrate (and optional target)."""

    def __init__(self, templates: list[ReactionTemplate] | None = None,
                 evidence_service=None) -> None:
        if templates is None:
            from crea.services.reaction_library import SEED_REACTION_TEMPLATES

            templates = list(SEED_REACTION_TEMPLATES)
        self._templates = list(templates)
        self._evidence = evidence_service

    def search(self, substrate_smiles: str, target_smiles: str = ""
               ) -> TransformationSearchResult:
        """Candidate templates for a substrate, optionally toward a target.

        `normalize` failures propagate (the caller maps them to a structured
        error); this service never touches the network.
        """
        from crea.chem.normalize import normalize

        substrate = normalize(substrate_smiles)
        target = normalize(target_smiles) if target_smiles.strip() else None

        diagnostics: list[str] = []
        candidates: list[TransformationCandidate] = []
        for template in self._templates:
            candidate = self._match(template, substrate, target)
            if candidate is not None:
                candidates.append(candidate)

        candidates.sort(key=lambda c: (
            _LEVEL_RANK[c.match_level], -c.evidence_count,
            c.reaction_template.template_id))
        if target is not None and candidates:
            diagnostics.append(
                "a template with no product pattern can only reach "
                "substrate_match; a full transformation_match needs both the "
                "substrate and the product pattern")
        return TransformationSearchResult(
            substrate=substrate.canonical_smiles,
            target=target.canonical_smiles if target is not None else "",
            candidates=candidates, diagnostics=diagnostics)

    def _match(self, template: ReactionTemplate, substrate, target
               ) -> TransformationCandidate | None:
        substrate_atoms = self._all_matches(substrate, template.substrate_patterns)
        if not substrate_atoms:
            return None

        product_atoms: list[list[int]] = []
        level = TransformationMatchLevel.SUBSTRATE_MATCH
        notes: list[str] = []

        if target is not None and template.product_patterns:
            product_atoms = self._all_matches(target, template.product_patterns)
            if not product_atoms:
                # the template has a product pattern and the target does not
                # fit it: this is not that transformation
                return None
            level = TransformationMatchLevel.TRANSFORMATION_MATCH
        elif target is not None and not template.product_patterns:
            notes.append(
                "template matched by substrate pattern only; no product "
                "pattern is encoded, so a full transformation match cannot be "
                "established")
        elif not template.product_patterns:
            notes.append("product pattern unavailable")

        count, summary = self._evidence_for(template.template_id)
        return TransformationCandidate(
            reaction_template=template, match_level=level,
            transformation=template.transformation,
            matched_substrate_atoms=substrate_atoms,
            matched_product_atoms=product_atoms,
            evidence_count=count, evidence_summary=summary, notes=notes)

    @staticmethod
    def _all_matches(molecule, patterns: list[str]) -> list[list[int]]:
        out: list[list[int]] = []
        for pattern in patterns:
            out.extend(list(m) for m in matches_smarts(molecule, pattern))
        return out

    def _evidence_for(self, template_id: str) -> tuple[int, dict[str, int]]:
        if self._evidence is None:
            return 0, {}
        records = self._evidence.list_evidence(template_id)
        summary: dict[str, int] = {}
        for record in records:
            key = record.source.source_type.value
            summary[key] = summary.get(key, 0) + 1
        return len(records), summary


__all__ = ["TransformationSearchService"]

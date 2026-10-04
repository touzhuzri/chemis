"""Reaction comparison (CREA Stage 5.2).

Puts two or more `ReactionTemplate`s side by side on the facts they carry.
Deterministic and local: substrate compatibility reuses `TransformationSearch`
(5.1) -- the one structural-matching entry point -- and evidence is read from
the existing `ReactionEvidenceService`. No provider, no network, no AI, and no
recommendation: the result is facts, not a verdict (§20/§27).
"""

from __future__ import annotations

from crea.core.errors import ReactionComparisonError
from crea.core.models.reaction_comparison import (
    ReactionComparisonEntry,
    ReactionComparisonResult,
    ReactionEvidenceItem,
)
from crea.core.models.reaction_evidence import EvidenceSourceType


class ReactionComparisonService:
    """Compare a set of reaction templates on their known facts."""

    def __init__(self, kb=None, search_service=None, evidence_service=None) -> None:
        self._kb = kb
        if evidence_service is None:
            from crea.services.reaction_evidence import ReactionEvidenceService

            evidence_service = ReactionEvidenceService(kb=kb)
        self._evidence = evidence_service
        if search_service is None:
            from crea.services.transformation_search import TransformationSearchService

            search_service = TransformationSearchService(
                evidence_service=evidence_service)
        self._search = search_service

    def compare(self, template_ids: list[str], *, substrate: str = "",
                target: str = "") -> ReactionComparisonResult:
        """Compare the named templates, in input order (§19).

        Empty input is a structured error; an unknown template is
        `TEMPLATE_NOT_FOUND`. Substrate compatibility is only evaluated when a
        substrate is given.
        """
        from crea.services.reaction_library import get_template

        ids = [t for t in (template_ids or []) if t]
        if not ids:
            raise ReactionComparisonError(
                "INVALID_INPUT", "at least one reaction template id is required")

        compatibility = self._compatibility_map(substrate, target)

        entries: list[ReactionComparisonEntry] = []
        for template_id in ids:
            template = get_template(template_id)
            if template is None:
                raise ReactionComparisonError(
                    "TEMPLATE_NOT_FOUND", f"no reaction template {template_id!r}")
            entries.append(self._entry(template, compatibility))

        return ReactionComparisonResult(
            substrate=substrate, target=target, entries=entries,
            diagnostics=[
                "comparison shows known reaction facts; it is not a prediction "
                "of experimental success, and yield is not modelled"])

    # -- pieces -------------------------------------------------------------

    def _compatibility_map(self, substrate: str, target: str) -> dict:
        if not substrate.strip():
            return {}
        result = self._search.search(substrate, target)
        return {c.reaction_template.template_id: c for c in result.candidates}

    def _entry(self, template, compatibility: dict) -> ReactionComparisonEntry:
        candidate = compatibility.get(template.template_id)
        if candidate is not None:
            level = candidate.match_level.value
            atoms = len(candidate.matched_substrate_atoms)
        elif compatibility:
            level = "no_match"
            atoms = 0
        else:
            level = ""
            atoms = 0

        records = self._evidence.list_evidence(template.template_id)
        summary: dict[str, int] = {}
        literature: list[ReactionEvidenceItem] = []
        for record in records:
            kind = record.source.source_type.value
            summary[kind] = summary.get(kind, 0) + 1
            if record.source.source_type is EvidenceSourceType.LITERATURE:
                literature.append(self._literature_item(record))

        selectivity = {}
        if template.selectivity is not None:
            selectivity = {
                "regioselectivity": template.selectivity.regioselectivity,
                "stereoselectivity": template.selectivity.stereoselectivity,
                "chemoselectivity": template.selectivity.chemoselectivity,
            }

        return ReactionComparisonEntry(
            template_id=template.template_id, name=template.name,
            class_family=template.class_family.value,
            reaction_class=template.reaction_class,
            transformation=template.transformation,
            substrate_compatibility=level, matched_substrate_atoms=atoms,
            reagents=[r.name for r in template.reagents if r.name],
            conditions=[c.value for c in template.conditions if c.value],
            mechanism_class=template.mechanism_class,
            selectivity=selectivity,
            limitations=[l.text for l in template.limitations if l.text],
            evidence_count=len(records), evidence_summary=summary,
            literature=literature, yield_available=False)

    def _literature_item(self, record) -> ReactionEvidenceItem:
        paper = None
        if self._kb is not None and record.source.source_id:
            paper = self._kb.store.get_paper(record.source.source_id)
        return ReactionEvidenceItem(
            evidence_type=record.evidence_type.value,
            source_type=record.source.source_type.value,
            support_scope=record.support_scope, status=record.status.value,
            paper_id=record.source.source_id,
            title=(paper.title or "") if paper is not None else record.source.title,
            year=paper.year if paper is not None else None,
            journal=(paper.journal or "") if paper is not None else "",
            doi=(paper.doi or "") if paper is not None else "")


__all__ = ["ReactionComparisonService"]

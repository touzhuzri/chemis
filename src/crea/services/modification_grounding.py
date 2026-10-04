"""Modification grounding (CREA Stage 6.2).

The AI proposes; this checks every claim against the AI Context. A reaction
template id, an evidence id, a literature id -- each must resolve in the
context, whatever the model said (§13/§15/§30). Nothing the model returns is
trusted on its word, and nothing new is created.

A candidate that resolves entirely is `GROUNDED`; one that resolves partly is
`PARTIALLY_GROUNDED`; one that resolves not at all is `UNSUPPORTED` and is not
offered as chemistry (§28).
"""

from __future__ import annotations

from crea.core.ids import content_hash
from crea.core.models.modification import (
    GroundingStatus,
    ModificationCandidate,
    StructuralChange,
)
from crea.services.modification_prompt import (
    allowed_evidence_ids,
    allowed_literature_ids,
    required_reaction_ids,
)


class ModificationGroundingService:
    """Verify AI candidates against the AI Context they were built from."""

    def ground(self, raw: dict, context,
               index: int = 0) -> tuple[ModificationCandidate | None, list[str]]:
        """One raw candidate -> a grounded candidate, or None + reasons.

        Returns `(candidate, reasons)`. `candidate` is None when nothing in the
        candidate resolves (UNSUPPORTED); otherwise the candidate carries only
        the ids that actually resolve, with its grounding status.
        """
        reasons: list[str] = []
        template_ids = [str(t) for t in (raw.get("reaction_template_ids") or [])]
        evidence_ids = [str(e) for e in (raw.get("evidence_ids") or [])]
        literature_ids = [str(l) for l in (raw.get("literature_ids") or [])]

        known_templates = required_reaction_ids(context)
        known_evidence = allowed_evidence_ids(context)
        known_literature = allowed_literature_ids(context)

        good_templates = [t for t in template_ids if t in known_templates]
        good_evidence = [e for e in evidence_ids if e in known_evidence]
        good_literature = [l for l in literature_ids if l in known_literature]

        for tid in template_ids:
            if tid not in known_templates:
                reasons.append(f"reaction template {tid!r} is not in the context")
        for eid in evidence_ids:
            if eid not in known_evidence:
                reasons.append(f"evidence {eid!r} is not in the context")
        for lid in literature_ids:
            if lid not in known_literature:
                reasons.append(f"literature {lid!r} is not in the context")

        if not good_templates:
            # no grounded reaction: not a chemistry candidate (§28)
            return None, reasons or ["no grounded reaction template in the candidate"]

        status = GroundingStatus.GROUNDED if (
            len(good_templates) == len(template_ids)
            and len(good_evidence) == len(evidence_ids)
            and len(good_literature) == len(literature_ids)
        ) else GroundingStatus.PARTIALLY_GROUNDED

        change = raw.get("structural_change") or {}
        match_status = {t: self._match_level(context, t)
                        for t in good_templates}
        summary: dict[str, int] = {}
        for evidence in context.evidence:
            if evidence.evidence_id in good_evidence:
                summary[evidence.source_type] = summary.get(evidence.source_type, 0) + 1
        if not good_evidence:
            # fall back to the reaction's own evidence summary from the context
            for reaction in context.reactions:
                if reaction.template_id in good_templates:
                    summary["total"] = summary.get("total", 0) + reaction.evidence_count
                    break

        candidate = ModificationCandidate(
            candidate_id="mcand-" + content_hash(
                f"{index}|{good_templates}|{raw.get('description', '')}", 16),
            description=str(raw.get("description") or ""),
            structural_change=StructuralChange(
                from_feature=str(change.get("from_feature") or ""),
                to_feature=str(change.get("to_feature") or ""),
                scope=str(change.get("scope") or "")),
            reaction_template_ids=good_templates,
            match_status=match_status,
            evidence_ids=good_evidence,
            literature_ids=good_literature,
            evidence_summary=summary,
            rationale=str(raw.get("rationale") or ""),
            grounding_status=status)
        return candidate, reasons

    @staticmethod
    def _match_level(context, template_id: str) -> str:
        for match in context.matches:
            if match.template_id == template_id:
                return match.match_level
        return ""


__all__ = ["ModificationGroundingService"]

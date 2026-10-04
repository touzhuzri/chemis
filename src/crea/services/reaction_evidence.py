"""Reaction evidence service (Stage 3.4).

Joins ReactionTemplates to the evidence that supports them. The evidence
itself is domain data (`core.models.reaction_evidence`); this service validates
it against a real template, stores the non-derived records, and derives the
CURATED self-evidence of a curated template.

The curated library is its own knowledge source (§9): a template with no
stored evidence still returns a CURATED_KNOWLEDGE record derived from the
template's own provenance -- never fabricated literature (§15). Derived records
are not persisted, so the library does not grow 46 duplicate rows (§26).
"""

from __future__ import annotations

from crea.core.errors import ReactionEvidenceError
from crea.core.ids import content_hash, normalize_doi
from crea.core.models.reaction_evidence import (
    EvidenceLocator,
    EvidenceSourceReference,
    EvidenceSourceType,
    ReactionEvidence,
    ReactionEvidenceType,
)
from crea.core.models.reaction_template import EvidenceStatus
from crea.core.provenance import Provenance, utc_now_iso


def curated_evidence_for(template) -> ReactionEvidence:
    """The CURATED self-evidence of a curated template (§9/§26).

    Derived, deterministic, and honest: the support scope is the template's
    own transformation, the source is the curated seed, and no DOI is invented.
    """
    return ReactionEvidence(
        evidence_id=f"evidence-curated-{template.template_id}",
        reaction_template_id=template.template_id,
        evidence_type=ReactionEvidenceType.CURATED_KNOWLEDGE,
        source=EvidenceSourceReference(
            source_type=EvidenceSourceType.CURATED,
            source_id=template.source_id or f"seed/{template.template_id}",
            title=template.name),
        support_scope=template.transformation.note or template.name,
        status=EvidenceStatus.CURATED,
        note="Curated chemistry knowledge.",
        provenance=template.provenance)


class ReactionEvidenceService:
    """List, attach and validate the evidence behind a ReactionTemplate."""

    def __init__(self, kb=None) -> None:
        self._kb = kb

    # -- validation ---------------------------------------------------------

    @staticmethod
    def _require_template(template_id: str):
        from crea.services.reaction_library import get_template

        template = get_template(template_id)
        if template is None:
            raise ReactionEvidenceError(
                "TEMPLATE_NOT_FOUND",
                f"no reaction template {template_id!r}")
        return template

    def validate_evidence(self, evidence: ReactionEvidence,
                          template) -> list[str]:
        """Structural checks; returns the problems found (empty = valid)."""
        problems: list[str] = []
        if not evidence.evidence_id:
            problems.append("evidence_id is required")
        if evidence.reaction_template_id != template.template_id:
            problems.append(
                "reaction_template_id does not match the target template")
        if not evidence.support_scope.strip():
            problems.append("support_scope is required")
        if evidence.source.source_type is EvidenceSourceType.UNKNOWN:
            problems.append("source_type must be stated")
        return problems

    # -- reads --------------------------------------------------------------

    def list_evidence(self, template_id: str) -> list[ReactionEvidence]:
        """Stored evidence for a template, plus its CURATED self-evidence.

        When no evidence is stored the curated template is its own knowledge
        source, so a derived CURATED_KNOWLEDGE record is returned instead of
        an empty list (§9). A template with genuinely no evidence and no
        curated provenance returns `[]`.
        """
        template = self._require_template(template_id)
        from crea.services.reaction_library import SEED_REACTION_TEMPLATES

        stored = (self._kb.list_reaction_evidence(template_id)
                  if self._kb is not None else [])
        if stored:
            return stored
        if template in SEED_REACTION_TEMPLATES:
            return [curated_evidence_for(template)]
        return []

    def get_evidence(self, evidence_id: str) -> ReactionEvidence | None:
        if self._kb is None:
            return None
        return self._kb.get_reaction_evidence(evidence_id)

    # -- literature linking (Stage 4.4) -------------------------------------

    def evidence_for_literature(self, paper_id: str) -> list[ReactionEvidence]:
        """The reverse query: which ReactionEvidence cite this paper (§2)."""
        if self._kb is None:
            return []
        return self._kb.find_reaction_evidence_by_source(
            EvidenceSourceType.LITERATURE.value, paper_id)

    def link_literature(self, template_id: str, *, paper_id: str = "",
                        doi: str = "", evidence_type: ReactionEvidenceType,
                        support_scope: str, locator: EvidenceLocator | None = None,
                        status: EvidenceStatus = EvidenceStatus.LITERATURE_SUPPORTED,
                        note: str = "") -> tuple[ReactionEvidence, bool]:
        """Link a locally-stored `Paper` to a ReactionTemplate as evidence.

        Local only (§7/§8): the DOI is normalized and resolved against the
        literature store; no provider is ever consulted. A DOI that is not
        stored locally is `LITERATURE_NOT_FOUND`, never an internet lookup.

        Dedup (§10) is by (template, source_type, source_id, evidence_type,
        support_scope) -- the locator is deliberately *not* part of the key, so
        Figure 2 and Scheme 3 on the same paper do not become two records.
        Returns `(record, is_new)`.
        """
        template = self._require_template(template_id)  # TEMPLATE_NOT_FOUND
        if self._kb is None:
            raise ReactionEvidenceError(
                "INVALID_EVIDENCE", "no knowledge base is configured")

        resolved_id = self._resolve_paper_id(paper_id, doi)
        if resolved_id is None:
            raise ReactionEvidenceError(
                "LITERATURE_NOT_FOUND",
                f"no local literature record for "
                f"{('DOI ' + doi) if doi else ('paper ' + paper_id)}; "
                f"add the record to the literature store first")

        # Dedup: return the existing record rather than duplicate it.
        for existing in self._kb.list_reaction_evidence(template_id):
            src = existing.source
            if (src.source_type is EvidenceSourceType.LITERATURE
                    and src.source_id == resolved_id
                    and existing.evidence_type is evidence_type
                    and existing.support_scope == support_scope):
                return existing, False

        evidence = ReactionEvidence(
            evidence_id="evidence-" + content_hash(
                f"{template_id}|literature|{resolved_id}|"
                f"{evidence_type.value}|{support_scope}", 16),
            reaction_template_id=template_id,
            evidence_type=evidence_type,
            source=EvidenceSourceReference(
                source_type=EvidenceSourceType.LITERATURE,
                source_id=resolved_id,
                # a title is a display convenience read at link time; the DOI,
                # journal and year live only on the Paper (§2)
                title=self._paper_title(resolved_id),
                locator=locator or EvidenceLocator(),
                retrieved_at=utc_now_iso()),
            support_scope=support_scope,
            status=status,
            note=note,
            # Evidence provenance is its own (§17): this records the linking
            # action, never a copy of the Paper's provenance.
            provenance=Provenance(
                tool="reaction-evidence-linker", tool_version="crea4-st4.4",
                source="evidence_linking"))
        return self._kb.save_reaction_evidence(evidence), True

    def _resolve_paper_id(self, paper_id: str, doi: str) -> str | None:
        """A local Paper id from an id or a DOI. No network (Stage 4.4 §7)."""
        if paper_id:
            paper = self._kb.store.get_paper(paper_id)
            return paper.paper_id if paper is not None else None
        if doi:
            normalized = normalize_doi(doi)
            # normalize_doi is syntactic (prefixes, case, punctuation); it does
            # not enforce the DOI *form*, so a string that is not a DOI at all
            # is caught here rather than reported as merely absent.
            if not normalized or not (
                    normalized.startswith("10.") and "/" in normalized):
                raise ReactionEvidenceError(
                    "INVALID_DOI", f"{doi!r} is not a DOI")
            paper = self._kb.find_paper_by_doi(normalized)
            return paper.paper_id if paper is not None else None
        return None

    def _paper_title(self, paper_id: str) -> str:
        paper = self._kb.store.get_paper(paper_id)
        return (paper.title or "") if paper is not None else ""

    # -- writes -------------------------------------------------------------

    def attach_evidence(self, evidence: ReactionEvidence) -> ReactionEvidence:
        """Validate against the target template and store one record."""
        template = self._require_template(evidence.reaction_template_id)
        problems = self.validate_evidence(evidence, template)
        if problems:
            raise ReactionEvidenceError(
                "INVALID_EVIDENCE", "; ".join(problems))
        if self._kb is None:
            raise ReactionEvidenceError(
                "INVALID_EVIDENCE", "no knowledge base is configured for storing evidence")
        return self._kb.save_reaction_evidence(evidence)


__all__ = ["ReactionEvidenceService", "curated_evidence_for"]

"""Reaction evidence use cases (CREA Stage 3.4).

The workbench's reaction-evidence side. Each call opens a short-lived
knowledge base and delegates to the `ReactionEvidenceService`; this layer
orchestrates and computes no chemistry of its own.
"""

from __future__ import annotations

from pathlib import Path


class ReactionEvidenceApplication:
    """List, read and attach the evidence behind a ReactionTemplate."""

    def __init__(self, db_path) -> None:
        self._db_path = Path(db_path)

    def _with(self, fn):
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.reaction_evidence import ReactionEvidenceService

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        try:
            return fn(ReactionEvidenceService(kb=kb))
        finally:
            kb.close()

    def list_evidence(self, template_id: str):
        return self._with(lambda svc: svc.list_evidence(template_id))

    def get_evidence(self, evidence_id: str):
        return self._with(lambda svc: svc.get_evidence(evidence_id))

    def attach_evidence(self, evidence):
        return self._with(lambda svc: svc.attach_evidence(evidence))

    def link_literature(self, template_id: str, **kwargs):
        """Link a local paper to a template (Stage 4.4). Local-only, no network."""
        return self._with(lambda svc: svc.link_literature(template_id, **kwargs))

    def evidence_for_literature(self, paper_id: str):
        """The reverse query: evidence citing a paper (Stage 4.4)."""
        return self._with(lambda svc: svc.evidence_for_literature(paper_id))


__all__ = ["ReactionEvidenceApplication"]

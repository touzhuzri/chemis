"""Transformation search use case (CREA Stage 5.1).

Opens a short-lived knowledge base for candidate evidence and delegates the
search to `TransformationSearchService`. Local only: no provider, no network,
no AI.
"""

from __future__ import annotations

from pathlib import Path


class TransformationSearchApplication:
    """Search the reaction templates for a substrate (and optional target)."""

    def __init__(self, db_path) -> None:
        self._db_path = Path(db_path)

    def search(self, substrate: str, target: str = ""):
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.reaction_evidence import ReactionEvidenceService
        from crea.services.transformation_search import TransformationSearchService

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        try:
            service = TransformationSearchService(
                evidence_service=ReactionEvidenceService(kb=kb))
            return service.search(substrate, target)
        finally:
            kb.close()


__all__ = ["TransformationSearchApplication"]

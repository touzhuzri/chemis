"""Reaction comparison use case (CREA Stage 5.2).

Opens a short-lived knowledge base for evidence / literature metadata and
delegates to `ReactionComparisonService`. The result is transient -- nothing
is persisted. Local only: no provider, no network, no AI.
"""

from __future__ import annotations

from pathlib import Path


class ReactionComparisonApplication:
    """Compare reaction templates on their known facts."""

    def __init__(self, db_path) -> None:
        self._db_path = Path(db_path)

    def compare(self, template_ids: list[str], *, substrate: str = "",
                target: str = ""):
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.reaction_comparison import ReactionComparisonService

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        try:
            return ReactionComparisonService(kb=kb).compare(
                template_ids, substrate=substrate, target=target)
        finally:
            kb.close()


__all__ = ["ReactionComparisonApplication"]

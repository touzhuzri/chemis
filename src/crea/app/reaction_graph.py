"""Reaction-graph use cases (CREA Stage 5.3).

Opens a short-lived knowledge base and delegates to `ReactionGraphService`.
The graph is derived and transient -- nothing is persisted. Local only: no
provider, no network, no AI.
"""

from __future__ import annotations

from pathlib import Path


class ReactionGraphApplication:
    """Build a reaction graph from a molecule, reaction or literature root."""

    def __init__(self, db_path) -> None:
        self._db_path = Path(db_path)

    def _with(self, fn):
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.reaction_graph import ReactionGraphService

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        try:
            return fn(ReactionGraphService(kb))
        finally:
            kb.close()

    def molecule_graph(self, entity_id: str, depth: int = 1):
        return self._with(lambda svc: svc.molecule_graph(entity_id, depth))

    def reaction_graph(self, template_id: str, depth: int = 1):
        return self._with(lambda svc: svc.reaction_graph(template_id, depth))

    def literature_graph(self, paper_id: str, depth: int = 1):
        return self._with(lambda svc: svc.literature_graph(paper_id, depth))


__all__ = ["ReactionGraphApplication"]

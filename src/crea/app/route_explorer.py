"""Route explorer use case (CREA Stage 5.4).

Opens a short-lived knowledge base for candidate evidence and delegates the
search to `RouteExplorerService`. Transient: routes are not persisted. Local
only: no provider, no network, no AI, no retrosynthesis.
"""

from __future__ import annotations

from pathlib import Path


class RouteExplorerApplication:
    """Explore forward routes between two molecules."""

    def __init__(self, db_path) -> None:
        self._db_path = Path(db_path)

    def explore(self, start_smiles: str, target_smiles: str,
                max_steps: int = 3):
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.route_explorer import RouteExplorerService

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        try:
            return RouteExplorerService(kb=kb).explore(
                start_smiles, target_smiles, max_steps)
        finally:
            kb.close()


__all__ = ["RouteExplorerApplication"]

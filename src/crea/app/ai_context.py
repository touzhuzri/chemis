"""AI context use case (CREA Stage 6.1).

Opens a short-lived knowledge base and builds an `AIContext` from local data.
No AI is invoked: this returns facts, never an answer. Local only -- no
provider, no network.
"""

from __future__ import annotations

from pathlib import Path


class AIContextApplication:
    """Assemble a structured AI context from the local chemistry data."""

    def __init__(self, db_path) -> None:
        self._db_path = Path(db_path)

    def build(self, request):
        from crea.services.ai_context import AIContextService
        from crea.services.knowledge_base import KnowledgeBase

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        try:
            return AIContextService(kb).build(request)
        finally:
            kb.close()


__all__ = ["AIContextApplication"]

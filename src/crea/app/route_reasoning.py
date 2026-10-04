"""Route reasoning use case (CREA Stage 6.3).

Runs the deterministic explorer, builds the AI Context, calls the injected LLM
once, and returns a grounded, transient reasoning. Offline / no key reports
`AI_PROVIDER_UNAVAILABLE`; the deterministic Route Explorer is unaffected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable


class RouteReasoningApplication:
    """Explain and compare the deterministic candidate routes."""

    def __init__(self, db_path, llm_factory: Callable | None = None) -> None:
        self._db_path = Path(db_path)
        self._llm_factory = llm_factory

    def reason(self, request):
        from crea.core.errors import RouteReasoningError
        from crea.services.ai_context import AIContextService
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.route_explorer import RouteExplorerService
        from crea.services.route_reasoning import RouteReasoningService

        if self._llm_factory is None:
            raise RouteReasoningError(
                "AI_PROVIDER_UNAVAILABLE", "no AI provider is configured")

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        try:
            service = RouteReasoningService(
                AIContextService(kb), RouteExplorerService(kb=kb),
                self._llm_factory())
            return service.reason(request)
        finally:
            kb.close()


__all__ = ["RouteReasoningApplication"]

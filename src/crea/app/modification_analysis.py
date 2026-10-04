"""Modification-analysis use case (CREA Stage 6.2).

Builds the AI Context, runs the LLM-backed analysis, and returns a grounded,
transient `ModificationAnalysis`. The LLM is injected by the composition root;
offline / no key reports `AI_PROVIDER_UNAVAILABLE` without touching the
deterministic tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable


class ModificationAnalysisApplication:
    """Interpret a requested modification into grounded CREA candidates."""

    def __init__(self, db_path, llm_factory: Callable | None = None) -> None:
        self._db_path = Path(db_path)
        self._llm_factory = llm_factory

    def analyze(self, request):
        from crea.core.errors import ModificationAnalysisError
        from crea.services.ai_context import AIContextService
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.modification_analysis import ModificationAnalysisService

        if self._llm_factory is None:
            raise ModificationAnalysisError(
                "AI_PROVIDER_UNAVAILABLE",
                "no AI provider is configured")

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        try:
            service = ModificationAnalysisService(
                AIContextService(kb), self._llm_factory())
            return service.analyze(request)
        finally:
            kb.close()


__all__ = ["ModificationAnalysisApplication"]

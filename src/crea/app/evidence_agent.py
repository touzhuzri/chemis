"""Evidence-aware agent use case (CREA Stage 6.4).

Assembles the read-only tool registry over the deterministic services and runs
the bounded agent. The LLM is injected by the composition root; offline / no
key reports `AGENT_PROVIDER_UNAVAILABLE` without touching the deterministic
tools. The run is transient -- nothing is persisted (§十).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable


class EvidenceAgentApplication:
    """Run the bounded, read-only, evidence-aware agent."""

    def __init__(self, db_path, llm_factory: Callable | None = None,
                 literature_factory: Callable | None = None) -> None:
        self._db_path = Path(db_path)
        self._llm_factory = llm_factory
        self._literature_factory = literature_factory

    def run(self, question: str, *, molecule_smiles: str = "",
            molecule_id: str = ""):
        from crea.agent.evidence_agent import EvidenceAgent
        from crea.agent.evidence_tools import build_evidence_tool_registry
        from crea.core.errors import ProviderUnavailable
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.reaction_comparison import ReactionComparisonService
        from crea.services.reaction_evidence import ReactionEvidenceService
        from crea.services.route_explorer import RouteExplorerService
        from crea.services.transformation_search import TransformationSearchService

        if self._llm_factory is None:
            from crea.core.errors import ProviderUnavailable as _PU

            raise _PU("no AI provider is configured")

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        try:
            evidence = ReactionEvidenceService(kb=kb)
            # the molecule the question is about, as a graph/search root
            context = self._molecule_context(molecule_smiles, molecule_id)
            registry = build_evidence_tool_registry(
                transformation_search=TransformationSearchService(
                    evidence_service=evidence),
                comparison=ReactionComparisonService(kb=kb),
                evidence=evidence,
                route=RouteExplorerService(kb=kb),
                literature=self._literature())
            agent = EvidenceAgent(registry, self._llm_factory())
            return agent.run(question, context=context)
        finally:
            kb.close()

    def _literature(self):
        if self._literature_factory is None:
            return None
        from crea.services.literature import LiteratureService

        providers = self._literature_factory() or {}
        if not providers:
            return None
        return LiteratureService(providers)

    @staticmethod
    def _molecule_context(smiles: str, entity_id: str) -> str:
        if smiles:
            return f"the question is about the molecule with SMILES {smiles}"
        if entity_id:
            return f"the question is about molecule entity id {entity_id}"
        return ""


__all__ = ["EvidenceAgentApplication"]

"""Bounded research-workflow use cases (CREA Stage 7.2).

Assembles the read-only/deterministic task executor and the workflow service,
and exposes create/read/run operations. The LLM and literature factories are
injected by the composition root; without them the AI and literature tasks fail
honestly while every deterministic task still runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable


class TaskWorkflowApplication:
    """Create, read and run bounded research workflows over a session."""

    def __init__(self, db_path, *, llm_factory: Callable | None = None,
                 literature_factory: Callable | None = None) -> None:
        self._db_path = Path(db_path)
        self._llm_factory = llm_factory
        self._literature_factory = literature_factory

    def _with(self, fn):
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.research_task_executor import ResearchTaskExecutor
        from crea.services.task_workflow import TaskWorkflowService

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        try:
            executor = ResearchTaskExecutor(
                kb,
                literature=self._literature(),
                modification_factory=self._ai(self._modification),
                route_reasoning_factory=self._ai(self._route_reasoning),
                agent_factory=self._ai(self._agent))
            return fn(TaskWorkflowService(kb, executor))
        finally:
            kb.close()

    # -- AI/literature factories -------------------------------------------

    def _ai(self, builder):
        if self._llm_factory is None:
            return None
        return lambda: builder()

    def _modification(self):
        from crea.services.ai_context import AIContextService
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.modification_analysis import ModificationAnalysisService

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        return ModificationAnalysisService(AIContextService(kb), self._llm_factory())

    def _route_reasoning(self):
        from crea.services.ai_context import AIContextService
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.route_explorer import RouteExplorerService
        from crea.services.route_reasoning import RouteReasoningService

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        return RouteReasoningService(AIContextService(kb),
                                     RouteExplorerService(kb=kb),
                                     self._llm_factory())

    def _agent(self):
        from crea.agent.evidence_agent import EvidenceAgent
        from crea.agent.evidence_tools import build_evidence_tool_registry
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.reaction_comparison import ReactionComparisonService
        from crea.services.reaction_evidence import ReactionEvidenceService
        from crea.services.route_explorer import RouteExplorerService
        from crea.services.transformation_search import TransformationSearchService

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        evidence = ReactionEvidenceService(kb=kb)
        registry = build_evidence_tool_registry(
            transformation_search=TransformationSearchService(evidence_service=evidence),
            comparison=ReactionComparisonService(kb=kb),
            evidence=evidence, route=RouteExplorerService(kb=kb))
        return EvidenceAgent(registry, self._llm_factory())

    def _literature(self):
        if self._literature_factory is None:
            return None
        from crea.services.literature import LiteratureService

        providers = self._literature_factory() or {}
        return LiteratureService(providers) if providers else None

    # -- operations ---------------------------------------------------------

    def create(self, study_id: str, **kwargs):
        return self._with(lambda svc: svc.create(study_id, **kwargs))

    def list(self, study_id: str):
        return self._with(lambda svc: svc.list(study_id))

    def detail(self, workflow_id: str):
        return self._with(lambda svc: svc.detail(workflow_id))

    def get(self, workflow_id: str):
        return self._with(lambda svc: svc.get(workflow_id))

    def ready_tasks(self, workflow_id: str):
        return self._with(lambda svc: svc.ready_tasks(workflow_id))

    def start(self, workflow_id: str):
        return self._with(lambda svc: svc.start(workflow_id))

    def pause(self, workflow_id: str):
        return self._with(lambda svc: svc.pause(workflow_id))

    def resume(self, workflow_id: str):
        return self._with(lambda svc: svc.resume(workflow_id))

    def cancel(self, workflow_id: str):
        return self._with(lambda svc: svc.cancel(workflow_id))

    def execute_task(self, workflow_id: str, task_id: str):
        return self._with(lambda svc: svc.execute_task(workflow_id, task_id))

    def skip_task(self, workflow_id: str, task_id: str, reason: str = ""):
        return self._with(lambda svc: svc.skip_task(workflow_id, task_id, reason))


__all__ = ["TaskWorkflowApplication"]

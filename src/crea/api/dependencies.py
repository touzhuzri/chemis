"""Workbench wiring: the one place the API's objects are assembled.

Phase 4. Everything below this module is Phase 3's engine; this module
chooses which composer builds the research stack (offline fixtures or
live providers), where the knowledge base lives, and hands the
application layer its use cases.

The stack factory contract: `factory(document_ids)` returns exactly what
`ResearchWorkflow` needs -- (workflow, library, evidence sink, tool
registry) -- with the named documents ingested. The job system never
assembles chemistry or providers itself, and the HTTP layer never sees
one (§39-§41).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from crea.app.ai_context import AIContextApplication
from crea.app.events import EventLog
from crea.app.final_audit import FinalAuditApplication
from crea.app.evidence_agent import EvidenceAgentApplication
from crea.app.jobs import JobManager
from crea.app.knowledge import KnowledgeApplication
from crea.app.literature import LiteratureApplication
from crea.app.modification_analysis import ModificationAnalysisApplication
from crea.app.molecule_library import MoleculeLibraryApplication
from crea.app.pubchem import PubChemApplication
from crea.app.reaction_comparison import ReactionComparisonApplication
from crea.app.reaction_evidence import ReactionEvidenceApplication
from crea.app.reaction_graph import ReactionGraphApplication
from crea.app.research import ResearchApplication
from crea.app.research_intelligence import ResearchIntelligenceApplication
from crea.app.research_report import ResearchReportApplication
from crea.app.research_study import ResearchStudyApplication
from crea.app.task_workflow import TaskWorkflowApplication
from crea.app.route_explorer import RouteExplorerApplication
from crea.app.route_reasoning import RouteReasoningApplication
from crea.app.transformation_search import TransformationSearchApplication
from crea.app.workspace import WorkspaceApplication


def default_db_path() -> Path:
    return Path("data") / "crea.db"


def document_path(documents_dir: Path, document_id: str) -> Path:
    """The stored file of one uploaded document (§58/§59).

    The id is a content hash and the file is `<id>.pdf` -- a user-supplied
    name never becomes a path, and no request can point outside this
    directory.
    """
    registry_path = Path(documents_dir) / "documents.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        registry = {}
    if document_id not in registry:
        raise KeyError(f"no document {document_id!r}")
    return Path(documents_dir) / f"{document_id}.pdf"


def make_stack_factory(*, db_path: Path, documents_dir: Path,
                       offline: bool = False):
    """The default research-stack composer.

    `offline=True` wires the fixture providers (synthetic literature,
    explicitly labelled as such). Otherwise the real providers run:
    OpenAlex/Crossref need no credential, PubChem resolves compound
    names, and the extraction model is attached only when an LLM key is
    configured -- without one the run honestly reports discovery-only
    instead of inventing reactions (§121.L).

    The live composer lives in `crea.agent.live` -- the composition root
    (§84 of Phase 3 / §2): the API layer orchestrates, and only the
    composition root may name the real transport.
    """

    def factory(document_ids: list[str]):
        from crea.services.knowledge_base import KnowledgeBase

        knowledge_base = KnowledgeBase(db_path)
        knowledge_base.initialize()
        if offline:
            from crea.agent.offline import build_offline_workflow_stack

            workflow, library, sink, registry = build_offline_workflow_stack(
                knowledge_base=knowledge_base)
        else:
            from crea.agent.live import build_live_workflow_stack

            workflow, library, sink, registry = build_live_workflow_stack(
                knowledge_base=knowledge_base)
        for document_id in document_ids:
            library.ingest(document_path(documents_dir, document_id))
        return workflow, library, sink, registry

    return factory


def make_intelligence_stack_factory(*, db_path: Path, offline: bool = False):
    """The M2.0 AI-research stack composer.

    Same discipline as `make_stack_factory`: the job is to choose which
    composition root builds the enforcing tool registry -- offline
    fixtures or live providers -- and to ingest whatever documents the
    caller actually supplied. No chemistry and no transport is assembled
    here.
    """

    def factory(document_ids: list[str] | None = None):
        from crea.services.knowledge_base import KnowledgeBase

        knowledge_base = KnowledgeBase(db_path)
        knowledge_base.initialize()
        if offline:
            from crea.agent.offline import build_offline_intelligence_stack

            registry, library, sink = build_offline_intelligence_stack(
                knowledge_base=knowledge_base)
        else:
            from crea.agent.live import build_live_intelligence_stack

            registry, library, sink = build_live_intelligence_stack(
                knowledge_base=knowledge_base)
        return registry, library, sink

    return factory


@dataclass
class WorkbenchContext:
    """Everything the HTTP layer needs, assembled once at startup."""

    db_path: Path
    documents_dir: Path
    events: EventLog
    jobs: JobManager
    research: ResearchApplication
    knowledge: KnowledgeApplication
    workspace: WorkspaceApplication
    molecule_library: MoleculeLibraryApplication
    pubchem: PubChemApplication
    reaction_evidence: ReactionEvidenceApplication
    literature: LiteratureApplication
    transformation_search: TransformationSearchApplication
    reaction_comparison: ReactionComparisonApplication
    reaction_graph: ReactionGraphApplication
    route_explorer: RouteExplorerApplication
    ai_context: AIContextApplication
    modification_analysis: ModificationAnalysisApplication
    route_reasoning: RouteReasoningApplication
    evidence_agent: EvidenceAgentApplication
    research_study: ResearchStudyApplication
    task_workflow: TaskWorkflowApplication
    research_report: ResearchReportApplication
    final_audit: FinalAuditApplication
    #: M2.0 -- the AI Research Intelligence entry. Its LLM and tool-registry
    #: factories are built here, in the composition root, never in the app
    #: or the HTTP layer.
    research_intelligence: ResearchIntelligenceApplication | None = None
    offline: bool = False

    def close(self) -> None:
        pass


def _literature_provider_factory(offline: bool):
    """A provider factory for `LiteratureApplication` (Stage 4.2).

    Offline yields no providers (every call reports PROVIDERS_UNAVAILABLE_OFFLINE);
    otherwise the live Crossref + OpenAlex providers, built lazily from the
    composition root that is allowed to name the transport.
    """
    if offline:
        return lambda: {}

    def factory():
        from crea.agent.live import build_literature_providers

        return build_literature_providers()

    return factory


def _ai_llm_factory(offline: bool):
    """A factory for the LLM behind modification analysis (Stage 6.2).

    Offline yields None (AI_PROVIDER_UNAVAILABLE); otherwise the real
    OpenAI-compatible provider is built lazily from the environment. The
    deterministic chemistry tools never depend on this.
    """
    if offline:
        return None

    def factory():
        from crea.llm import LLMConfig, OpenAICompatibleLLMProvider

        return OpenAICompatibleLLMProvider(LLMConfig.from_env())

    return factory


def _unpaywall_provider_factory(offline: bool):
    """A factory for the Unpaywall access provider (Stage 4.3).

    Offline yields None (no network, permission stays UNKNOWN); otherwise the
    live provider, built from the composition root with the operator's email.
    """
    if offline:
        return None

    def factory():
        from crea.agent.live import build_unpaywall_provider

        return build_unpaywall_provider()

    return factory


def _pubchem_provider_factory(offline: bool):
    """A provider factory for `PubChemApplication` (Stage 2.3).

    Offline yields None (UNAVAILABLE, §22); otherwise the live provider is
    built lazily from the composition root, which is the one place allowed to
    name the real transport.
    """
    if offline:
        return lambda: None

    def factory():
        from crea.agent.live import build_pubchem_exact_provider

        return build_pubchem_exact_provider()

    return factory


def _workspace_agent_factory(offline: bool = False):
    """A `WorkspaceApplication` agent factory: a real LLM over the real tools.

    The LLM is built lazily per message (an OpenAI-compatible provider needs a
    credential and, once built, carries per-turn tool-call state), and only
    here -- the composition root is the one place that names a real vendor.
    Without `CREA_LLM_API_KEY` the factory raises, and `send_message` turns
    that into an honest assistant message rather than a fabricated answer.

    `offline=True` never builds a provider at all: `--offline` means the whole
    process makes no network call, so the factory raises and the message comes
    back as an honest unavailable -- never a live model asked behind the flag.
    """

    def factory(kb):
        if offline:
            from crea.core.errors import ProviderUnavailable

            raise ProviderUnavailable(
                "no AI provider is configured (offline mode)")
        from crea.agent.workspace_stack import build_workspace_agent
        from crea.llm import LLMConfig, OpenAICompatibleLLMProvider

        llm = OpenAICompatibleLLMProvider(LLMConfig.from_env())
        return build_workspace_agent(llm, knowledge_base=kb)

    return factory


def create_context(*, db_path=None, documents_dir=None, offline: bool = False,
                   stack_factory=None) -> WorkbenchContext:
    db = Path(db_path) if db_path is not None else default_db_path()
    docs = (Path(documents_dir) if documents_dir is not None
            else Path("data") / "documents")
    docs.mkdir(parents=True, exist_ok=True)
    db.parent.mkdir(parents=True, exist_ok=True)
    events = EventLog(db.parent / "research-events.jsonl")
    knowledge = KnowledgeApplication(db)
    factory = stack_factory or make_stack_factory(
        db_path=db, documents_dir=docs, offline=offline)
    jobs = JobManager(registry_path=db.parent / "research-jobs.json",
                      events=events, stack_factory=factory, db_path=db)
    research = ResearchApplication(jobs=jobs, events=events,
                                   documents_dir=docs, knowledge_app=knowledge)
    workspace = WorkspaceApplication(db, _workspace_agent_factory(offline))
    molecule_library = MoleculeLibraryApplication(db)
    pubchem = PubChemApplication(db, _pubchem_provider_factory(offline))
    reaction_evidence = ReactionEvidenceApplication(db)
    transformation_search = TransformationSearchApplication(db)
    reaction_comparison = ReactionComparisonApplication(db)
    reaction_graph = ReactionGraphApplication(db)
    route_explorer = RouteExplorerApplication(db)
    ai_context = AIContextApplication(db)
    modification_analysis = ModificationAnalysisApplication(
        db, _ai_llm_factory(offline))
    route_reasoning = RouteReasoningApplication(db, _ai_llm_factory(offline))
    evidence_agent = EvidenceAgentApplication(
        db, _ai_llm_factory(offline), _literature_provider_factory(offline))
    from crea.services.literature_retry import RetryPolicy

    literature = LiteratureApplication(
        db, _literature_provider_factory(offline),
        access_provider_factory=_unpaywall_provider_factory(offline),
        retry_policy=RetryPolicy(max_retries=2))
    intelligence = ResearchIntelligenceApplication(
        db_path=db,
        # Offline means this process makes no network call: no provider is
        # built at all, and a run reports MODEL_UNAVAILABLE rather than
        # quietly reaching out behind the flag.
        llm_factory=(None if offline else _ai_llm_factory(False)),
        stack_factory=make_intelligence_stack_factory(db_path=db,
                                                      offline=offline))
    return WorkbenchContext(db_path=db, documents_dir=docs, events=events,
                            jobs=jobs, research=research,
                            knowledge=knowledge, workspace=workspace,
                            molecule_library=molecule_library,
                            pubchem=pubchem,
                            reaction_evidence=reaction_evidence,
                            literature=literature,
                            transformation_search=transformation_search,
                            reaction_comparison=reaction_comparison,
                            reaction_graph=reaction_graph,
                            route_explorer=route_explorer,
                            ai_context=ai_context,
                            modification_analysis=modification_analysis,
                            route_reasoning=route_reasoning,
                            evidence_agent=evidence_agent,
                            research_study=ResearchStudyApplication(db),
                            task_workflow=TaskWorkflowApplication(
                                db, llm_factory=_ai_llm_factory(offline),
                                literature_factory=_literature_provider_factory(offline)),
                            research_report=ResearchReportApplication(db),
                            final_audit=FinalAuditApplication(db),
                            research_intelligence=intelligence,
                            offline=offline)


__all__ = ["WorkbenchContext", "create_context", "default_db_path",
           "document_path", "make_stack_factory"]

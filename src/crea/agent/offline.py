"""An entirely offline agent stack.

Everything the agent needs, wired from components that already exist, with no
key and no network:

* the three real providers, over a `FixtureTransport`
* the real `LiteratureService`, over those providers
* a real `LocalDocumentService` over a real PDF parser, fed a synthetic PDF
* a scripted LLM

The fixture payloads here are **synthetic demo data** written for this file.
They are not the test suite's fixtures and they are not recorded API responses.

The scripted planner is a lookup keyed on the compound name, not a model. It
handles the one demo question; for anything else it says so rather than
improvising, which is the behaviour the real provider is meant to replace.
"""

from __future__ import annotations

import pathlib
import tempfile

from crea.agent.loop import ResearchAgent
from crea.agent.workspace import DocumentLibrary
from crea.core.models.document import DocumentMediaType
from crea.core.models.research import ActionKind, AgentAction
from crea.llm.fake import FakeLLMProvider
from crea.services.documents import LocalDocumentService
from crea.services.literature import LiteratureService
from crea.sources.base import Response, Transport
from crea.sources.crossref import CrossrefProvider, WORKS_ENDPOINT as CR
from crea.sources.openalex import OpenAlexProvider, WORKS_ENDPOINT as OA
from crea.sources.pdf import PypdfDocumentParser
from crea.sources.unpaywall import LOOKUP_ENDPOINT, UnpaywallProvider

#: Compound the demo knows about. The name maps to a structure; nothing here
#: infers one.
DEMO_COMPOUNDS = {
    "1,4-diphenyl-1,3-diyne": "c1ccccc1C#CC#Cc2ccccc2",
    "1,4-二苯基-1,3-丁二炔": "c1ccccc1C#CC#Cc2ccccc2",
    "diphenylbutadiyne": "c1ccccc1C#CC#Cc2ccccc2",
}

DEMO_DOI = "10.0000/synthetic.2026.0001"
DEMO_PDF_PAGES = [[
    "1,4-Diphenyl-1,3-diyne: a model system",
    "Abstract",
    "We report a model procedure for a symmetric diyne.",
    "Experimental",
    "A solution of the substrate was treated with reagent A.",
    "The reaction was stirred at 80 C for 12 hours.",
    "Yield 82%.",
]]


class FixtureTransport(Transport):
    """Serves canned responses. Cannot open a socket, by construction."""

    def __init__(self, routes: dict) -> None:
        self._routes = routes
        self.calls: list[str] = []

    async def request(self, method, url, *, params=None, json=None) -> Response:
        self.calls.append(url)
        for fragment, payload in self._routes.items():
            if fragment in url:
                return Response(200, payload, url=url)
        return Response(404, {"status": "not in the offline fixture set"}, url=url)


def _openalex_payload() -> dict:
    return {
        "meta": {"count": 1, "page": 1, "per_page": 10},
        "results": [{
            "id": "https://openalex.org/W0000000001",
            "doi": f"https://doi.org/{DEMO_DOI}",
            "display_name": "Synthesis of 1,4-diphenyl-1,3-diyne (synthetic record)",
            "title": "Synthesis of 1,4-diphenyl-1,3-diyne (synthetic record)",
            "publication_year": 2021,
            "ids": {"openalex": "https://openalex.org/W0000000001"},
            "authorships": [{"author": {"display_name": "A Synthetic"}}],
            "abstract_inverted_index": None,
            "open_access": {"is_oa": False, "oa_status": "closed", "oa_url": None},
            "best_oa_location": None,
        }],
    }


def _crossref_payload() -> dict:
    return {
        "status": "ok", "message-type": "work-list",
        "message": {
            "items": [{
                "DOI": DEMO_DOI,
                "title": ["Synthesis of 1,4-diphenylbuta-1,3-diyne (synthetic record)"],
                "container-title": ["Journal of Synthetic Chemistry"],
                "author": [{"given": "A", "family": "Synthetic"}],
                "issued": {"date-parts": [[2021, 4, 17]]},
                "publisher": "Example Synthetic Press",
                "type": "journal-article",
            }],
            "total-results": 1,
        },
    }


def build_offline_stack(question: str = "", *, knowledge_base=None
                        ) -> tuple[ResearchAgent, DocumentLibrary, str]:
    """A ready-to-run agent, a document library, and the ingested document id.

    `knowledge_base` (Phase 3.11), when given, is wired into the tool
    registry so the agent can query accumulated knowledge (§60/§61) and so
    its reaction tools write to the persistent registries.
    """
    from crea.agent.demo_pdf import make_demo_pdf

    transport = FixtureTransport({
        OA: _openalex_payload(),
        CR: _crossref_payload(),
    })
    literature = LiteratureService(
        providers={
            "openalex": OpenAlexProvider(transport=transport, endpoint=OA),
            "crossref": CrossrefProvider(transport=transport, endpoint=CR),
        }
    )

    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="crea-offline-"))
    pdf_path = workdir / "demo.pdf"
    pdf_path.write_bytes(make_demo_pdf(DEMO_PDF_PAGES))
    parsed = library.ingest(pdf_path)
    document_id = parsed.document.document_id

    from crea.agent.toolsets import build_registry

    from crea.agent.workspace import RouteWorkbench
    from crea.providers.fake_retrosynthesis import FakeRetrosynthesisProvider
    from crea.services.route_validation import RouteValidationService

    evidence_sink: list = []
    routes = RouteWorkbench(provider=FakeRetrosynthesisProvider(),
                            validator=RouteValidationService())
    entities = (knowledge_base.entity_registry()
                if knowledge_base is not None else None)
    reactions = (knowledge_base.reaction_registry()
                 if knowledge_base is not None else None)
    registry = build_registry(literature, library, evidence_sink, routes=routes,
                              entities=entities, reactions=reactions,
                              knowledge_base=knowledge_base)

    llm = FakeLLMProvider(plan(question, document_id))
    agent = ResearchAgent(llm=llm, registry=registry, documents=library,
                          evidence=evidence_sink)
    return agent, library, document_id


def build_offline_workflow_stack(*, extractor=None, compound_provider=None,
                             entities=None, reactions=None,
                             knowledge_base=None):
    """A ResearchWorkflow over the offline stack, with no agent in between.

    Same fixture literature, same fake retrosynthesis, same real tools -- only
    the driver differs: `ResearchWorkflow` walks the seven steps itself, so a
    caller can run one complete research task without a model. No document is
    ingested here: the caller registers whatever the user actually supplied,
    because a report must say "No user document provided" when that is the
    truth.

    `knowledge_base` (Phase 3.11) makes the run remember: the entity and
    reaction registries are built over the persistent store, one registry
    shared by the identity tool, the reaction tools and the workflow (or
    knowledge lands in a registry nobody reads), and the run's result is
    committed when it finishes. Given none, everything stays in memory and
    the run is byte-for-byte what it was before (§36).

    Returns `(workflow, library, evidence_sink, registry)`; the registry is
    handed back so a caller can read session state a tool left behind (the
    identity resolution, say) without reaching through the workflow.
    """
    from crea.agent.toolsets import build_registry
    from crea.agent.workspace import IdentityState, RouteWorkbench
    from crea.providers.fake_retrosynthesis import FakeRetrosynthesisProvider
    from crea.services.identity import IdentityResolutionService
    from crea.services.research_workflow import ResearchWorkflow
    from crea.services.route_validation import RouteValidationService

    if knowledge_base is not None:
        # The persistent registries ARE the reuse mechanism (§41): a second
        # run finds CHEM-00000001 by identity key instead of minting a new
        # id, and a re-read PDF merges into its existing reaction.
        if entities is None:
            entities = knowledge_base.entity_registry()
        if reactions is None:
            reactions = knowledge_base.reaction_registry()

    transport = FixtureTransport({
        OA: _openalex_payload(),
        CR: _crossref_payload(),
    })
    literature = LiteratureService(
        providers={
            "openalex": OpenAlexProvider(transport=transport, endpoint=OA),
            "crossref": CrossrefProvider(transport=transport, endpoint=CR),
        }
    )
    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    evidence_sink: list = []
    routes = RouteWorkbench(provider=FakeRetrosynthesisProvider(),
                            validator=RouteValidationService())
    # An explicit IdentityState: the identity tool holds its resolution on the
    # state it was built with, so the registry must own one or the answer is
    # written nowhere a caller can read it.
    identity = IdentityState(service=IdentityResolutionService(
        compound_provider=compound_provider))
    # One registry, wired everywhere: the tools' registration of an
    # established identity and the workflow's knowledge layer must write to
    # the same place, or a run's knowledge splits in two.
    semantic_resolver = _semantic_resolver(
        identity_service=getattr(identity, "service", None),
        compound_provider=compound_provider,
        entity_registry=entities)
    registry = build_registry(literature, library, evidence_sink,
                              extractor=extractor, routes=routes,
                              identity=identity, entities=entities,
                              reactions=reactions, knowledge_base=knowledge_base,
                              semantic_resolver=semantic_resolver)
    workflow = ResearchWorkflow(registry, evidence_sink=evidence_sink,
                                documents=library, entities=entities,
                                reactions=reactions,
                                knowledge_base=knowledge_base)
    return workflow, library, evidence_sink, registry


def build_offline_intelligence_stack(*, knowledge_base=None, policy=None):
    """The *enforcing* tool registry over the offline fixtures (M2.0).

    Same fixture literature, same fake retrosynthesis, same tools as the
    workflow stack -- what differs is the driver and the registry. The
    driver is the AI Research Intelligence, and it needs the registry that
    enforces the M1.1 execution policy (`build_agent_tools`), because a
    model-driven loop may not be allowed to run a persistent write without
    approval or reach the network behind the offline flag.

    `policy` defaults to the restrictive one *about effects*: model-calling
    tools are refused and a persistent write needs a human approval. It
    does NOT set `allow_network=False`, and saying why matters: the tools
    declare `access=("network",)` because their *live* backends reach the
    network, while this stack wires them to `FixtureTransport`, which
    cannot open a socket by construction. The offline guarantee here is
    the transport, not the policy flag -- and a caller that wants the
    stricter flag can pass `policy=` and watch `search_literature` come
    back as `NEEDS_INPUT` with `NETWORK_NOT_ALLOWED`, which is an honest
    refusal rather than a silent downgrade.

    Returns `(registry, library, evidence_sink)` -- the caller builds the
    intelligence, because an offline stack has no model to decide with.
    """
    from crea.agent.tool_surface import build_agent_tools
    from crea.agent.tools import ExecutionPolicy
    from crea.agent.workspace import IdentityState, RouteWorkbench
    from crea.providers.fake_retrosynthesis import FakeRetrosynthesisProvider
    from crea.services.identity import IdentityResolutionService
    from crea.services.route_validation import RouteValidationService

    if knowledge_base is not None:
        entities = knowledge_base.entity_registry()
        reactions = knowledge_base.reaction_registry()
    else:
        entities = None
        reactions = None

    transport = FixtureTransport({OA: _openalex_payload(), CR: _crossref_payload()})
    literature = LiteratureService(providers={
        "openalex": OpenAlexProvider(transport=transport, endpoint=OA),
        "crossref": CrossrefProvider(transport=transport, endpoint=CR),
    })
    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    evidence_sink: list = []
    routes = RouteWorkbench(provider=FakeRetrosynthesisProvider(),
                            validator=RouteValidationService())
    identity = IdentityState(service=IdentityResolutionService())
    semantic_resolver = _semantic_resolver(
        identity_service=getattr(identity, "service", None),
        compound_provider=None, entity_registry=entities)
    registry = build_agent_tools(
        literature, library, evidence_sink, routes=routes, identity=identity,
        entities=entities, reactions=reactions,
        knowledge_base=knowledge_base, semantic_resolver=semantic_resolver,
        policy=policy or ExecutionPolicy(allow_network=True,
                                         allow_model_calls=False))
    return registry, library, evidence_sink


def plan(question: str, document_id: str) -> list[AgentAction]:
    """The demo's script.

    A lookup, not a planner. If the question names a compound we know, this
    runs; otherwise it asks the user, which is what a real provider would
    replace with an actual model.
    """
    smiles = None
    for name, structure in DEMO_COMPOUNDS.items():
        if name.lower() in question.lower():
            smiles = structure
            break
    if smiles is None:
        return [AgentAction(
            action=ActionKind.CLARIFY,
            question=("离线演示只认识这几个化合物名："
                      + "、".join(DEMO_COMPOUNDS)
                      + "。请给出其中之一，或配置一个真实的 LLM provider。"),
        )]

    return [
        AgentAction(action=ActionKind.TOOL_CALL, tool="analyze_molecule",
                    arguments={"smiles": smiles}),
        AgentAction(action=ActionKind.TOOL_CALL, tool="search_literature",
                    arguments={"query": "1,4-diphenyl-1,3-diyne synthesis",
                               "limit": 5}),
        AgentAction(action=ActionKind.TOOL_CALL, tool="search_local_document",
                    arguments={"document_id": document_id, "query": "Yield"}),
        AgentAction(action=ActionKind.TOOL_CALL, tool="get_document_evidence",
                    arguments={"document_id": document_id, "query": "Yield 82%",
                               "max_evidence": 3}),
        AgentAction(action=ActionKind.FINAL,
                    summary="解析结构、检索文献、检索本地 PDF，"
                            "并以可追溯证据给出结论。",
                    claim_specs=[{
                        "text": "本地文档记录的实验部分报告该反应搅拌温度 80 C、"
                                "时间 12 小时、收率 82%。",
                        "claim_type": "experimental_fact",
                        "from_tool": "get_document_evidence",
                    }]),
    ]


def _semantic_resolver(*, identity_service, compound_provider,
                       entity_registry=None):
    """The semantic spine over an offline stack (CREA 4.0 Stage B-1).

    Same shape as the live one; the name chain is whatever compound
    provider the caller injected (a captured fake in tests). No network
    appears here by construction.
    """
    from crea.providers.name_lookup import CompoundNameLookup
    from crea.semantic.naming import NameResolver
    from crea.semantic.resolver import SemanticResolver

    providers = []
    if compound_provider is not None:
        providers.append(CompoundNameLookup(compound_provider))
    return SemanticResolver(
        identity_service=identity_service,
        name_resolver=NameResolver(providers),
        entity_registry=entity_registry)

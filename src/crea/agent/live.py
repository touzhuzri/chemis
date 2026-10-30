"""A stack that talks to the internet, when there is a credential to do it with.

Assembled from the same components as the offline stack -- the same
`LiteratureService`, the same tools, the same agent -- with a real transport and
a real model behind them. Nothing here reimplements anything; it only decides
which ends are real.

Every missing prerequisite is a clear error. There is no path here that quietly
downgrades to the offline stack, because a report that looks researched and
was not is the one outcome this project exists to prevent.
"""

from __future__ import annotations

import os

from crea.agent.loop import ResearchAgent
from crea.agent.toolsets import build_registry
from crea.agent.workspace import DocumentLibrary
from crea.core.errors import ProviderUnavailable
from crea.services.documents import LocalDocumentService
from crea.services.literature import LiteratureService
from crea.sources.crossref import CrossrefProvider, WORKS_ENDPOINT as CR
from crea.sources.http import HttpxTransport
from crea.sources.openalex import OpenAlexProvider, WORKS_ENDPOINT as OA
from crea.sources.pdf import PypdfDocumentParser
from crea.sources.unpaywall import LOOKUP_ENDPOINT, UnpaywallProvider

ENV_UNPAYWALL_EMAIL = "CREA_UNPAYWALL_EMAIL"


def build_live_stack(llm, documents: list | None = None, *, knowledge_base=None):
    """Real providers behind a real model.

    OpenAlex and Crossref need no credential. Unpaywall does, and is attached
    only when one is configured -- its absence downgrades the *access*
    enrichment and nothing else.

    `knowledge_base` (Phase 3.11) wires the persistent knowledge query
    tools and persistent registries into the tool registry, exactly as the
    offline stack does. Local persistence is not a provider: the live stack
    talks to the network for literature and to the knowledge base for what
    is already established, and never confuses the two (§85).
    """
    # A presence probe, not a use: this module must never hold a client
    # library itself. `HttpxTransport` is where one is created.
    import importlib.util

    if importlib.util.find_spec("httpx") is None:
        raise ProviderUnavailable(
            "httpx is required for a live run. Install it with: "
            "pip install -e '.[live]'. The offline stack needs nothing."
        )

    # The operator's credentials may be in `.env` rather than in the shell.
    # Read it first: everything below looks for them in the environment.
    from crea.core.env import load_local_env

    load_local_env()

    transport = HttpxTransport()

    providers = {
        "openalex": OpenAlexProvider(transport=transport, endpoint=OA),
        "crossref": CrossrefProvider(transport=transport, endpoint=CR),
    }
    access = None
    email = os.environ.get(ENV_UNPAYWALL_EMAIL, "").strip()
    if email:
        access = UnpaywallProvider(transport=transport, email=email,
                                   endpoint=LOOKUP_ENDPOINT)

    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    for path in documents or []:
        library.ingest(path)

    from crea.agent.workspace import RouteWorkbench
    from crea.services.route_validation import RouteValidationService
    from crea.sources.aizynthfinder import AiZynthFinderAdapter

    evidence_sink: list = []
    # The real engine is attempted and reports its own status; when it cannot be
    # installed the agent sees that rather than a route from nowhere.
    routes = RouteWorkbench(provider=AiZynthFinderAdapter(),
                            validator=RouteValidationService())
    registry = build_registry(
        LiteratureService(providers, access_provider=access),
        library, evidence_sink, routes=routes,
        entities=(knowledge_base.entity_registry()
                  if knowledge_base is not None else None),
        reactions=(knowledge_base.reaction_registry()
                   if knowledge_base is not None else None),
        knowledge_base=knowledge_base)
    agent = ResearchAgent(llm=llm, registry=registry, documents=library,
                          evidence=evidence_sink)
    return agent, library


def build_live_workflow_stack(*, knowledge_base=None):
    """A ResearchWorkflow over the real providers (Phase 4, §121.L).

    Same contract as `build_offline_workflow_stack`, with live literature
    (OpenAlex/Crossref -- official APIs, no credential), PubChem for
    compound names, the AiZynthFinder worker for routes, and an extraction
    model only when an LLM key is configured. Provider failures surface as
    the tool failures they are -- a PARTIAL run, never a fabricated one.
    """
    import os

    from crea.agent.workspace import IdentityState, RouteWorkbench
    from crea.services.identity import IdentityResolutionService
    from crea.services.research_workflow import ResearchWorkflow
    from crea.services.route_validation import RouteValidationService
    from crea.sources.aizynthfinder import AiZynthFinderAdapter

    transport = HttpxTransport()
    literature = LiteratureService(providers={
        "openalex": OpenAlexProvider(transport=transport),
        "crossref": CrossrefProvider(transport=transport),
    })

    compound_provider = None
    try:
        from crea.providers.pubchem import PubChemProvider

        compound_provider = PubChemProvider(transport=transport)
    except Exception:  # noqa: BLE001 - name lookup is optional
        compound_provider = None

    extractor = None
    if os.environ.get("CREA_LLM_API_KEY", "").strip():
        try:
            from crea.llm import LLMConfig, OpenAICompatibleLLMProvider
            from crea.services.reaction_extraction import ReactionExtractor

            extractor = ReactionExtractor(
                OpenAICompatibleLLMProvider(LLMConfig.from_env()))
        except Exception:  # noqa: BLE001 - without a usable key: no extraction
            extractor = None

    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    evidence_sink: list = []
    routes = RouteWorkbench(provider=AiZynthFinderAdapter(),
                            validator=RouteValidationService())
    identity = IdentityState(service=IdentityResolutionService(
        compound_provider=compound_provider))
    entities = (knowledge_base.entity_registry()
                if knowledge_base is not None else None)
    reactions = (knowledge_base.reaction_registry()
                 if knowledge_base is not None else None)
    semantic_resolver = _live_semantic_resolver(
        identity_service=identity.service, compound_provider=compound_provider,
        entity_registry=entities)
    registry = build_registry(
        literature, library, evidence_sink, extractor=extractor,
        routes=routes, identity=identity, entities=entities,
        reactions=reactions, knowledge_base=knowledge_base,
        semantic_resolver=semantic_resolver)
    workflow = ResearchWorkflow(
        registry, evidence_sink=evidence_sink, documents=library,
        entities=entities, reactions=reactions,
        knowledge_base=knowledge_base)
    return workflow, library, evidence_sink, registry


def build_live_intelligence_stack(*, knowledge_base=None, policy=None):
    """The *enforcing* tool registry over the real providers (M2.0).

    Same providers, tools and identity chain as the live workflow stack.
    What differs is the registry: `build_agent_tools` enforces the M1.1
    execution policy, which is what a model-driven loop needs -- the model
    picks the tool, and the registry still decides whether the network, a
    model-calling tool or a persistent write is allowed here.

    `policy` defaults to the permissive-but-checked live one: network and
    model-calling tools are allowed (that is what "live" means), and
    persistent writes still require a human approval on the call.

    Returns `(registry, library, evidence_sink)`. The caller supplies the
    LLM: a composition root wires the credential, and this module never
    builds a model of its own.
    """
    import os

    from crea.agent.tool_surface import build_agent_tools
    from crea.agent.tools import ExecutionPolicy
    from crea.agent.workspace import IdentityState, RouteWorkbench
    from crea.services.identity import IdentityResolutionService
    from crea.services.route_validation import RouteValidationService
    from crea.sources.aizynthfinder import AiZynthFinderAdapter

    transport = HttpxTransport()
    literature = LiteratureService(providers={
        "openalex": OpenAlexProvider(transport=transport),
        "crossref": CrossrefProvider(transport=transport),
    })

    compound_provider = None
    try:
        from crea.providers.pubchem import PubChemProvider

        compound_provider = PubChemProvider(transport=transport)
    except Exception:  # noqa: BLE001 - name lookup is optional
        compound_provider = None

    extractor = None
    if os.environ.get("CREA_LLM_API_KEY", "").strip():
        try:
            from crea.llm import LLMConfig, OpenAICompatibleLLMProvider
            from crea.services.reaction_extraction import ReactionExtractor

            extractor = ReactionExtractor(
                OpenAICompatibleLLMProvider(LLMConfig.from_env()))
        except Exception:  # noqa: BLE001 - without a usable key: no extraction
            extractor = None

    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    evidence_sink: list = []
    routes = RouteWorkbench(provider=AiZynthFinderAdapter(),
                            validator=RouteValidationService())
    identity = IdentityState(service=IdentityResolutionService(
        compound_provider=compound_provider))
    entities = (knowledge_base.entity_registry()
                if knowledge_base is not None else None)
    reactions = (knowledge_base.reaction_registry()
                 if knowledge_base is not None else None)
    semantic_resolver = _live_semantic_resolver(
        identity_service=identity.service, compound_provider=compound_provider,
        entity_registry=entities)
    registry = build_agent_tools(
        literature, library, evidence_sink, extractor=extractor, routes=routes,
        identity=identity, entities=entities, reactions=reactions,
        knowledge_base=knowledge_base, semantic_resolver=semantic_resolver,
        policy=policy or ExecutionPolicy(allow_network=True,
                                         allow_model_calls=True))
    return registry, library, evidence_sink


ENV_CROSSREF_MAILTO = "CREA_CROSSREF_MAILTO"
ENV_OPENALEX_API_KEY = "CREA_OPENALEX_API_KEY"
ENV_OPENALEX_MAILTO = "CREA_OPENALEX_MAILTO"


def build_literature_providers():
    """Crossref + OpenAlex over the real transport, configured from the env.

    The operator's contact address and any OpenAlex key are read from the
    environment (or `.env`) here, in the composition root -- never hard-coded,
    never logged, never written onto a record. Both providers work anonymously;
    the mailto and the key are enrichments.
    """
    from crea.core.env import load_local_env
    from crea.sources.crossref import CrossrefProvider
    from crea.sources.openalex import OpenAlexProvider

    load_local_env()
    transport = HttpxTransport()
    return {
        "crossref": CrossrefProvider(
            transport, mailto=os.environ.get(ENV_CROSSREF_MAILTO, "").strip() or None),
        "openalex": OpenAlexProvider(
            transport,
            api_key=os.environ.get(ENV_OPENALEX_API_KEY, "").strip() or None,
            mailto=os.environ.get(ENV_OPENALEX_MAILTO, "").strip() or None),
    }


def build_unpaywall_provider():
    """The Unpaywall access provider over the real transport (Stage 4.3).

    The operator's contact address is read from the environment here, in the
    composition root -- never hard-coded. Without it the provider is
    `missing_key`, a normal state that leaves permission UNKNOWN.
    """
    from crea.core.env import load_local_env
    from crea.sources.unpaywall import UnpaywallProvider

    load_local_env()
    email = os.environ.get(ENV_UNPAYWALL_EMAIL, "").strip()
    return UnpaywallProvider(HttpxTransport(), email=email or None)


def build_pubchem_exact_provider():
    """PubChem exact-identity provider over the real transport (Stage 2.3).

    Lives here -- the live composition root -- because it names the one real
    HTTP transport, which `test_no_network.py` permits only in composition
    roots. The application layer receives the provider, never the transport.
    """
    from crea.providers.pubchem_exact import PubChemExactProvider
    from crea.sources.http import HttpxTransport

    return PubChemExactProvider(transport=HttpxTransport())


def _live_semantic_resolver(*, identity_service, compound_provider,
                            entity_registry):
    """The semantic spine over the live stack (CREA 4.0 Stage B-1).

    Name chain order (Stage A's evidence): OPSIN (deterministic systematic
    nomenclature) then PubChem (the canonical compound integration). NCI
    CIR stays out until its service terms are cleared (docs/LICENSES.md).
    A provider that cannot be consulted is a diagnostic; the chain
    continues (§8/§10).
    """
    from crea.providers.name_lookup import CompoundNameLookup, OpsinNameLookup
    from crea.semantic.naming import NameResolver
    from crea.semantic.resolver import SemanticResolver

    providers = [OpsinNameLookup()]
    if compound_provider is not None:
        providers.append(CompoundNameLookup(compound_provider))
    return SemanticResolver(
        identity_service=identity_service,
        name_resolver=NameResolver(providers),
        entity_registry=entity_registry)

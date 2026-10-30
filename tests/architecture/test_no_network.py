"""The no-network guard.

The requirement is that the default suite is offline, not that it happens to
be. Two mechanisms enforce it:

1. this file -- a static check that no production module imports a network
   library or declares a live endpoint;
2. the session-scoped socket patch in `tests/conftest.py` -- a runtime check
   that no connection is ever opened.

Either alone would be a convention. Together they make an accidental request a
test failure.
"""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "crea"

#: Full module paths, not package roots. `urllib.parse` is string handling and
#: `urllib.request` is a network client; forbidding the root would ban the
#: harmless one along with the dangerous one.
NETWORK_MODULES = {
    "httpx", "http", "http.client", "socket", "ssl", "requests", "urllib3",
    "urllib.request", "urllib.error", "ftplib", "telnetlib", "smtplib",
    "xmlrpc", "websockets", "aiohttp", "aiofiles", "boto3", "google",
    "anthropic", "openai",
    # our own real transport, by either name
    "crea.sources.http",
}


def _imports(path: pathlib.Path) -> set[str]:
    """Every module named by an import, at full dotted depth."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
                # a top-level package import is enough to be the whole package
                out.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
            out.add(node.module.split(".")[0])
    return out


def _all_modules() -> list[pathlib.Path]:
    return sorted(SRC.rglob("*.py"))


#: The modules allowed to reach the network. Named here rather than inferred,
#: so adding another is a visible, deliberate edit.
#:
#: `sources/http.py` is the literature/document transport. `llm/openai_compatible.py`
#: is the one module permitted to talk to a model endpoint -- the agent above it
#: never sees an HTTP client. Neither is imported by `core/` or `chem/`, which is
#: asserted separately below.
NETWORK_ALLOWED = {"sources/http.py", "llm/openai_compatible.py"}

#: Composition roots. These wire real providers and the real transport together
#: and therefore have to name them; everything that *uses* the stack still
#: goes through a service and may not. Named rather than inferred, so adding
#: another is a visible, deliberate edit.
COMPOSITION_ROOTS = {"agent/live.py", "agent/workspace_stack.py"}

#: Client libraries. Distinct from the modules above: `crea.sources.http` is
#: our own transport, and naming it is fine; `httpx` is the library behind it.
CLIENT_LIBRARIES = {"httpx", "requests", "aiohttp", "urllib.request", "urllib3"}


@pytest.mark.parametrize("path", _all_modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_production_module_imports_a_network_library(path):
    rel = str(path.relative_to(SRC)).replace("\\", "/")
    offenders = _imports(path) & NETWORK_MODULES
    if rel in NETWORK_ALLOWED:
        assert offenders, f"{rel} is the network module and should import one"
        return
    if rel in COMPOSITION_ROOTS:
        # It may name `crea.sources.http`, the one place a client is created.
        # It may not reach past that for a client library of its own.
        direct = {o for o in offenders if o in CLIENT_LIBRARIES}
        assert not direct, f"{rel} must reach the transport, not {sorted(direct)}"
        return
    assert not offenders, f"{rel} imports {sorted(offenders)}; only {NETWORK_ALLOWED} may"


#: Endpoints may only be written down in a provider module, and only once the
#: contract sheet records that provider as VERIFIED.
ENDPOINT_ALLOWED = {"sources/openalex.py", "sources/crossref.py",
                   "sources/unpaywall.py", "llm/openai_compatible.py"}


def test_no_production_module_defines_a_live_url():
    """An endpoint lives in a provider, or nowhere. Verified in the contract sheet."""
    offenders = []
    for path in _all_modules():
        rel = str(path.relative_to(SRC)).replace("\\", "/")
        if rel in ENDPOINT_ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for marker in ("https://api.", "http://api.", "api.openalex.org",
                       "api.crossref.org", "api.unpaywall.org",
                       "content.openalex.org"):
            if marker in text and "PHASE2_DESIGN" not in text:
                offenders.append(f"{rel}: {marker}")
    assert not offenders, f"endpoint outside a provider module: {offenders}"


def test_the_transport_seam_has_no_concrete_implementation():
    """The base Transport stays abstract; the real one is a named subclass."""
    from crea.sources.base import Response, Transport
    from crea.sources.http import HttpxTransport

    import asyncio

    with pytest.raises(NotImplementedError):
        asyncio.run(Transport().request("GET", "https://example.org"))
    assert Response(200, {}).status_code == 200

    # The real transport is checked by shape, not by construction: the default
    # suite must run with no network dependency installed at all.
    assert issubclass(HttpxTransport, Transport)
    assert HttpxTransport.request is not Transport.request


def test_the_retrieval_branch_does_not_exist_yet():
    """`get_fulltext` must not have grown a transport call."""
    from crea.sources.base import BaseProvider
    import inspect

    source = inspect.getsource(BaseProvider.get_fulltext)
    assert "self.transport" not in source
    assert "await" not in source


def test_the_guard_itself_is_live():
    """A guard that cannot fail is not a guard.

    Attempts a real outbound connection and asserts the guard rejects it. If
    this test ever passes silently because the connection succeeded, the
    offline guarantee is gone and everything above it is theatre.

    Only meaningful while the guard is installed: when the operator has turned
    live testing on, the guard is lifted by design and there is nothing here
    to assert.
    """
    import os
    import socket

    if os.environ.get("CREA_LIVE_TESTS") == "1":
        pytest.skip("the guard is lifted while live testing is on")

    with pytest.raises(AssertionError, match="outbound connection"):
        socket.create_connection(("api.openalex.org", 443), timeout=1)


#: A provider must not know another provider exists. Cross-source identity,
#: merging and access reconciliation are the service layer's job, and a
#: provider that reached for a sibling would make the boundary untestable.
PROVIDER_MODULES = {"sources/openalex.py", "sources/crossref.py",
                    "sources/unpaywall.py"}
SIBLING_PROVIDERS = {"crea.sources.openalex", "crea.sources.crossref",
                     "crea.sources.unpaywall"}


@pytest.mark.parametrize("rel", sorted(PROVIDER_MODULES))
def test_a_provider_never_imports_another_provider(rel):
    imported = _imports(SRC / rel)
    others = {m for m in SIBLING_PROVIDERS if m not in imported
              or m == f"crea.sources.{pathlib.Path(rel).stem}"}
    # a module may import the shared base, never a sibling
    for name in imported:
        if name in SIBLING_PROVIDERS:
            assert name == f"crea.sources.{pathlib.Path(rel).stem}",                 f"{rel} imports sibling provider {name}"
    assert True, others


@pytest.mark.parametrize("rel", ["services/literature_merge.py",
                                 "services/access_reconciliation.py"])
def test_the_service_layer_does_not_import_provider_modules(rel):
    """Cross-provider work happens here, on `Paper` objects, not on providers.

    The services reason about records and evidence. Reaching into a provider
    module would tie the orchestration to one adapter's internals and make the
    boundary impossible to swap.
    """
    imported = _imports(SRC / rel)
    assert not (imported & SIBLING_PROVIDERS), f"{rel} imports a provider module"
    assert "crea.sources.http" not in imported, f"{rel} imports the transport"


#: The service layer orchestrates. It must not acquire chemistry, network, or
#: model capabilities, and it must not grow a ranking.
SERVICE_ORCHESTRATION_ONLY = ["services/literature.py"]


@pytest.mark.parametrize("rel", SERVICE_ORCHESTRATION_ONLY)
def test_the_service_layer_stays_orchestration(rel):
    path = SRC / rel
    imported = _imports(path)
    source = path.read_text(encoding="utf-8")

    # no chemistry, no network, no model
    assert not (imported & {"rdkit", "httpx", "requests", "anthropic", "openai"}),         f"{rel} gained a capability the layers below it own"
    # no provider internals
    assert not (imported & SIBLING_PROVIDERS), f"{rel} imports a provider module"
    # ranking is deliberately absent, and that is an architectural position
    for banned in ("relevance_score", "semantic_rank", "rerank"):
        assert banned not in source, f"{rel} introduced {banned}"


#: The evidence layer must be reachable from nothing below it, and must reach
#: nothing below it either: Paper in, Evidence out, with no chemistry and no
#: network in between.
EVIDENCE_MODULES = ["services/evidence.py"]
FORBIDDEN_IN_EVIDENCE = {
    "rdkit", "httpx", "requests", "urllib.request", "anthropic", "openai",
    "crea.chem", "crea.sources.openalex", "crea.sources.crossref",
    "crea.sources.unpaywall", "crea.services.literature",
    "crea.services.matching", "crea.services.dedup",
}


@pytest.mark.parametrize("rel", EVIDENCE_MODULES)
def test_the_evidence_layer_depends_on_models_and_nothing_else(rel):
    imported = _imports(SRC / rel)
    offenders = imported & FORBIDDEN_IN_EVIDENCE
    assert not offenders, f"{rel} depends on {sorted(offenders)}"
    # it does import the models it transforms, which is the whole point
    assert "crea.core.models.evidence" in imported
    assert "crea.core.models.literature" in imported


def test_the_evidence_models_do_not_depend_on_any_provider():
    for rel in ("core/models/evidence.py", "core/policy/evidence.py"):
        imported = _imports(SRC / rel)
        assert not (imported & SIBLING_PROVIDERS), f"{rel} imports a provider"
        assert "rdkit" not in imported
        assert "httpx" not in imported


def test_the_literature_service_produces_no_evidence():
    """A search is not a citation. The two must not be fused."""
    source = (SRC / "services" / "literature.py").read_text(encoding="utf-8")
    for banned in ("Evidence", "Claim", "evidence_identity", "Locator"):
        assert banned not in source, f"LiteratureService now produces {banned}"


#: pypdf lives here and nowhere else. A PDF library is an adapter; the moment
#: one is imported by a model or a service, swapping parsers stops being a
#: one-file change.
PDF_PARSER_ALLOWED = {"sources/pdf.py"}
PDF_PARSERS = {"pypdf", "PyPDF2", "fitz", "pdfminer", "pdfplumber"}

DOCUMENT_MODULES = [
    "core/models/document.py",
    "core/policy/document.py",
    "services/documents.py",
    "services/document_sections.py",
    "services/document_evidence.py",
    "sources/pdf.py",
]


@pytest.mark.parametrize("rel", DOCUMENT_MODULES)
def test_only_the_pdf_adapter_imports_a_pdf_library(rel):
    imported = _imports(SRC / rel)
    found = {m for m in imported
             if m.split(".")[0] in PDF_PARSERS or m in PDF_PARSERS}
    if rel in PDF_PARSER_ALLOWED:
        return
    assert not found, f"{rel} imports a PDF library directly: {sorted(found)}"


def test_the_evidence_service_never_sees_a_pdf_library():
    """It sees ParsedDocument, or nothing."""
    for rel in ("services/evidence.py", "services/document_evidence.py",
                 "core/models/evidence.py", "core/policy/evidence.py"):
        imported = _imports(SRC / rel)
        assert not {m for m in imported
                    if m.split(".")[0] in PDF_PARSERS}, rel


def test_the_document_service_depends_on_no_literature_provider():
    for rel in ("services/documents.py", "services/document_evidence.py",
                 "core/models/document.py"):
        imported = _imports(SRC / rel)
        assert not (imported & SIBLING_PROVIDERS), f"{rel} imports a provider"
        assert "crea.services.literature" not in imported, rel


def test_the_document_layer_never_reaches_the_network():
    for rel in DOCUMENT_MODULES:
        imported = _imports(SRC / rel)
        for banned in ("httpx", "requests", "urllib.request", "crea.sources.http"):
            assert banned not in imported, f"{rel} imports {banned}"


def test_the_chem_core_knows_nothing_about_documents():
    offenders = []
    for path in (SRC / "chem").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for banned in ("pypdf", "LocalDocument", "ParsedDocument", "pdf"):
            if banned in text:
                offenders.append(f"{path.name}: {banned}")
    assert not offenders, offenders


def test_core_does_not_import_a_pdf_parser():
    for path in (SRC / "core").rglob("*.py"):
        imported = _imports(path)
        assert not {m for m in imported if m.split(".")[0] in PDF_PARSERS}, path


# --- Phase 3: the agent layer ---------------------------------------------

AGENT_MODULES = [
    "agent/loop.py", "agent/tools.py", "agent/toolsets.py",
    "agent/workspace.py", "agent/offline.py", "agent/demo_pdf.py",
    "agent/prompt.py", "core/models/research.py",
    "llm/base.py", "llm/fake.py", "report/research.py",
    # M2.0: the AI Research Intelligence reads a model's reply and turns it
    # into a decision. It is exactly the module this guard exists for.
    "agent/intelligence.py", "agent/intelligence_prompt.py",
    "agent/intelligence_runtime.py",
    "core/models/research_decision.py",
    "app/research_intelligence.py", "report/intelligence.py",
]

#: The one module allowed to hold an HTTP client for a model endpoint.
LLM_PROVIDER = "llm/openai_compatible.py"


def test_no_module_ever_evaluates_model_supplied_text():
    """The one rule that cannot bend.

    A model names a tool and supplies JSON. There is no eval, no exec, no
    dynamic import anywhere in the agent path, and no argument path that
    reaches a shell.
    """
    banned = ("eval(", "exec(", "compile(", "__import__", "os.system",
              "subprocess.", "importlib.import_module")
    offenders = []
    for rel in AGENT_MODULES:
        text = (SRC / rel).read_text(encoding="utf-8")
        for needle in banned:
            if needle in text:
                offenders.append(f"{rel}: {needle}")
    assert not offenders, offenders


def test_the_agent_talks_to_providers_only_through_services():
    """The agent may use a service; it may not reach into an adapter."""
    for rel in ("agent/tools.py", "agent/toolsets.py", "agent/loop.py",
                "agent/workspace.py"):
        imported = _imports(SRC / rel)
        assert not (imported & SIBLING_PROVIDERS), f"{rel} imports a provider"
        assert "crea.sources.http" not in imported, rel


def test_the_agent_layer_reaches_no_network_on_its_own():
    """The agent above the provider must not import an HTTP client itself."""
    for rel in AGENT_MODULES:
        imported = _imports(SRC / rel)
        for banned in ("httpx", "requests", "urllib.request", "aiohttp"):
            assert banned not in imported, f"{rel} imports {banned}"


def test_core_and_chem_import_no_llm_and_no_http():
    """`core` must not know a model exists, and `chem` must not know either."""
    for package in ("core", "chem"):
        for path in (SRC / package).rglob("*.py"):
            imported = _imports(path)
            assert not any(i == "crea.llm" or i.startswith("crea.llm.")
                           for i in imported), path
            for banned in ("httpx", "requests", "aiohttp"):
                assert banned not in imported, f"{path} imports {banned}"


def test_the_offline_demo_never_imports_the_test_suite():
    """`src/` must not reach into `tests/`, or the package only works in pytest."""
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "tests.fakes" in text or "from tests" in text:
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, offenders


def test_the_llm_provider_carries_no_capability_of_its_own():
    """A model turns a prompt into text. Nothing else.

    The concrete provider is allowed to hold an HTTP client -- that is the one
    thing it is for -- but it still reaches no chemistry, no service and no
    provider.
    """
    concrete = _imports(SRC / LLM_PROVIDER)
    for banned in ("crea.chem", "crea.services", "crea.sources", "rdkit"):
        assert not any(i == banned or i.startswith(banned + ".") for i in concrete),             f"{LLM_PROVIDER} imports {banned}"

    for rel in ("llm/base.py", "llm/fake.py"):
        imported = _imports(SRC / rel)
        for banned in ("crea.chem", "crea.services", "crea.sources", "rdkit"):
            assert not any(i == banned or i.startswith(banned + ".")
                           for i in imported), f"{rel} imports {banned}"


def test_the_chem_core_is_untouched_by_the_agent():
    for path in (SRC / "chem").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for banned in ("crea.agent", "crea.llm", "ResearchAgent", "ToolRegistry"):
            assert banned not in text, f"{path.name} knows about the agent"


# --- Phase 3.3: the route layer's boundaries -------------------------------

ROUTE_MODULES = [
    "core/models/route.py",
    "providers/retrosynthesis.py",
    "providers/fake_retrosynthesis.py",
    "sources/aizynthfinder.py",
    "services/route_validation.py",
]


def _code_only(rel: str) -> str:
    """Source with comments and docstrings stripped.

    These guards are about what the code does. A module whose docstring
    explains that it does *not* do something should not fail a check for doing
    it -- and did once, which is why they read the parsed tree.
    """
    import ast as _ast
    import textwrap as _tw

    tree = _ast.parse(_tw.dedent((SRC / rel).read_text(encoding="utf-8")))
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Module, _ast.ClassDef, _ast.FunctionDef,
                             _ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], _ast.Expr)
                    and isinstance(body[0].value, _ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
    return _ast.unparse(tree)

#: The retrosynthesis engine may only be named by its own adapter.
RETRO_ENGINE = {"aizynthfinder", "azf", "aizynthconf"}


@pytest.mark.parametrize("rel", ROUTE_MODULES)
def test_only_the_adapter_knows_the_engine(rel):
    imported = _imports(SRC / rel)
    named = {i for i in imported if i.split(".")[0] in RETRO_ENGINE}
    if rel == "sources/aizynthfinder.py":
        return          # the adapter is allowed to, and does, import it lazily
    assert not named, f"{rel} imports the engine: {sorted(named)}"


def test_chem_knows_nothing_about_routes():
    """The chemical core analyses structures; it does not plan syntheses."""
    for path in (SRC / "chem").rglob("*.py"):
        code = _code_only(str(path.relative_to(SRC)))
        for banned in ("aizynth", "retrosynth", "CandidateRoute",
                      "ReactionStep", "crea.agent", "crea.llm"):
            assert banned not in code, f"{path.name} knows about {banned}"


def test_the_route_layer_does_not_import_the_agent_or_an_llm():
    """A provider reports. It does not reason, and it does not plan."""
    for rel in ROUTE_MODULES:
        imported = _imports(SRC / rel)
        for banned in ("crea.agent", "crea.llm", "anthropic", "openai"):
            assert banned not in imported, f"{rel} imports {banned}"


def test_the_provider_layer_does_not_import_an_adapter():
    """`providers/fake_retrosynthesis` must not reach for the real one."""
    for rel in ("providers/retrosynthesis.py", "providers/fake_retrosynthesis.py"):
        imported = _imports(SRC / rel)
        assert "crea.sources.aizynthfinder" not in imported, rel


def test_route_validation_reaches_no_network_and_no_model():
    imported = _imports(SRC / "services/route_validation.py")
    for banned in ("httpx", "requests", "crea.llm", "crea.sources.http"):
        assert banned not in imported, banned


def test_the_adapter_does_no_literature_or_evidence_work():
    """It converts routes. It does not search, read, or cite."""
    code = _code_only("sources/aizynthfinder.py")
    for banned in ("LiteratureService", "ReactionRecord", "Evidence",
                   "search_local_document", "analyze_molecule",
                   "validate_route_step", "crea.agent"):
        assert banned not in code, f"the adapter reaches for {banned}"

"""Architecture constraints.

These tests exist because the layering is the design. They are cheap and they
fail loudly the moment a shortcut is taken -- for example the first time
someone calls `Chem.MolFromSmiles` from the service layer, or lets the
Chemical Core import a provider.
"""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "crea"

FORBIDDEN_IN_CHEM = {
    "httpx", "requests", "urllib", "aiohttp", "anthropic", "openai",
    "crea.providers", "crea.services", "crea.report", "crea.cli",
}
FORBIDDEN_IN_CORE = {
    "rdkit", "httpx", "requests", "anthropic",
    "crea.chem", "crea.providers", "crea.services", "crea.report", "crea.cli",
    # Phase 3.11: storage is an adapter below the domain, never part of it.
    "sqlite3",
}
FORBIDDEN_IN_SERVICES = {"rdkit", "sqlite3"}


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


def _python_files(package: str) -> list[pathlib.Path]:
    return sorted((SRC / package).rglob("*.py"))


@pytest.mark.parametrize("path", _python_files("chem"), ids=lambda p: p.name)
def test_chem_has_no_network_no_llm_no_providers(path):
    for name in _imports(path):
        root = name.split(".")[0]
        assert name not in FORBIDDEN_IN_CHEM, f"{path.name} imports {name}"
        assert root not in FORBIDDEN_IN_CHEM, f"{path.name} imports {name}"


@pytest.mark.parametrize("path", _python_files("core"), ids=lambda p: p.name)
def test_core_depends_on_nothing_below_it(path):
    for name in _imports(path):
        root = name.split(".")[0]
        assert name not in FORBIDDEN_IN_CORE, f"{path.name} imports {name}"
        assert root not in FORBIDDEN_IN_CORE, f"{path.name} imports {name}"


@pytest.mark.parametrize("path", _python_files("services"), ids=lambda p: p.name)
def test_services_do_not_reach_for_rdkit_directly(path):
    # The service layer orchestrates; it does not compute chemistry. Going
    # straight to RDKit from here would bypass the Chemical Core's contracts.
    for name in _imports(path):
        assert name not in FORBIDDEN_IN_SERVICES, f"{path.name} imports {name}"


def test_molfromsmiles_is_confined_to_the_chemical_core():
    """`normalize.py` is the only sanctioned way to build a molecule.

    Checked on the syntax tree rather than on the source text. A substring
    match fires on a docstring that *mentions* the parser -- and a guard that
    fires on prose is a guard people learn to work around. An attribute access
    is a call site wherever it appears, including the `getattr(Chem, "...")`
    form a text search would miss.
    """
    offenders = []
    for package in ("core", "services", "report", "cli", "providers"):
        for path in _python_files(package):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in (
                    "MolFromSmiles", "MolFromSmarts",
                ):
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
                    break
    assert not offenders, f"molecule parsing outside the Chemical Core: {offenders}"


def test_layering_is_importable_in_order():
    import crea.chem  # noqa: F401
    import crea.core  # noqa: F401
    import crea.providers  # noqa: F401
    import crea.report  # noqa: F401
    import crea.services  # noqa: F401


def test_the_entity_layer_stays_inside_its_boundaries():
    """Phase 3.8.1 (§19), extended for 3.8.2. The entity and mention models
    are core: no agent, no llm, no sources, no providers -- and therefore no
    PubChem, no network. The registry and mention resolver are services that
    consume identity resolutions: no RDKit (the services rule), and the same
    forbidden reaches as the models. A mention resolver that could import a
    language model could mint an identity from a sentence."""
    forbidden = ("crea.agent", "crea.llm", "crea.sources", "crea.providers", "rdkit")
    for path in (SRC / "core" / "models" / "entity.py",
                 SRC / "core" / "models" / "mention_resolution.py",
                 SRC / "core" / "models" / "reaction_knowledge.py",
                 SRC / "services" / "entity_registry.py",
                 SRC / "services" / "mention_resolution.py",
                 SRC / "services" / "reaction_builder.py",
                 SRC / "services" / "route_reaction_linking.py"):
        for name in _imports(path):
            assert not any(name == f or name.startswith(f + ".")
                           for f in forbidden), (
                f"{path.name} imports {name}: the entity layer must not")


def test_the_reaction_services_go_through_interfaces_not_implementations():
    """Phase 3.9 (§57). The reaction layer consumes identity resolutions,
    matchers and validators -- never a provider implementation, never
    AiZynthFinder, never a language model. OpenAlex/Crossref/PubChem live
    behind the provider interface; the reaction services must not reach
    around it."""
    forbidden = (
        "crea.agent", "crea.llm", "rdkit",
        "crea.sources", "crea.providers",
        "aizynthfinder",
    )
    for path in (SRC / "services" / "reaction_builder.py",
                 SRC / "services" / "route_reaction_linking.py"):
        for name in _imports(path):
            assert not any(name == f or name.startswith(f + ".")
                           for f in forbidden), (
                f"{path.name} imports {name}: the reaction layer must go "
                f"through interfaces, not implementations")


# ---------------------------------------------------------------------------
# Phase 3.11 (§84/§85): persistence is an adapter, not a new architecture
# ---------------------------------------------------------------------------


def test_the_repository_layer_is_sql_and_nothing_else():
    """Repositories may speak SQLite (§84) -- that is their whole job --
    and must never speak to a model, a network provider or the agent. The
    moment a repository imports PubChem, it is a provider wearing a
    database's name (§85)."""
    forbidden = (
        "crea.llm", "crea.agent", "crea.providers", "crea.sources",
        "rdkit", "openai", "anthropic", "httpx", "requests", "aizynthfinder",
    )
    for path in _python_files("repositories"):
        for name in _imports(path):
            assert not any(name == f or name.startswith(f + ".")
                           for f in forbidden), (
                f"{path.name} imports {name}: local persistence is not a "
                f"provider, a model or an agent")


def test_the_knowledge_base_service_depends_on_no_provider_and_no_llm():
    """The facade (§26) orchestrates storage. It must not reach OpenAI,
    OpenRouter, PubChem HTTP or any provider implementation: writes arrive
    as validated domain objects (§107), never as model output."""
    forbidden = (
        "crea.llm", "crea.sources", "crea.providers",
        "openai", "anthropic", "openrouter", "httpx", "requests",
    )
    for name in _imports(SRC / "services" / "knowledge_base.py"):
        assert not any(name == f or name.startswith(f + ".")
                       for f in forbidden), (
            f"knowledge_base.py imports {name}: the facade stores domain "
            f"objects and nothing else")


def test_the_report_and_the_agent_never_touch_sqlite():
    """Reports consume domain models and query results (§84); the agent
    goes through the service/repository abstraction (§60). Neither may
    open a database: a report that queries SQL is a second, unreviewed
    data path. The agent may import the repository *error taxonomy*
    (`crea.repositories.errors`) -- §59 defines those names exactly so
    callers can branch on them instead of on SQLite's own strings -- but
    never the store itself."""
    offenders = []
    for path in _python_files("report"):
        for name in _imports(path):
            if name == "sqlite3" or name.startswith("crea.repositories"):
                offenders.append(f"{path.relative_to(SRC)} -> {name}")
    for path in _python_files("agent"):
        for name in _imports(path):
            if name == "sqlite3" or name == "crea.repositories.sqlite":
                offenders.append(f"{path.relative_to(SRC)} -> {name}")
    assert not offenders, f"presentation and tools reach into storage: {offenders}"


def test_research_intelligence_reads_knowledge_and_touches_nothing_else():
    """Phase 3.12 (§84/§95). Coverage, gaps, plans and synthesis are
    deterministic readings of stored knowledge: no LLM, no provider, no
    RDKit, no SQL. The same holds for the integrity audit -- it reports
    the chain, it does not recompute chemistry or re-fetch sources."""
    forbidden = (
        "crea.llm", "crea.sources", "crea.providers", "crea.agent",
        "rdkit", "sqlite3", "openai", "anthropic", "httpx", "requests",
    )
    for path in (SRC / "services" / "research_intelligence.py",
                 SRC / "services" / "knowledge_integrity.py",
                 SRC / "core" / "models" / "knowledge.py"):
        for name in _imports(path):
            assert not any(name == f or name.startswith(f + ".")
                           for f in forbidden), (
                f"{path.name} imports {name}: research intelligence is a "
                f"deterministic reading of knowledge and nothing else")


def test_the_workbench_layers_stay_above_the_engine():
    """Phase 4 (§2/§39-§41). The layering is

        core -> chem -> services -> repositories -> agent -> report
            -> app -> api -> Web UI

    and nothing may reach down past its neighbours. The application layer
    orchestrates use cases: no RDKit, no SQL, no transport, not even a web
    framework (it is transport-free). The API layer speaks HTTP and may
    import FastAPI -- but never sqlite3, never RDKit, never the store:
    Frontend -> HTTP API -> Application -> Service -> Repository -> SQLite
    is the only path (§39)."""
    app_forbidden = {
        "rdkit", "sqlite3", "fastapi", "starlette", "uvicorn",
        "crea.repositories.sqlite", "crea.llm",
    }
    api_forbidden = {
        "rdkit", "sqlite3", "crea.repositories.sqlite",
    }
    offenders = []
    for path in _python_files("app"):
        for name in _imports(path):
            if name in app_forbidden or name == "fastapi":
                offenders.append(f"{path.relative_to(SRC)} -> {name}")
    for path in _python_files("api"):
        for name in _imports(path):
            if name in api_forbidden:
                offenders.append(f"{path.relative_to(SRC)} -> {name}")
    assert not offenders, f"the workbench reached below its layer: {offenders}"


# ---------------------------------------------------------------------------
# CREA 4.0 Stage B-1 (§5/§8/§9/§11): the semantic layer's boundaries
# ---------------------------------------------------------------------------


def test_the_semantic_layer_reads_deterministic_chemistry_and_nothing_else():
    """`semantic/` classifies, delegates, collects and registers. It may
    import core, chem and services; it may never import a provider, a
    network client, an LLM, a store, or RDKit directly (the chem layer is
    the only RDKit home, §3)."""
    forbidden = (
        "crea.providers", "crea.sources", "crea.agent", "crea.llm",
        "crea.app", "crea.api", "crea.repositories", "sqlite3",
        "rdkit", "httpx", "requests", "openai", "anthropic",
    )
    offenders = []
    for path in _python_files("semantic"):
        for name in _imports(path):
            assert not any(name == f or name.startswith(f + ".")
                           for f in forbidden), (
                f"{path.name} imports {name}: the semantic layer is "
                f"deterministic chemistry and identity, nothing else")


def test_pubchem_has_one_upper_entry():
    """Stage A found the source/provider pair; B-1 settles it: `crea.
    providers.pubchem` is THE integration, `crea.sources.pubchem` is its
    transport and may not be imported by business code (§11/acceptance 9)."""
    import ast as _ast

    allowed = {"providers/pubchem.py", "providers/name_lookup.py"}
    offenders = []
    for package in ("core", "chem", "semantic", "services", "repositories",
                    "agent", "app", "api", "report", "tools", "cli.py"):
        root = SRC / package
        paths = (list(root.rglob("*.py")) if root.is_dir()
                 else ([root] if root.exists() else []))
        for path in paths:
            for name in _imports(path):
                if name == "crea.sources.pubchem":
                    rel = str(path.relative_to(SRC)).replace("\\", "/")
                    if rel not in allowed:
                        offenders.append(rel)
    assert not offenders, (
        f"PubChem reached outside its canonical entry: {offenders}")


def test_the_engine_speaks_one_worker_protocol():
    """Three workers (retrosynthesis, DECIMER, OPSIN), ONE wire format (§9:
    no third protocol). The envelope lives in engine/protocol.py and every
    worker script announces the same protocol string."""
    import ast as _ast

    protocol_module = SRC / "engine" / "protocol.py"
    text = protocol_module.read_text(encoding="utf-8")
    assert 'PROTOCOL_VERSION = "crea-engine/1"' in text

    workers = pathlib.Path(__file__).resolve().parents[2] / "workers" / "chemistry"
    for script in sorted(workers.glob("*.py")):
        body = script.read_text(encoding="utf-8")
        assert '"crea-engine/1"' in body, (
            f"{script.name} does not speak crea-engine/1")
        # no second envelope definition
        assert "PROTOCOL_VERSION =" not in body, (
            f"{script.name} redefines the protocol version")


# ---------------------------------------------------------------------------
# CREA 4.0 Stage C (§17): the tool layer's boundaries
# ---------------------------------------------------------------------------


def test_the_tool_layer_never_bypasses_the_semantic_spine_or_forks_identity():
    """Pins 2+3: no name -> structure outside `semantic`, no second
    identity system. Tools call SemanticResolver/NameResolver; they never
    construct identity candidates/resolutions of their own, and they never
    define a class that pretends to be an identity model."""
    import ast as _ast

    offenders = []
    classes = []
    for path in _python_files("tools"):
        text = path.read_text(encoding="utf-8")
        tree = _ast.parse(text)
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef):
                classes.append(node.name)
            if isinstance(node, _ast.Call):
                fn = node.func
                name = getattr(fn, "id", getattr(fn, "attr", ""))
                if name in ("ChemicalIdentityCandidate",
                            "ChemicalIdentityResolution"):
                    offenders.append(f"{path.name}:{node.lineno} builds {name}")
        for name in _imports(path):
            if name == "crea.services.identity":
                offenders.append(f"{path.name} reaches identity directly")
    assert not offenders, offenders
    assert not [c for c in classes if "Identity" in c], (
        f"a second identity vocabulary appeared in tools: {classes}")


def test_the_tool_layer_keeps_sql_and_the_worker_envelope_below_it():
    """Pins 4+6: tools do not speak `crea-engine/1` themselves (the
    execution adapter does) and never touch SQLite (Tool -> Service ->
    Repository)."""
    offenders = []
    for path in _python_files("tools"):
        for name in _imports(path):
            if name in ("sqlite3", "crea.repositories.sqlite") or (
                    name == "crea.engine.protocol"):
                offenders.append(f"{path.name} -> {name}")
    assert not offenders, offenders


def test_tools_do_not_copy_rdkit_logic():
    """Pin 5: the chemistry layer is the only RDKit home -- including for
    the tools that report chemistry."""
    offenders = []
    for package in ("tools", "semantic"):
        for path in _python_files(package):
            for name in _imports(path):
                if name == "rdkit" or name.startswith("rdkit."):
                    offenders.append(f"{path.relative_to(SRC)} -> {name}")
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# CREA 4.0 Stage D (§22): reaction intelligence stays on the rails
# ---------------------------------------------------------------------------


def test_the_reaction_layer_references_entities_and_never_mints_identity():
    """Pins 1+2+6: reactions point AT ChemicalEntity ids and never derive
    a chemical identity of their own -- no structure parsing in the
    reaction modules, no identity-vocabulary classes, and exactly ONE
    Reaction model."""
    import ast as _ast

    reaction_modules = [SRC / "core" / "models" / "reaction_knowledge.py",
                        SRC / "core" / "models" / "reaction.py",
                        SRC / "services" / "reaction_builder.py",
                        SRC / "services" / "reaction_extraction.py",
                        SRC / "services" / "reaction_matching.py"]
    for path in reaction_modules:
        for name in _imports(path):
            assert name not in ("crea.chem", "crea.semantic", "rdkit"), (
                f"{path.name} imports {name}: the reaction layer must not "
                f"compute structure or resolve identity itself")

    for path in _python_files("core") + _python_files("services") +             _python_files("tools"):
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef) and node.name in (
                    "NewReaction", "ReactionEntity", "ReactionGraphNode",
                    "ReactionObject", "ReactionV2"):
                raise AssertionError(f"second reaction model: {path.name}")
    # the ONE Reaction class lives in the knowledge module
    text = (SRC / "core" / "models" / "reaction_knowledge.py").read_text(
        encoding="utf-8")
    tree = _ast.parse(text)
    names = [n.name for n in tree.body if isinstance(n, _ast.ClassDef)
             and n.name == "Reaction"]
    assert names == ["Reaction"]


def test_reaction_construction_keeps_evidence_and_provenance():
    """Pin 5: a Reaction carries evidence and provenance as part of its
    contract, and the save path honours them (behavioural tests assert the
    FK on evidence)."""
    for path in (SRC / "core" / "models" / "reaction_knowledge.py",
                 SRC / "services" / "reaction_builder.py"):
        assert "Provenance" in _imports(path) or (
            "crea.core.provenance" in _imports(path)), path.name


# ---------------------------------------------------------------------------
# CREA 4.0 Stage E (§5/§16.8): the Chemistry Agent reaches chemistry only
# through the injected ToolRegistry
# ---------------------------------------------------------------------------


def test_the_chemistry_agent_depends_on_the_tool_registry_abstraction_only():
    """The ChemistryAgent orchestrates; it must not import RDKit, the
    semantic/identity spine, a provider, a source, a service, a repository,
    or even the concrete tool layer. Every chemical capability arrives as a
    `ToolResult` through the injected `ToolRegistryLike`, and the tool
    schemas come from `registry.describe()` (§7) -- never a second
    hand-maintained list and never a direct reach into chemistry internals."""
    forbidden = (
        "rdkit", "sqlite3",
        "crea.chem", "crea.semantic", "crea.providers", "crea.sources",
        "crea.services", "crea.repositories", "crea.tools",
        "httpx", "requests", "openai", "anthropic",
    )
    path = SRC / "agent" / "chemistry_agent.py"
    for name in _imports(path):
        assert not any(name == f or name.startswith(f + ".")
                       for f in forbidden), (
            f"chemistry_agent.py imports {name}: the agent must reach "
            f"chemistry only through the injected ToolRegistry")


def test_the_chemistry_agent_reuses_the_one_result_vocabulary():
    """Stage E §4/§11: no second result model. `AgentResult` reuses the one
    `ToolResult` for its `tool_calls` and carries no `Evidence2`/
    `Provenance2`/`ToolResult2` class anywhere in the agent layer."""
    import ast as _ast

    agent = (SRC / "agent" / "chemistry_agent.py").read_text(encoding="utf-8")
    tree = _ast.parse(agent)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ClassDef) and node.name in (
                "ToolResult2", "Evidence2", "Provenance2", "AgentResult2"):
            raise AssertionError(f"second result vocabulary: {node.name}")
    # AgentResult holds ToolResults, not a parallel structure
    assert "tool_calls: list[ToolResult]" in agent


# ---------------------------------------------------------------------------
# CREA 4.0 Stage F (§22/§30): the workspace keeps the agent and chemistry
# on their own sides of the boundary
# ---------------------------------------------------------------------------


def test_the_workspace_app_reaches_chemistry_only_through_the_agent():
    """The workspace application orchestrates: it opens the knowledge base,
    persists messages and runs the injected agent. It must not import RDKit,
    the semantic spine, a provider, a source, the tool layer or SQLite -- the
    agent factory is injected, so chemistry stays behind it (§22)."""
    forbidden = (
        "rdkit", "sqlite3",
        "crea.chem", "crea.semantic", "crea.providers", "crea.sources",
        "crea.tools", "crea.repositories", "crea.llm",
    )
    path = SRC / "app" / "workspace.py"
    for name in _imports(path):
        assert not any(name == f or name.startswith(f + ".")
                       for f in forbidden), (
            f"app/workspace.py imports {name}: the workspace must reach "
            f"chemistry only through the injected agent")


def test_the_agent_never_reaches_the_workspace_repository():
    """Stage F §30: the ChemistryAgent receives a neutral message history and
    knows nothing about workspaces, their repository, or the app layer. The
    workspace orchestrates the agent; the agent never reaches back."""
    forbidden = (
        "crea.services.workspace", "crea.core.models.workspace",
        "crea.app", "crea.repositories",
    )
    path = SRC / "agent" / "chemistry_agent.py"
    for name in _imports(path):
        assert not any(name == f or name.startswith(f + ".")
                       for f in forbidden), (
            f"chemistry_agent.py imports {name}: the agent must not reach "
            f"the workspace repository")


def test_the_workspace_domain_reuses_the_one_message_and_result_vocabulary():
    """Stage F §3/§4: one message model, one result model. `WorkspaceMessage`
    reuses the one `ToolResult` for its `tool_calls` and no second message or
    result class exists in the workspace domain."""
    import ast as _ast

    path = SRC / "core" / "models" / "workspace.py"
    text = path.read_text(encoding="utf-8")
    tree = _ast.parse(text)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ClassDef) and node.name in (
                "ToolResult2", "Message2", "WorkspaceMessage2", "Evidence2"):
            raise AssertionError(f"second vocabulary: {node.name}")
    assert "list[ToolResult]" in text
    assert "from crea.core.models.research import ToolResult" in text


# ---------------------------------------------------------------------------
# CREA 4.0 Stage G (§8/§29/§30): the explorer is a chemistry capability,
# not an agent, and it composes the existing chemistry layer
# ---------------------------------------------------------------------------


def test_the_explorer_depends_on_chemistry_services_not_the_agent_or_web():
    """The ChemicalExplorer is a domain capability: it may use core, chem and
    services (the identity + reaction seams), and must never reach the agent,
    the app, the API, the web, the tool layer, the semantic spine, a provider
    or a store (§29). It composes `crea.chem` -- never re-implements RDKit."""
    forbidden = (
        "crea.agent", "crea.app", "crea.api", "crea.tools", "crea.semantic",
        "crea.providers", "crea.sources", "crea.repositories", "crea.llm",
        "rdkit", "sqlite3", "httpx", "requests",
    )
    path = SRC / "services" / "exploration.py"
    names = _imports(path)
    for name in names:
        assert not any(name == f or name.startswith(f + ".")
                       for f in forbidden), (
            f"exploration.py imports {name}: the explorer is chemistry, "
            f"not an agent or a web capability")
    assert any(n.startswith("crea.chem.") for n in names), (
        "the explorer should compose the existing chemistry layer (§8)")


def test_no_second_agent_appears_for_exploration():
    """Stage G §30: exploration is a `ChemicalExplorer`, never an
    `ExplorationAgent`/`ChemistryExplorationAgent`. The agent stays the one
    `ChemistryAgent`."""
    import ast as _ast

    for package in ("services", "tools", "agent", "app", "api"):
        for path in _python_files(package):
            tree = _ast.parse(path.read_text(encoding="utf-8"))
            for node in _ast.walk(tree):
                if isinstance(node, _ast.ClassDef) and node.name in (
                        "ExplorationAgent", "ChemistryExplorationAgent",
                        "ChemicalExplorationAgent"):
                    raise AssertionError(
                        f"second agent appeared: {path.name}:{node.name}")


def test_exploration_reuses_the_one_result_and_identity_vocabulary():
    """Stage G §5/§10: `ExplorationResult` reuses `ChemicalEntity` for its
    target and candidates -- no `ExplorationEntity2`, no second identity."""
    import ast as _ast

    path = SRC / "core" / "models" / "exploration.py"
    text = path.read_text(encoding="utf-8")
    tree = _ast.parse(text)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ClassDef) and node.name in (
                "Candidate2", "ExplorationEntity", "ExplorationEntity2",
                "ReactionEvidence2", "Relationship2"):
            raise AssertionError(f"second vocabulary: {node.name}")
    assert "from crea.core.models.entity import ChemicalEntity" in text


# ---------------------------------------------------------------------------
# CREA 4.0 Stage H (§22/§24): the transformation layer is chemistry over the
# existing Reaction, never an agent, never a second reaction model
# ---------------------------------------------------------------------------


def test_transformation_search_is_deterministic_chemistry_not_ai_or_network():
    """Stage 5.1 §1/§11: transformation search is SMARTS matching over the
    templates. It must not import a provider, a source, a transport, an LLM or
    a vector/embedding library, and it must not reach the network."""
    forbidden = (
        "crea.providers", "crea.sources", "crea.agent", "crea.llm",
        "crea.app", "crea.api", "httpx", "requests",
        "openai", "anthropic", "sentence_transformers", "faiss", "numpy",
    )
    path = SRC / "services" / "transformation_search.py"
    for name in _imports(path):
        assert not any(name == f or name.startswith(f + ".")
                       for f in forbidden), (
            f"transformation_search.py imports {name}: search is deterministic "
            f"chemistry, not AI and not a provider")


def test_the_final_audit_is_offline_deterministic_and_never_repairs():
    """Stage 7.4 §六十八/§六十九/§八十八: the audit reads and reports. It must
    not import a provider, a source, an agent, an LLM, a transport, SQLite or
    RDKit -- no network, no model, no repair."""
    forbidden = (
        "crea.providers", "crea.sources", "crea.agent", "crea.llm",
        "crea.api", "httpx", "requests", "openai", "anthropic", "sqlite3",
        "rdkit",
    )
    for rel in ("services/final_audit.py", "core/models/final_audit.py"):
        path = SRC / rel
        for name in _imports(path):
            assert not any(name == f or name.startswith(f + ".")
                           for f in forbidden), (
                f"{rel} imports {name}: the audit is offline, deterministic and "
                f"read-only")
    # and it must not contain a write/repair path (checked on code, not prose:
    # the module docstring says it never repairs, and that must not fail it)
    import ast as _ast

    tree = _ast.parse((SRC / "services" / "final_audit.py").read_text(
        encoding="utf-8"))
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Module, _ast.ClassDef, _ast.FunctionDef,
                             _ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], _ast.Expr)
                    and isinstance(body[0].value, _ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
    code = _ast.unparse(tree)
    for banned in ("save_", "delete_", "add_reference", "link_literature",
                   "create("):
        assert banned not in code, f"final_audit.py calls {banned}"


def test_the_study_report_is_read_only_and_reruns_nothing():
    """Stage 7.3 §四十一/§四十二/§七十三/§七十四: building a report reads what
    the session holds. It must not import a provider, a source, an agent, an
    LLM, a transport, SQLite, or RDKit -- it does not recompute chemistry, run a
    search, call a model, or touch the database directly."""
    forbidden = (
        "crea.providers", "crea.sources", "crea.agent", "crea.llm",
        "crea.api", "httpx", "requests", "openai", "anthropic", "sqlite3",
        "rdkit",
    )
    for rel in ("services/study_report.py", "report/study.py",
                "core/models/study_report.py"):
        path = SRC / rel
        for name in _imports(path):
            assert not any(name == f or name.startswith(f + ".")
                           for f in forbidden), (
                f"{rel} imports {name}: the report is read-only assembly, not a "
                f"re-run of chemistry, search or a model")


def test_the_study_report_never_claims_verification():
    """§六十四: the report organises results; it never says VERIFIED."""
    code = (SRC / "services" / "study_report.py").read_text(encoding="utf-8")
    for banned in ('"VERIFIED"', '"VALIDATED"', "Final Scientific Conclusion"):
        assert banned not in code, f"study_report.py contains {banned}"


def test_the_task_workflow_orchestrates_services_and_never_calls_a_model():
    """Stage 7.2 §十六/§五十: the workflow orchestrates; services do the work.
    The workflow service itself must not import an LLM, an agent, a provider or
    a transport -- AI work goes through the executor's injected factories."""
    forbidden = (
        "crea.providers", "crea.sources", "crea.agent", "crea.llm",
        "crea.api", "httpx", "requests", "openai", "anthropic", "sqlite3",
    )
    for rel in ("services/task_workflow.py", "core/models/task_workflow.py"):
        path = SRC / rel
        for name in _imports(path):
            assert not any(name == f or name.startswith(f + ".")
                           for f in forbidden), (
                f"{rel} imports {name}: the workflow orchestrates services, it "
                f"does not call a model, an agent or a provider")


def test_no_task_workflow_creates_a_task_from_a_model():
    """§七十二: the task set comes from a template or the user. The workflow
    module must build tasks only from its static TEMPLATES / explicit specs."""
    code = (SRC / "services" / "task_workflow.py").read_text(encoding="utf-8")
    # the workflow never calls an LLM to author tasks
    for banned in ("self._llm", "llm.complete", "EvidenceAgent(", "ModificationAnalysisService("):
        assert banned not in code, f"task_workflow.py references {banned}"


def test_the_evidence_agent_reaches_facts_only_through_readonly_tools():
    """Stage 6.4 §四/§五/§八/§十九/§二十/§二十一: the agent loop uses the tool
    registry and nothing else -- no database, no file, no SQL, no HTTP, no
    provider, no LLM SDK. Every tool it is given is read-only."""
    # The loop reaches facts only through the registry: no service, no chem,
    # no store, no provider, no transport.
    loop_forbidden = (
        "crea.services", "crea.chem", "crea.repositories", "crea.providers",
        "crea.sources", "crea.app", "crea.api", "sqlite3", "httpx",
        "requests", "openai", "anthropic",
    )
    for name in _imports(SRC / "agent" / "evidence_agent.py"):
        assert not any(name == f or name.startswith(f + ".") for f in loop_forbidden), (
            f"evidence_agent.py imports {name}: the loop uses the tool registry, "
            f"never a service, a store or a provider directly")

    # The tool layer wraps services and read-only chemistry, but never a
    # provider, a store, a transport or a model SDK.
    tools_forbidden = (
        "crea.providers", "crea.sources", "crea.app", "crea.api", "sqlite3",
        "crea.repositories", "httpx", "requests", "openai", "anthropic",
    )
    for name in _imports(SRC / "agent" / "evidence_tools.py"):
        assert not any(name == f or name.startswith(f + ".") for f in tools_forbidden), (
            f"evidence_tools.py imports {name}: tools are read-only wrappers "
            f"over services, never a provider or a store")

    # no executable path anywhere in the agent layer
    for rel in ("agent/evidence_agent.py", "agent/evidence_tools.py"):
        code = (SRC / rel).read_text(encoding="utf-8")
        for banned in ("eval(", "exec(", "subprocess", "os.system"):
            assert banned not in code, f"{rel} contains {banned}"


def test_modification_analysis_depends_on_an_llm_abstraction_not_a_vendor():
    """Stage 6.2 §43/§44: the service orchestrates the AI Context and an
    injected LLM. It must not import a vendor SDK, a transport, a provider, an
    agent, or SQLite -- the model is an abstraction, and facts come only from
    the AI Context."""
    forbidden = (
        "sqlite3", "crea.repositories", "crea.providers", "crea.sources",
        "crea.agent", "httpx", "requests", "openai", "anthropic", "ollama",
    )
    for rel in ("services/modification_analysis.py",
                "services/modification_grounding.py",
                "services/modification_prompt.py",
                "services/route_reasoning.py",
                "services/route_reasoning_prompt.py"):
        path = SRC / rel
        for name in _imports(path):
            assert not any(name == f or name.startswith(f + ".")
                           for f in forbidden), (
                f"{rel} imports {name}: modification analysis uses the LLM "
                f"abstraction and the AI Context, never a vendor or the DB")


def test_ai_context_assembles_facts_and_never_invokes_a_model():
    """Stage 6.1 §2/§31: the AI Context layer reads the deterministic chemistry
    and produces a structured DTO. It must not import an LLM, an agent, a
    provider, a transport, or a prompt template -- it assembles facts, it does
    not reason."""
    forbidden = (
        "crea.providers", "crea.sources", "crea.agent", "crea.llm",
        "crea.app", "crea.api", "httpx", "requests", "openai", "anthropic",
    )
    path = SRC / "services" / "ai_context.py"
    for name in _imports(path):
        assert not any(name == f or name.startswith(f + ".")
                       for f in forbidden), (
            f"ai_context.py imports {name}: AI context is fact assembly, not a "
            f"model call and not a provider")


def test_route_explorer_is_a_bounded_deterministic_local_search():
    """Stage 5.4 §32/§33/§36: route exploration consumes the local graph and
    templates. It must not import an LLM, an agent, a provider, a transport, or
    a retrosynthesis engine -- it is not AI retrosynthesis."""
    forbidden = (
        "crea.providers", "crea.sources", "crea.agent", "crea.llm",
        "crea.app", "crea.api", "httpx", "requests",
        "openai", "anthropic", "aizynthfinder",
    )
    path = SRC / "services" / "route_explorer.py"
    for name in _imports(path):
        assert not any(name == f or name.startswith(f + ".")
                       for f in forbidden), (
            f"route_explorer.py imports {name}: the explorer is a bounded "
            f"forward search over local data, not AI retrosynthesis")


def test_reaction_graph_is_a_deterministic_local_layer():
    """Stage 5.3 §36/§37: the reaction graph depends on the molecule library,
    the templates, evidence and papers -- never an LLM, an agent, a network
    client, a provider, or a graph database."""
    forbidden = (
        "crea.providers", "crea.sources", "crea.agent", "crea.llm",
        "crea.app", "crea.api", "httpx", "requests",
        "openai", "anthropic", "neo4j", "networkx", "rdflib",
    )
    path = SRC / "services" / "reaction_graph.py"
    for name in _imports(path):
        assert not any(name == f or name.startswith(f + ".")
                       for f in forbidden), (
            f"reaction_graph.py imports {name}: the graph is a deterministic "
            f"local layer, not a provider, a model or a graph database")


def test_reaction_comparison_is_local_and_makes_no_recommendation():
    """Stage 5.2 §16/§20: comparison is facts, deterministically, from local
    data. It must not import a provider, a source, a transport, an LLM, or any
    scoring/ML library."""
    forbidden = (
        "crea.providers", "crea.sources", "crea.agent", "crea.llm",
        "crea.app", "crea.api", "httpx", "requests",
        "openai", "anthropic", "sklearn", "numpy",
    )
    path = SRC / "services" / "reaction_comparison.py"
    for name in _imports(path):
        assert not any(name == f or name.startswith(f + ".")
                       for f in forbidden), (
            f"reaction_comparison.py imports {name}: comparison is local facts, "
            f"not a provider and not a model")


def test_literature_evidence_linking_is_local_not_a_provider():
    """Stage 4.4 §8: linking a paper to a reaction template resolves a DOI
    against the local literature store. The service must not import a
    provider, a source, a transport or an LLM -- evidence linking never
    triggers a network lookup."""
    forbidden = (
        "crea.providers", "crea.sources", "crea.agent", "crea.llm",
        "crea.app", "crea.api", "httpx", "requests", "rdkit", "sqlite3",
    )
    path = SRC / "services" / "reaction_evidence.py"
    for name in _imports(path):
        assert not any(name == f or name.startswith(f + ".")
                       for f in forbidden), (
            f"reaction_evidence.py imports {name}: evidence linking is local, "
            f"never a provider lookup")


def test_the_transformation_service_is_a_capability_not_an_agent_or_web():
    """The TransformationEvidenceService queries the injected reaction
    registry; it must not import the agent, app, API, web, tool layer, the
    semantic spine, a provider, a store or RDKit (§22). It orchestrates an
    injected `ReactionRegistry` -- nothing else."""
    forbidden = (
        "crea.agent", "crea.app", "crea.api", "crea.tools", "crea.semantic",
        "crea.providers", "crea.sources", "crea.repositories", "crea.llm",
        "rdkit", "sqlite3", "crea.chem",
    )
    path = SRC / "services" / "transformation.py"
    for name in _imports(path):
        assert not any(name == f or name.startswith(f + ".")
                       for f in forbidden), (
            f"transformation.py imports {name}: it is a one-hop reaction "
            f"query, not chemistry, not an agent, not a web capability")


def test_transformation_references_the_existing_reaction_and_never_replaces_it():
    """Stage H §4: `Transformation` references the Stage D `Reaction`; there
    is no `TransformationReaction`, `Reaction2` or second reaction model."""
    import ast as _ast

    path = SRC / "core" / "models" / "transformation.py"
    text = path.read_text(encoding="utf-8")
    tree = _ast.parse(text)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ClassDef) and node.name in (
                "TransformationReaction", "Reaction2", "TransformationEntity",
                "TransformationEvidence2", "Evidence2", "Provenance2"):
            raise AssertionError(f"second model: {node.name}")
    assert "from crea.core.models.reaction_knowledge import Reaction" in text
    assert "reaction: Reaction" in text

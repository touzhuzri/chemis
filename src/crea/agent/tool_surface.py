"""The unified agent tool surface (CREA V1.1, M1.1).

One registry, one canonical name per capability, one declared policy per
tool. `build_agent_tools()` composes the three existing tool sets -- research,
workspace and evidence -- without rewriting any of them:

    build_registry (26)  ─┐
    default_tool_registry (10) ─┼─▶  build_agent_tools()  ─▶  ToolRegistry
    build_evidence_tool_registry (8) ─┘        (enforcing)

The old registries stay exactly as they were: their callers (ResearchWorkflow,
ResearchAgent, ChemistryAgent, EvidenceAgent, CLI, API) are untouched. The new
registry is `enforce_policy=True`, so it is the one that actually checks
preconditions, network/model policy, persistent-write approval and output
budgets.

What this module does NOT do:

* it does not add chemistry -- every canonical tool delegates to an existing
  tool or an existing service;
* it does not rename a tool out of existence -- every old name that is not a
  canonical name is registered as an alias;
* it does not silently drop fields -- the duplicate tools are merged or
  enveloped explicitly, and the merge is tested.

Alias rule: an alias points at a canonical tool that exists in this registry.
The four canonical names that were "implemented but unregistered" in the M1.0
census (`search_reaction_templates`, `compare_reaction_templates`,
`get_evidence_for_paper`, `explore_routes`) are wrappers over the evidence
registry. `reaction_evidence` is an alias of `get_reaction_template_evidence`,
not of `get_evidence_for_reaction`: one is keyed by reaction *template*, the
other by stored reaction id, and conflating them would be the exact rename-only
lie this phase forbids.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, Field

from crea.agent.evidence_tools import build_evidence_tool_registry
from crea.agent.tools import (
    Access,
    Approval,
    Effects,
    ExecutionPolicy,
    ToolRegistry,
    ToolSpec,
    _ToolFailure,
)
from crea.agent.toolsets import build_registry
from crea.agent.workspace import IdentityState
from crea.core.models.research import ToolResult, ToolStatus
from crea.services.entity_registry import ChemicalEntityRegistry
from crea.services.reaction_builder import ReactionRegistry
from crea.services.reaction_comparison import ReactionComparisonService
from crea.services.reaction_evidence import ReactionEvidenceService
from crea.services.route_explorer import RouteExplorerService
from crea.services.transformation_search import TransformationSearchService
from crea.tools.registry import default_tool_registry


# ---------------------------------------------------------------------------
# Canonical names
# ---------------------------------------------------------------------------

RESEARCH_CANONICALS = (
    "analyze_molecule",
    "compare_structures",
    "create_claim",
    "extract_reactions",
    "get_document_evidence",
    "get_entity",
    "get_evidence_for_reaction",
    "get_existing_knowledge",
    "get_knowledge_gaps",
    "get_papers_for_entity",
    "get_reaction",
    "get_reaction_details",
    "get_routes_for_entity",
    "lookup_paper",
    "resolve_chemical_identity",
    "retrosynthesize",
    "route_evidence_report",
    "search_entities",
    "search_knowledge",
    "search_literature",
    "search_local_document",
    "search_reaction_evidence",
    "search_reactions",
    "synthesize_evidence",
    "validate_reaction",
    "validate_route_step",
)

#: Workspace-only capabilities that the research registry does not have.
WORKSPACE_CANONICALS = (
    "name_structure",
    "search_chemical",
    "explore_chemical_space",
    "find_chemical_transformations",
)

#: Canonical name -> the evidence-registry tool it wraps.
EVIDENCE_CANONICALS = {
    "get_reaction_template": "get_reaction_template",
    "search_reaction_templates": "search_transformations",
    "compare_reaction_templates": "compare_reactions",
    "get_reaction_template_evidence": "reaction_evidence",
    "get_evidence_for_paper": "literature_for_paper",
    "explore_routes": "route_search",
}

#: Old name -> (canonical name, the old arguments model).
#: These remain callable; they are not a second implementation.
ALIASES = {
    "resolve_chemical": "resolve_chemical_identity",
    "analyze_structure": "analyze_molecule",
    "inspect_molecule": "analyze_molecule",
    "reaction_evidence": "get_reaction_template_evidence",
    "literature_for_paper": "get_evidence_for_paper",
    "route_search": "explore_routes",
    "search_transformations": "search_reaction_templates",
    "compare_reactions": "compare_reaction_templates",
}

ALL_CANONICALS = (set(RESEARCH_CANONICALS) | set(WORKSPACE_CANONICALS)
                  | set(EVIDENCE_CANONICALS))


# ---------------------------------------------------------------------------
# Declared policy (verified against the implementation, not the old name)
# ---------------------------------------------------------------------------


def _identity_fact(data: Any) -> tuple[str, ...]:
    if isinstance(data, dict) and data.get("chemistry_allowed"):
        return ("identity_resolved",)
    return ()


def _routes_fact(data: Any) -> tuple[str, ...]:
    if isinstance(data, dict) and data.get("route_count"):
        return ("routes_held",)
    return ()


def _meta(*, access=("local",), effects="none", approval="none",
          timeout=60.0, timeout_mode="advisory", limit=8000,
          requires=(), retryable=True, network_optional=False,
          model_optional=False, sets_facts=(), fact_rule=None) -> dict:
    return {
        "access": tuple(access),
        "effects": effects,
        "approval": approval,
        "timeout_s": timeout,
        "timeout_mode": timeout_mode,
        "output_limit_chars": limit,
        "requires": tuple(requires),
        "retryable": retryable,
        "network_optional": network_optional,
        "model_optional": model_optional,
        "sets_facts": tuple(sets_facts),
        "fact_rule": fact_rule,
    }


_NETWORK_READ = _meta(access=("network",), timeout=30.0, timeout_mode="native")
_LOCAL_READ = _meta()
_LOCAL_READ_BIG = _meta(limit=12000)

ANNOTATIONS: dict[str, dict] = {
    # -- research: computation and knowledge --------------------------------
    "analyze_molecule": _meta(limit=4000),
    "compare_structures": _meta(limit=6000),
    "create_claim": _LOCAL_READ,
    "get_entity": _LOCAL_READ,
    "get_evidence_for_reaction": _LOCAL_READ,
    "get_existing_knowledge": _meta(limit=6000),
    "get_knowledge_gaps": _meta(limit=6000),
    "get_papers_for_entity": _LOCAL_READ,
    "get_reaction": _LOCAL_READ,
    "get_routes_for_entity": _LOCAL_READ,
    "search_entities": _LOCAL_READ,
    "search_knowledge": _meta(limit=6000),
    "search_reactions": _LOCAL_READ,
    "synthesize_evidence": _meta(limit=8000),
    # -- research: discovery ------------------------------------------------
    "lookup_paper": _NETWORK_READ,
    "search_literature": _meta(access=("network",), timeout=30.0,
                               timeout_mode="native", limit=12000),
    "search_reaction_evidence": _meta(access=("network",), timeout=45.0,
                                      timeout_mode="native", limit=12000),
    "search_local_document": _meta(timeout=60.0, limit=8000),
    # -- research: document -> evidence / conditions ------------------------
    "get_document_evidence": _meta(effects=Effects.TEMPORARY.value,
                                   timeout=60.0, limit=8000),
    "get_reaction_details": _meta(access=("local", "model"),
                                  effects=Effects.TEMPORARY.value,
                                  timeout=120.0, timeout_mode="native",
                                  retryable=False, model_optional=True,
                                  limit=8000),
    "extract_reactions": _meta(access=("local", "model"),
                               effects=Effects.PERSISTENT.value,
                               approval=Approval.REQUIRED.value,
                               timeout=120.0, timeout_mode="native",
                               retryable=False, model_optional=True,
                               limit=8000),
    # -- research: identity -------------------------------------------------
    "resolve_chemical_identity": _meta(
        access=("local", "network"),
        effects=Effects.PERSISTENT.value,
        approval=Approval.REQUIRED.value,
        timeout=60.0, timeout_mode="native", network_optional=True,
        limit=8000, fact_rule=_identity_fact),
    # -- research: routes ---------------------------------------------------
    "retrosynthesize": _meta(effects=Effects.TEMPORARY.value, timeout=600.0,
                             timeout_mode="native", retryable=True,
                             requires=("identity_resolved",),
                             fact_rule=_routes_fact, limit=12000),
    "validate_route_step": _meta(effects=Effects.TEMPORARY.value,
                                 requires=("routes_held",), timeout=60.0),
    "validate_reaction": _meta(effects=Effects.TEMPORARY.value,
                               requires=("routes_held",), timeout=60.0),
    "route_evidence_report": _meta(effects=Effects.TEMPORARY.value,
                                   requires=("routes_held",), timeout=60.0,
                                   limit=12000),
    # -- workspace-only -----------------------------------------------------
    "name_structure": _meta(access=("local", "network"), timeout=60.0,
                            timeout_mode="native", network_optional=True),
    "search_chemical": _NETWORK_READ,
    "explore_chemical_space": _meta(
        access=("local", "network"),
        effects=Effects.PERSISTENT.value,
        approval=Approval.REQUIRED.value,
        timeout=60.0, timeout_mode="native", network_optional=True,
        limit=8000),
    "find_chemical_transformations": _LOCAL_READ,
    # -- evidence canonical wrappers ---------------------------------------
    "get_reaction_template": _LOCAL_READ,
    "search_reaction_templates": _LOCAL_READ,
    "compare_reaction_templates": _LOCAL_READ,
    "get_reaction_template_evidence": _LOCAL_READ,
    "get_evidence_for_paper": _LOCAL_READ,
    "explore_routes": _meta(timeout=60.0, limit=12000),
}

assert set(ANNOTATIONS) == ALL_CANONICALS, (
    "every canonical tool needs a declared policy")


# ---------------------------------------------------------------------------
# Unified argument model for the one tool whose callers disagree
# ---------------------------------------------------------------------------


class _SearchReactionsArgs(BaseModel):
    """Research filters plus the workspace's singular spelling.

    `reactant_entity_id` is the Stage C workspace argument; the research tool
    takes `reactant_entity_ids`. Accepting both is the explicit conversion --
    the singular value is appended to the list, never silently dropped.
    """

    entity_id: str | None = None
    product_entity_id: str | None = None
    reactant_entity_ids: list[str] = Field(default_factory=list)
    reactant_entity_id: str | None = Field(
        default=None,
        description="Legacy workspace spelling; converted to "
                    "reactant_entity_ids before the tool runs.")
    paper_id: str | None = None
    doi: str | None = None
    reaction_key: str | None = None
    structure: str | None = None
    structure_mode: str = "exact"


# ---------------------------------------------------------------------------
# Delegation helpers
# ---------------------------------------------------------------------------


def _call(registry, name: str, arguments: dict) -> ToolResult:
    """Invoke either a ToolRegistry (`execute`) or a ChemistryToolRegistry
    (`invoke`). Both return a ToolResult and never raise for a tool problem."""
    if hasattr(registry, "invoke"):
        return registry.invoke(name, arguments)
    return registry.execute(name, arguments)


def _data(result: ToolResult) -> dict:
    if result.status is not ToolStatus.OK:
        raise _ToolFailure(result)
    return result.data if isinstance(result.data, dict) else {"value": result.data}


def _research_delegate(research, name: str) -> Callable[..., Any]:
    def run(**kwargs):
        return research.execute(name, kwargs)

    return run


def _workspace_delegate(workspace, name: str) -> Callable[..., Any]:
    def run(**kwargs):
        return _call(workspace, name, kwargs)

    return run


def _evidence_delegate(evidence, underlying: str) -> Callable[..., Any]:
    def run(**kwargs):
        return evidence.execute(underlying, kwargs)

    return run


def _run_search_reactions(research, **kwargs):
    args = dict(kwargs)
    legacy = args.pop("reactant_entity_id", None)
    if legacy:
        ids = list(args.get("reactant_entity_ids") or [])
        if legacy not in ids:
            ids.append(legacy)
        args["reactant_entity_ids"] = ids
    return research.execute("search_reactions", args)


def _run_search_literature(research, **kwargs):
    data = _data(research.execute("search_literature", kwargs))
    data.setdefault("count", data.get("paper_count", 0))
    data.setdefault("paper_count", data.get("count", 0))
    data.setdefault("warnings", [])
    data.setdefault("note", "")
    return data


def _run_retrosynthesize(research, **kwargs):
    data = _data(research.execute("retrosynthesize", kwargs))
    data.setdefault("available", data.get("status") == "available")
    data.setdefault("provenance", {})
    # The workspace tool called this `reason_code`; derive it from the same
    # codes rather than dropping the field or inventing a new one.
    if "reason_code" not in data:
        codes = list(data.get("reason_codes") or [])
        data["reason_code"] = codes[0] if codes else ""
    # Routes live on the run's workbench so validate_route_step can grade
    # them. That is session state, not knowledge-base persistence.
    data["persisted"] = False
    data["stored_for_validation"] = bool(data.get("route_count"))
    return data


def _run_compare_structures(research, workspace, **kwargs):
    """Merge the two comparison surfaces. Both are deterministic and local."""
    successes: list[dict] = []
    failures: list[tuple[str, ToolResult]] = []
    try:
        successes.append(_data(research.execute("compare_structures", kwargs)))
    except _ToolFailure as exc:
        failures.append(("research", exc.result))
    try:
        successes.append(_data(_call(workspace, "compare_structures", kwargs)))
    except _ToolFailure as exc:
        failures.append(("workspace", exc.result))
    if not successes:
        raise _ToolFailure(failures[0][1])
    data: dict = {}
    for payload in successes:
        data.update(payload)
    if failures:
        # Never invent the missing half: say which surface failed and why.
        data["partial"] = True
        data["limitations"] = list(data.get("limitations") or []) + [
            f"{label}: {result.error or result.status.value}"
            for label, result in failures]
    return data


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


def build_agent_tools(
    literature=None,
    documents=None,
    evidence_sink: list | None = None,
    *,
    extractor=None,
    routes=None,
    validator=None,
    identity=None,
    entities=None,
    reactions=None,
    knowledge_base=None,
    semantic_resolver=None,
    name_resolver=None,
    compound_provider=None,
    retrosynthesis_provider=None,
    explorer=None,
    transformation_service=None,
    transformation_search=None,
    comparison: ReactionComparisonService | None = None,
    reaction_evidence: ReactionEvidenceService | None = None,
    route_explorer: RouteExplorerService | None = None,
    policy: ExecutionPolicy | None = None,
) -> ToolRegistry:
    """Build the one registry the future CoreAgent will use.

    Every backend is optional: a missing one does not remove the tool, it
    makes the tool report `unavailable` when called. That mirrors the existing
    composition roots and keeps the surface discoverable offline.
    """
    sink = evidence_sink if evidence_sink is not None else []
    entities = entities if entities is not None else ChemicalEntityRegistry()
    reactions = reactions if reactions is not None else ReactionRegistry()
    identity = identity if identity is not None else IdentityState()

    research = build_registry(
        literature, documents, sink, extractor=extractor, routes=routes,
        validator=validator, identity=identity, entities=entities,
        reactions=reactions, knowledge_base=knowledge_base,
        semantic_resolver=semantic_resolver)
    workspace = default_tool_registry(
        semantic_resolver=semantic_resolver, name_resolver=name_resolver,
        compound_provider=compound_provider, literature=literature,
        retrosynthesis_provider=retrosynthesis_provider, reactions=reactions,
        explorer=explorer, transformation_service=transformation_service)

    reaction_evidence = (reaction_evidence if reaction_evidence is not None
                        else ReactionEvidenceService(kb=knowledge_base))
    transformation_search = (transformation_search
                             if transformation_search is not None
                             else TransformationSearchService(
                                 evidence_service=reaction_evidence))
    comparison = (comparison if comparison is not None
                  else ReactionComparisonService(
                      kb=knowledge_base, search_service=transformation_search,
                      evidence_service=reaction_evidence))
    route_explorer = (route_explorer if route_explorer is not None
                      else RouteExplorerService(
                          kb=knowledge_base,
                          search_service=transformation_search))
    evidence = build_evidence_tool_registry(
        transformation_search=transformation_search, comparison=comparison,
        evidence=reaction_evidence, route=route_explorer, literature=literature)

    registry = ToolRegistry(enforce_policy=True,
                            policy=policy or ExecutionPolicy())
    #: Kept for diagnostics/tests; the unified registry is the only surface a
    #: CoreAgent should use.
    registry.research = research
    registry.workspace = workspace
    registry.evidence = evidence
    registry.evidence_sink = sink

    def register(name: str, description: str, arguments_model,
                 run, meta: dict) -> None:
        registry.register(ToolSpec(
            name=name, description=description,
            arguments_model=arguments_model, run=run, **meta))

    # -- research canonical tools ------------------------------------------
    for name in RESEARCH_CANONICALS:
        source = research.get(name)
        assert source is not None, name
        if name == "compare_structures":
            run = (lambda _research=research, _workspace=workspace, **kw:
                   _run_compare_structures(_research, _workspace, **kw))
            arguments_model = source.arguments_model
        elif name == "search_literature":
            run = (lambda _research=research, **kw:
                   _run_search_literature(_research, **kw))
            arguments_model = source.arguments_model
        elif name == "search_reactions":
            run = (lambda _research=research, **kw:
                   _run_search_reactions(_research, **kw))
            arguments_model = _SearchReactionsArgs
        elif name == "retrosynthesize":
            run = (lambda _research=research, **kw:
                   _run_retrosynthesize(_research, **kw))
            arguments_model = source.arguments_model
        else:
            run = _research_delegate(research, name)
            arguments_model = source.arguments_model
        register(name, source.description, arguments_model, run,
                 ANNOTATIONS[name])

    # -- workspace-only canonical tools ------------------------------------
    for name in WORKSPACE_CANONICALS:
        source = workspace.get(name)
        assert source is not None, name
        register(name, source.description, source.arguments_model,
                 _workspace_delegate(workspace, name), ANNOTATIONS[name])

    # -- evidence canonical wrappers ---------------------------------------
    for name, underlying in EVIDENCE_CANONICALS.items():
        source = evidence.get(underlying)
        assert source is not None, underlying
        register(name, source.description, source.arguments_model,
                 _evidence_delegate(evidence, underlying), ANNOTATIONS[name])

    # -- aliases (registered last; they point at a canonical that exists) --
    for alias, canonical in ALIASES.items():
        canonical_spec = registry.get(canonical)
        assert canonical_spec is not None, canonical
        if alias in ("resolve_chemical",):
            source = workspace.get(alias)
        elif alias in ("analyze_structure",):
            source = workspace.get(alias)
        else:
            source = evidence.get(alias)
        assert source is not None, alias
        _register_alias(registry, alias, canonical_spec, source.arguments_model)

    return registry


def _register_alias(registry: ToolRegistry, alias: str,
                    canonical: ToolSpec, arguments_model) -> None:
    """An alias validates its own (old) schema, then calls the canonical.

    The canonical is called directly, not through the registry: the alias
    already inherits the canonical's policy and approval, so a second check
    would be the same decision made twice.
    """
    def run(**kwargs):
        validated = canonical.arguments_model.model_validate(kwargs)
        return canonical.run(**validated.model_dump())

    registry.register(ToolSpec(
        name=alias,
        description=canonical.description,
        arguments_model=arguments_model,
        run=run,
        alias_of=canonical.name,
        canonical=False,
        access=canonical.access,
        effects=canonical.effects,
        approval=canonical.approval,
        timeout_s=canonical.timeout_s,
        timeout_mode=canonical.timeout_mode,
        output_limit_chars=canonical.output_limit_chars,
        requires=canonical.requires,
        retryable=canonical.retryable,
        sets_facts=canonical.sets_facts,
        fact_rule=canonical.fact_rule,
        network_optional=canonical.network_optional,
        model_optional=canonical.model_optional,
    ))


__all__ = [
    "ALIASES", "ALL_CANONICALS", "ANNOTATIONS", "EVIDENCE_CANONICALS",
    "RESEARCH_CANONICALS", "WORKSPACE_CANONICALS", "build_agent_tools",
]

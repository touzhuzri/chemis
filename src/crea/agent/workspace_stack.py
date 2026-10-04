"""The ChemistryAgent over the Stage C/D tool layer (CREA 4.0 Stage F).

A composition root, like `live.py`/`offline.py` but for the workspace: it
builds a `ChemistryAgent` wired to the real `default_tool_registry` --
the SemanticResolver, the name chain, the reaction registry -- over a
`KnowledgeBase` when one is given, or in-memory when it is not. The LLM is
injected; nothing here decides which vendor it is, and nothing here falls
back to a scripted model (§: a fabricated answer is worse than none).

Name lookups are best-effort: OPSIN (isolated worker) always, PubChem when
the transport is importable. A provider that cannot be consulted is a
diagnostic on the tool, never a crash and never a fabricated structure.
"""

from __future__ import annotations

from crea.agent.chemistry_agent import ChemistryAgent
from crea.tools.registry import default_tool_registry


def build_workspace_agent(llm, *, knowledge_base=None) -> ChemistryAgent:
    """A `ChemistryAgent` over the real chemistry tools (§5/§26).

    `knowledge_base`, when given, wires the persistent entity and reaction
    registries so a resolved identity and stored reactions survive across
    turns and across processes. Given none, everything is in-memory and the
    agent is still the real implementation over the real resolver.
    """
    from crea.semantic.naming import NameResolver
    from crea.semantic.resolver import SemanticResolver
    from crea.services.entity_registry import ChemicalEntityRegistry
    from crea.services.identity import IdentityResolutionService
    from crea.services.reaction_builder import ReactionRegistry

    entities = (knowledge_base.entity_registry()
                if knowledge_base is not None else ChemicalEntityRegistry())
    reactions = (knowledge_base.reaction_registry()
                 if knowledge_base is not None else ReactionRegistry())
    name_resolver, compound_provider = _name_chain()
    resolver = SemanticResolver(
        identity_service=IdentityResolutionService(),
        name_resolver=name_resolver, entity_registry=entities)

    tools = default_tool_registry(
        semantic_resolver=resolver, name_resolver=name_resolver,
        compound_provider=compound_provider, reactions=reactions)
    return ChemistryAgent(tools, llm)


def _name_chain():
    """OPSIN (deterministic nomenclature) and PubChem when importable.

    Same chain order as the live stack (Stage B-1): OPSIN first, PubChem
    second. NCI CIR stays out (opt-in, terms pending). A missing transport
    is not a failure -- the chain continues with what it has.
    """
    from crea.providers.name_lookup import OpsinNameLookup
    from crea.semantic.naming import NameResolver

    providers = [OpsinNameLookup()]
    compound_provider = None
    try:
        from crea.providers.name_lookup import CompoundNameLookup
        from crea.providers.pubchem import PubChemProvider
        from crea.sources.http import HttpxTransport

        compound_provider = PubChemProvider(transport=HttpxTransport())
        providers.append(CompoundNameLookup(compound_provider))
    except Exception:  # noqa: BLE001 - PubChem is an enrichment, not a gate
        compound_provider = None
    return NameResolver(providers), compound_provider


__all__ = ["build_workspace_agent"]

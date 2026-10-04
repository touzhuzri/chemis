"""The Chemistry Tool Registry: register, discover, invoke (§1/§3).

One place a capability becomes callable. The registry holds
`ChemistryTool`s, answers `describe()` with their declared surfaces (for
a human, a model, or a future agent), and executes them through the
contract -- so every invocation is schema-validated, timed, and reported
as a structured `ToolResult`.

`bridge_to(agent_registry)` registers every tool into the established
agent `ToolRegistry` as a `ToolSpec`: one contract, one bridge (§4). The
agent keeps its own registry mechanics; the tool layer does not fork
them.
"""

from __future__ import annotations

from crea.core.models.research import ToolResult, ToolStatus
from crea.tools.contract import ChemistryTool, ToolError, ToolErrorCode


class ChemistryToolRegistry:
    """Discoverable, invocable tool collection."""

    def __init__(self) -> None:
        self._tools: dict[str, ChemistryTool] = {}

    def register(self, tool: ChemistryTool) -> ChemistryTool:
        if not tool.name:
            raise ValueError("a tool without a name cannot be discovered")
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> ChemistryTool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> list[dict]:
        """The declared surfaces, sorted -- discovery without execution."""
        return [self._tools[name].describe() for name in self.names()]

    def invoke(self, name: str, arguments: dict | None = None,
               step_id: str = "", **kwargs) -> ToolResult:
        """Run one tool through its contract. Arguments travel as a dict
        (like the agent's `execute`), so no tool parameter can ever
        collide with the registry's own. Never raises for a tool-level
        problem -- the failure IS the result (§5)."""
        merged = dict(arguments or {})
        merged.update(kwargs)
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                tool=name, status=ToolStatus.ERROR,
                error_type=ToolErrorCode.NOT_FOUND.value,
                error=f"no tool named {name!r}; available: "
                      f"{', '.join(self.names())}",
                step_id=step_id)
        return tool.invoke(step_id=step_id, **merged)

    def bridge_to(self, agent_registry) -> None:
        """Register every tool with the agent's registry (§4)."""
        for name in self.names():
            agent_registry.register(self._tools[name].as_agent_spec())


def default_tool_registry(*, semantic_resolver=None, name_resolver=None,
                          compound_provider=None, literature=None,
                          retrosynthesis_provider=None,
                          reactions=None,
                          explorer=None,
                          transformation_service=None) -> ChemistryToolRegistry:
    """The representative Stage C toolset (§7-§10), wired to whatever
    backends the composition root supplies.

    A missing backend does not remove a tool: the tool stays discoverable
    and reports `unavailable` honestly (§: never fake success).

    `explorer` (Stage G) is the `ChemicalExplorer`; when none is supplied
    one is built over the resolver's entity registry and the reaction
    registry, exploring the *local* chemical space (§9).
    `transformation_service` (Stage H) is the `TransformationEvidenceService`;
    when none is supplied one is built over the reaction registry.
    """
    from crea.tools.chemistry import (
        AnalyzeStructureTool,
        CompareStructuresTool,
        ExploreChemicalSpaceTool,
        FindChemicalTransformationsTool,
        NameStructureTool,
        ResolveChemicalTool,
        RetrosynthesizeTool,
        SearchChemicalTool,
        SearchLiteratureTool,
        SearchReactionsTool,
    )

    if explorer is None:
        explorer = _default_explorer(semantic_resolver, reactions)
    if transformation_service is None:
        from crea.services.transformation import TransformationEvidenceService

        transformation_service = TransformationEvidenceService(reactions=reactions)

    registry = ChemistryToolRegistry()
    registry.register(ResolveChemicalTool(resolver=semantic_resolver))
    registry.register(NameStructureTool(name_resolver=name_resolver))
    registry.register(AnalyzeStructureTool())
    registry.register(CompareStructuresTool())
    registry.register(SearchChemicalTool(provider=compound_provider))
    registry.register(SearchLiteratureTool(literature=literature))
    registry.register(SearchReactionsTool(reactions=reactions))
    registry.register(RetrosynthesizeTool(provider=retrosynthesis_provider))
    registry.register(ExploreChemicalSpaceTool(explorer=explorer,
                                               resolver=semantic_resolver))
    registry.register(FindChemicalTransformationsTool(
        service=transformation_service))
    return registry


def _default_explorer(semantic_resolver, reactions):
    """The Stage G explorer over the local chemical space (§9)."""
    from crea.services.entity_registry import ChemicalEntityRegistry
    from crea.services.exploration import ChemicalExplorer, LocalEntitySpace

    entity_registry = (getattr(semantic_resolver, "_registry", None)
                       if semantic_resolver is not None else None)
    if entity_registry is None:
        entity_registry = ChemicalEntityRegistry()
        if semantic_resolver is not None:
            semantic_resolver.bind_entity_registry(entity_registry)
    return ChemicalExplorer(
        entity_registry=entity_registry, reactions=reactions,
        space=LocalEntitySpace(entity_registry))


__all__ = ["ChemistryToolRegistry", "default_tool_registry"]

"""The research task executor (CREA Stage 7.2).

Runs ONE task by calling the service its `task_type` names (§十七/§四十六). It
never re-implements chemistry and never routes a deterministic task through the
agent (§四十七): `ROUTE_EXPLORATION` calls the route explorer directly, not the
agent's tool. AI tasks (6.2/6.3/6.4) are called through the same executor (§十八/
§四十九), inheriting their read-only, bounded limits.

A missing input is `BLOCKED` (we cannot run); a tool/service/provider error is
`FAILED` (§三十三). The two are never confused.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crea.core.models.research_study import ArtifactType
from crea.core.models.task_workflow import TaskType

#: Which artifact a successful task produces.
_ARTIFACT_FOR = {
    TaskType.MOLECULE_INSPECTION: ArtifactType.MOLECULE_INSPECTION_RESULT,
    TaskType.TRANSFORMATION_SEARCH: ArtifactType.TRANSFORMATION_SEARCH_RESULT,
    TaskType.REACTION_COMPARISON: ArtifactType.REACTION_COMPARISON_RESULT,
    TaskType.LITERATURE_SEARCH: ArtifactType.LITERATURE_SEARCH_RESULT,
    TaskType.EVIDENCE_REVIEW: ArtifactType.EVIDENCE_REVIEW_RESULT,
    TaskType.ROUTE_EXPLORATION: ArtifactType.ROUTE_EXPLORATION,
    TaskType.MODIFICATION_ANALYSIS: ArtifactType.MODIFICATION_ANALYSIS,
    TaskType.ROUTE_REASONING: ArtifactType.ROUTE_REASONING,
    TaskType.AGENT_ANALYSIS: ArtifactType.AGENT_RESULT,
}

#: Required input keys per task type (§六十).
_REQUIRED_INPUTS = {
    TaskType.MOLECULE_INSPECTION: ("smiles",),
    TaskType.TRANSFORMATION_SEARCH: ("substrate",),
    TaskType.REACTION_COMPARISON: ("reaction_template_ids",),
    TaskType.LITERATURE_SEARCH: ("query",),
    TaskType.EVIDENCE_REVIEW: ("reaction_template_id",),
    TaskType.ROUTE_EXPLORATION: ("start", "target"),
    TaskType.MODIFICATION_ANALYSIS: ("query",),
    TaskType.ROUTE_REASONING: ("start", "target", "query"),
    TaskType.AGENT_ANALYSIS: ("question",),
}


@dataclass
class TaskOutcome:
    status: str                     # "completed" | "failed" | "blocked"
    artifact_type: ArtifactType | None = None
    content: dict = field(default_factory=dict)
    error: str = ""


class ResearchTaskExecutor:
    """Dispatch one task to the service that owns it.

    `kb` is the knowledge base the deterministic services read from. AI
    services are supplied as factories (built lazily); when a factory is None
    the AI task is `failed` with a provider-unavailable reason (§三十三).
    """

    def __init__(self, kb, *, transformation_search=None, comparison=None,
                 evidence=None, route=None, literature=None,
                 modification_factory=None, route_reasoning_factory=None,
                 agent_factory=None) -> None:
        self._kb = kb
        if transformation_search is None:
            from crea.services.transformation_search import TransformationSearchService

            transformation_search = TransformationSearchService(
                evidence_service=evidence)
        if comparison is None:
            from crea.services.reaction_comparison import ReactionComparisonService

            comparison = ReactionComparisonService(kb=kb)
        if evidence is None:
            from crea.services.reaction_evidence import ReactionEvidenceService

            evidence = ReactionEvidenceService(kb=kb)
        if route is None:
            from crea.services.route_explorer import RouteExplorerService

            route = RouteExplorerService(kb=kb)
        self._search = transformation_search
        self._comparison = comparison
        self._evidence = evidence
        self._route = route
        self._literature = literature
        self._modification_factory = modification_factory
        self._route_reasoning_factory = route_reasoning_factory
        self._agent_factory = agent_factory

    def run(self, task) -> TaskOutcome:
        missing = self._missing_inputs(task)
        if missing:
            return TaskOutcome(
                status="blocked", error="TASK_INPUT_NOT_FOUND: " + ", ".join(missing))
        dangling = self._dangling_refs(task)
        if dangling:
            return TaskOutcome(
                status="blocked", error="TASK_INPUT_NOT_FOUND: " + ", ".join(dangling))

        try:
            return self._dispatch(task)
        except Exception as exc:  # noqa: BLE001 - a task failing is an outcome
            return TaskOutcome(status="failed", error=f"{type(exc).__name__}: {exc}")

    # -- validation ---------------------------------------------------------

    @staticmethod
    def _missing_inputs(task) -> list[str]:
        required = _REQUIRED_INPUTS.get(task.task_type, ())
        return [k for k in required if not task.input_refs.get(k)]

    def _dangling_refs(self, task) -> list[str]:
        """Any existing-object id in the inputs must resolve (§六十)."""
        from crea.repositories.errors import NotFound

        refs = task.input_refs
        problems: list[str] = []
        for entity_id in _as_list(refs.get("molecule_ids")):
            try:
                if self._kb.get_entity(entity_id) is None:
                    problems.append(f"molecule {entity_id!r}")
            except NotFound:
                problems.append(f"molecule {entity_id!r}")
        for template_id in _as_list(refs.get("reaction_template_ids")):
            from crea.services.reaction_library import get_template

            if get_template(template_id) is None:
                problems.append(f"reaction {template_id!r}")
        for paper_id in _as_list(refs.get("paper_ids")):
            if self._kb.store.get_paper(paper_id) is None:
                problems.append(f"literature {paper_id!r}")
        return problems

    # -- dispatch -----------------------------------------------------------

    def _dispatch(self, task) -> TaskOutcome:
        kind = task.task_type
        refs = task.input_refs
        artifact = _ARTIFACT_FOR[kind]

        if kind is TaskType.MOLECULE_INSPECTION:
            content = _inspect(refs["smiles"])
        elif kind is TaskType.TRANSFORMATION_SEARCH:
            result = self._search.search(refs["substrate"], refs.get("target", ""))
            content = result.model_dump(mode="json")
        elif kind is TaskType.REACTION_COMPARISON:
            result = self._comparison.compare(
                _as_list(refs["reaction_template_ids"]),
                substrate=refs.get("substrate", ""), target=refs.get("target", ""))
            content = result.model_dump(mode="json")
        elif kind is TaskType.EVIDENCE_REVIEW:
            records = self._evidence.list_evidence(refs["reaction_template_id"])
            content = {"reaction_template_id": refs["reaction_template_id"],
                       "count": len(records),
                       "evidence": [r.model_dump(mode="json") for r in records]}
        elif kind is TaskType.LITERATURE_SEARCH:
            if self._literature is None:
                return TaskOutcome(status="failed",
                                   error="AI/PROVIDER_UNAVAILABLE: no literature service")
            from crea.core.models.service import LiteratureSearchRequest

            result = self._literature.search(LiteratureSearchRequest(
                query=refs["query"], limit=min(int(refs.get("limit", 5)), 10)))
            content = result.model_dump(mode="json")
        elif kind is TaskType.ROUTE_EXPLORATION:
            result = self._route.explore(
                refs["start"], refs["target"], int(refs.get("max_steps", 3)))
            content = result.model_dump(mode="json")
        elif kind is TaskType.MODIFICATION_ANALYSIS:
            service = self._factory(self._modification_factory, "modification analysis")
            if service is None:
                return TaskOutcome(status="failed", error="AI_PROVIDER_UNAVAILABLE")
            from crea.core.models.modification import ModificationAnalysisRequest

            content = service.analyze(ModificationAnalysisRequest(
                molecule_smiles=refs.get("substrate", refs.get("molecule_smiles", "")),
                target_smiles=refs.get("target", ""),
                query=refs["query"])).model_dump(mode="json")
        elif kind is TaskType.ROUTE_REASONING:
            service = self._factory(self._route_reasoning_factory, "route reasoning")
            if service is None:
                return TaskOutcome(status="failed", error="AI_PROVIDER_UNAVAILABLE")
            from crea.core.models.route_reasoning import RouteReasoningRequest

            content = service.reason(RouteReasoningRequest(
                start_molecule=refs["start"], target_molecule=refs["target"],
                query=refs["query"],
                max_steps=int(refs.get("max_steps", 3)))).model_dump(mode="json")
        elif kind is TaskType.AGENT_ANALYSIS:
            service = self._factory(self._agent_factory, "evidence agent")
            if service is None:
                return TaskOutcome(status="failed", error="AI_PROVIDER_UNAVAILABLE")
            content = service.run(refs["question"]).model_dump(mode="json")
        else:  # pragma: no cover - the enum is closed
            return TaskOutcome(status="failed", error=f"unknown task type {kind!r}")

        return TaskOutcome(status="completed", artifact_type=artifact,
                           content=content)

    @staticmethod
    def _factory(factory, what):
        if factory is None:
            return None
        return factory()


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v) for v in value]


def _inspect(smiles: str) -> dict:
    from crea.chem.functional_groups import detect_functional_groups
    from crea.chem.normalize import normalize

    molecule = normalize(smiles)
    d = molecule.descriptors
    return {"canonical_smiles": molecule.canonical_smiles,
            "inchikey": molecule.inchikey, "formula": d.molecular_formula,
            "molecular_weight": d.mol_wt, "rings": d.num_rings,
            "functional_groups": sorted({g.name for g in detect_functional_groups(molecule)})}


__all__ = ["ResearchTaskExecutor", "TaskOutcome"]

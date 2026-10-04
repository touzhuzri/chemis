"""AI context assembly (CREA Stage 6.1).

Gathers **already-known** facts -- molecules, matching reactions, their
evidence, and the literature that evidence rests on -- into one structured,
versioned `AIContext`. It assembles; it never reasons, summarises, or invents
(§1/§三十八). Facts are read through the existing services (the molecule
library, `TransformationSearchService`, `ReactionEvidenceService`, the graph
and route explorers); nothing is recomputed a second way.

The scope is explicit (§25): a molecule request loads the molecule, its
matching reactions, those reactions' evidence and that evidence's literature --
and only extends to a graph or routes when asked.
"""

from __future__ import annotations

from crea.core.errors import ContextIntegrityError
from crea.core.models.ai_context import (
    CONTEXT_VERSION,
    AIContext,
    AIContextRequest,
    ContextEvidence,
    ContextGraph,
    ContextLiterature,
    ContextMolecule,
    ContextReaction,
    ContextReactionMatch,
    ContextRoute,
    ContextRouteStep,
    ContextWarning,
)
from crea.core.models.reaction_evidence import EvidenceSourceType
from crea.core.provenance import utc_now_iso

#: A reaction whose product cannot be determined contributes no route step.
_PRODUCT_UNAVAILABLE = "PRODUCT_STRUCTURE_UNAVAILABLE"

_LIMITATIONS = [
    "this is an assembly of known facts, not an AI answer or a recommendation",
    "evidence indicates what a reaction template has behind it; it does not "
    "prove a specific route was run",
    "fulltext permission is independent of evidence: UNKNOWN permission does "
    "not invalidate a citation",
]


class AIContextService:
    """Build an `AIContext` from the local, deterministic chemistry data."""

    def __init__(self, kb, search_service=None, graph_service=None,
                 route_service=None, evidence_service=None) -> None:
        self._kb = kb
        if evidence_service is None:
            from crea.services.reaction_evidence import ReactionEvidenceService

            evidence_service = ReactionEvidenceService(kb=kb)
        self._evidence = evidence_service
        if search_service is None:
            from crea.services.transformation_search import TransformationSearchService

            search_service = TransformationSearchService(
                evidence_service=evidence_service)
        self._search = search_service
        self._graph = graph_service
        self._route = route_service

    # -- entry --------------------------------------------------------------

    def build(self, request: AIContextRequest) -> AIContext:
        warnings: list[ContextWarning] = []
        source_scope: dict = {
            "molecule_ids": list(request.molecule_ids),
            "molecule_smiles": list(request.molecule_smiles),
            "reaction_template_ids": list(request.reaction_template_ids),
            "include_graph": request.include_graph,
            "include_routes": request.include_routes}

        molecules = self._resolve_molecules(request, warnings)
        reactions, matches = self._collect_reactions(
            molecules, request, warnings)

        evidence, literature = self._collect_evidence(reactions, warnings)

        graphs = self._collect_graphs(molecules, request) if request.include_graph else []
        routes = self._collect_routes(molecules, request, warnings) \
            if request.include_routes else []

        context = AIContext(
            created_at=utc_now_iso(), query=request.query,
            source_scope=source_scope,
            # deterministic ordering (§27)
            molecules=sorted(molecules, key=lambda m: m.entity_id),
            reactions=sorted(reactions, key=lambda r: r.template_id),
            matches=sorted(matches, key=lambda m: m.template_id),
            evidence=sorted(evidence, key=lambda e: e.evidence_id),
            literature=sorted(literature, key=lambda l: l.paper_id),
            graphs=graphs, routes=routes,
            warnings=sorted(warnings, key=lambda w: (w.code, w.ref, w.message)),
            limitations=list(_LIMITATIONS))

        problems = self.check_integrity(context)
        if problems:
            raise ContextIntegrityError(
                "the AI context would be structurally incomplete", problems)
        return context

    def check_integrity(self, context: AIContext) -> list[str]:
        """Structural checks (§34): every reference must resolve in-context."""
        problems: list[str] = []
        paper_ids = {lit.paper_id for lit in context.literature}
        template_ids = {r.template_id for r in context.reactions}
        for ev in context.evidence:
            if ev.literature_id and ev.literature_id not in paper_ids:
                problems.append(
                    f"evidence {ev.evidence_id} cites literature "
                    f"{ev.literature_id!r} that is not in the context")
            if ev.reaction_template_id not in template_ids:
                problems.append(
                    f"evidence {ev.evidence_id} references reaction "
                    f"{ev.reaction_template_id!r} that is not in the context")
        for match in context.matches:
            if match.template_id not in template_ids:
                problems.append(
                    f"match references reaction {match.template_id!r} that is "
                    f"not in the context")
        return problems

    # -- molecules ----------------------------------------------------------

    def _resolve_molecules(self, request: AIContextRequest,
                           warnings: list[ContextWarning]) -> list[ContextMolecule]:
        entities = []
        seen: set[str] = set()
        for entity_id in request.molecule_ids[:request.max_molecules]:
            from crea.repositories.errors import NotFound

            try:
                entity = self._kb.get_entity(entity_id)
            except NotFound:
                warnings.append(ContextWarning(
                    code="MOLECULE_NOT_FOUND",
                    message=f"no molecule {entity_id!r}; skipped",
                    ref=entity_id))
                continue
            if entity.entity_id not in seen:
                seen.add(entity.entity_id)
                entities.append(entity)
        if request.molecule_smiles:
            for entity in self._resolve_smiles(request.molecule_smiles, warnings):
                if entity.entity_id not in seen and len(entities) < request.max_molecules:
                    seen.add(entity.entity_id)
                    entities.append(entity)
        return [self._molecule(entity) for entity in entities]

    def _resolve_smiles(self, smiles_list: list[str],
                        warnings: list[ContextWarning]):
        from crea.chem.normalize import NormalizationError
        from crea.semantic.resolver import ChemicalInput, SemanticResolver
        from crea.services.identity import IdentityResolutionService

        resolver = SemanticResolver(
            identity_service=IdentityResolutionService(),
            entity_registry=self._kb.entity_registry())
        out = []
        for smiles in smiles_list:
            try:
                result = resolver.resolve(ChemicalInput.of("SMILES", smiles))
            except (NormalizationError, ValueError) as exc:
                warnings.append(ContextWarning(
                    code="INVALID_MOLECULE", message=str(exc), ref=smiles))
                continue
            if result.entity is not None:
                out.append(result.entity)
            else:
                warnings.append(ContextWarning(
                    code="MOLECULE_UNRESOLVED",
                    message=f"{smiles!r} did not resolve to one entity",
                    ref=smiles))
        return out

    @staticmethod
    def _molecule(entity) -> ContextMolecule:
        from crea.chem.functional_groups import detect_functional_groups
        from crea.chem.normalize import NormalizationError, normalize
        from crea.chem.scaffold import murcko_scaffold_smiles

        formula = entity.molecular_formula or ""
        weight = entity.molecular_weight
        charge = 0
        atom_count = heavy = rings = 0
        fgs: list[str] = []
        scaffold = ""
        if entity.canonical_smiles:
            try:
                molecule = normalize(entity.canonical_smiles)
                d = molecule.descriptors
                formula = d.molecular_formula
                weight = d.mol_wt
                charge = d.net_charge
                atom_count = len(molecule.atoms)
                heavy = d.heavy_atom_count
                rings = d.num_rings
                fgs = sorted({g.name for g in detect_functional_groups(molecule)})
                scaffold = murcko_scaffold_smiles(molecule)
            except NormalizationError:
                pass
        names = list(entity.names)
        return ContextMolecule(
            entity_id=entity.entity_id, identity_key=entity.identity_key,
            standard_inchikey=entity.inchikey, smiles=entity.canonical_smiles,
            molecular_formula=formula, molecular_weight=weight,
            formal_charge=charge, atom_count=atom_count, heavy_atom_count=heavy,
            ring_count=rings, names=names,
            name_status="available" if names else "unavailable",
            functional_groups=fgs, scaffold=scaffold)

    # -- reactions ----------------------------------------------------------

    def _collect_reactions(self, molecules, request: AIContextRequest,
                           warnings: list[ContextWarning]):
        from crea.services.reaction_library import get_template

        template_ids: list[str] = list(request.reaction_template_ids)
        matches: list[ContextReactionMatch] = []
        if molecules:
            for molecule in molecules:
                if not molecule.smiles:
                    continue
                result = self._search.search(molecule.smiles)
                for candidate in result.candidates:
                    tid = candidate.reaction_template.template_id
                    template_ids.append(tid)
                    matches.append(ContextReactionMatch(
                        template_id=tid,
                        match_level=candidate.match_level.value,
                        matched_substrate_atoms=len(
                            candidate.matched_substrate_atoms)))

        reactions: list[ContextReaction] = []
        seen: set[str] = set()
        for template_id in template_ids[:request.max_reactions]:
            if template_id in seen:
                continue
            template = get_template(template_id)
            if template is None:
                warnings.append(ContextWarning(
                    code="REACTION_TEMPLATE_NOT_FOUND",
                    message=f"no reaction template {template_id!r}; skipped",
                    ref=template_id))
                continue
            seen.add(template_id)
            if not template.reaction_smarts:
                warnings.append(ContextWarning(
                    code=_PRODUCT_UNAVAILABLE,
                    message=f"product structure is unavailable for reaction "
                            f"template {template_id!r}",
                    ref=template_id))
            reactions.append(self._reaction(template))
        return reactions, matches

    def _reaction(self, template) -> ContextReaction:
        selectivity = {}
        if template.selectivity is not None:
            selectivity = {
                "regioselectivity": template.selectivity.regioselectivity,
                "stereoselectivity": template.selectivity.stereoselectivity,
                "chemoselectivity": template.selectivity.chemoselectivity}
        return ContextReaction(
            template_id=template.template_id, name=template.name,
            class_family=template.class_family.value,
            reaction_class=template.reaction_class,
            transformation=template.transformation.model_dump(mode="json"),
            mechanism_class=template.mechanism_class,
            reagents=[r.name for r in template.reagents if r.name],
            conditions=[c.value for c in template.conditions if c.value],
            selectivity=selectivity,
            limitations=[l.text for l in template.limitations if l.text],
            substrate_patterns=list(template.substrate_patterns),
            product_patterns=list(template.product_patterns),
            reaction_smarts=template.reaction_smarts,
            evidence_count=len(self._evidence.list_evidence(template.template_id)))

    # -- evidence + literature ----------------------------------------------

    def _collect_evidence(self, reactions: list[ContextReaction],
                          warnings: list[ContextWarning]):
        evidence: list[ContextEvidence] = []
        literature: list[ContextLiterature] = []
        seen_papers: set[str] = set()
        seen_evidence: set[str] = set()
        for reaction in reactions:
            for record in self._evidence.list_evidence(reaction.template_id):
                if record.evidence_id in seen_evidence:
                    continue
                seen_evidence.add(record.evidence_id)
                literature_id = ""
                if record.source.source_type is EvidenceSourceType.LITERATURE:
                    literature_id = record.source.source_id
                    if literature_id and literature_id not in seen_papers:
                        paper = self._kb.store.get_paper(literature_id)
                        if paper is not None:
                            seen_papers.add(literature_id)
                            literature.append(self._literature(paper))
                        # a missing paper is a dangling reference; integrity
                        # check below turns it into a ContextIntegrityError
                evidence.append(ContextEvidence(
                    evidence_id=record.evidence_id,
                    reaction_template_id=reaction.template_id,
                    evidence_type=record.evidence_type.value,
                    source_type=record.source.source_type.value,
                    support_scope=record.support_scope,
                    status=record.status.value,
                    literature_id=literature_id,
                    locator=(record.source.locator.model_dump(mode="json")
                             if record.source.locator else {})))
        # An evidence record whose literature is absent keeps its literature_id:
        # `check_integrity` then reports the dangling reference rather than the
        # context silently dropping it (§34).
        return evidence, literature

    @staticmethod
    def _literature(paper) -> ContextLiterature:
        abstract = paper.abstract
        return ContextLiterature(
            paper_id=paper.paper_id, title=paper.title or "",
            year=paper.year, journal=paper.journal or "", doi=paper.doi or "",
            oa_status=paper.access.oa_status.value,
            fulltext_permission=paper.access.permission.value,
            abstract=(abstract.text if abstract is not None else ""))

    # -- graph / routes -----------------------------------------------------

    def _collect_graphs(self, molecules, request: AIContextRequest):
        from crea.services.reaction_graph import ReactionGraphService

        service = self._graph or ReactionGraphService(self._kb)
        graphs = []
        for molecule in molecules:
            if not molecule.smiles:
                continue
            graph = service.molecule_graph(molecule.entity_id, depth=2)
            graphs.append(ContextGraph(
                root=graph.root,
                nodes=[n.model_dump(mode="json") for n in graph.nodes],
                edges=[e.model_dump(mode="json") for e in graph.edges]))
        return sorted(graphs, key=lambda g: g.root)

    def _collect_routes(self, molecules, request: AIContextRequest,
                        warnings: list[ContextWarning]):
        from crea.services.route_explorer import RouteExplorerService

        if not request.route_target_smiles:
            warnings.append(ContextWarning(
                code="ROUTE_TARGET_MISSING",
                message="include_routes=True but no route_target_smiles given"))
            return []
        service = self._route or RouteExplorerService(kb=self._kb)
        routes: list[ContextRoute] = []
        for molecule in molecules:
            if not molecule.smiles:
                continue
            result = service.explore(molecule.smiles, request.route_target_smiles)
            for path in result.paths:
                routes.append(ContextRoute(
                    route_id=path.route_id, status=path.status.value,
                    steps=[ContextRouteStep(
                        step_index=s.step_index,
                        reaction_template_id=s.reaction_template_id,
                        input_smiles=s.input_smiles, output_smiles=s.output_smiles)
                        for s in path.steps],
                    blocked_reason=path.blocked_reason))
        return sorted(routes, key=lambda r: (r.status, r.route_id))


__all__ = ["AIContextService", "CONTEXT_VERSION"]

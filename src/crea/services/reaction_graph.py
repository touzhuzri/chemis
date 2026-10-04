"""Reaction-graph service (CREA Stage 5.3).

Builds a transient graph from existing local data -- the molecule library, the
reaction templates, `ReactionEvidenceService` and the papers -- with a strict,
capped traversal. It reuses `TransformationSearchService` for structural
matching (never a second SMARTS path) and the Stage 4.4 reverse relation for
literature. No provider, no network, no AI, no route planning, no product
guessing (§9/§10/§27/§30).

Deterministic: the same query returns the same node and edge order (§33).
"""

from __future__ import annotations

from crea.core.errors import ReactionGraphError
from crea.core.models.reaction_evidence import EvidenceSourceType
from crea.core.models.reaction_graph import (
    GraphEdge,
    GraphEdgeType,
    GraphNode,
    GraphNodeType,
    ReactionGraph,
)

#: The traversal cap (§18). Bigger is refused, not silently obeyed.
MAX_DEPTH = 2


def _molecule_id(entity_id: str) -> str:
    return f"mol:{entity_id}"


def _reaction_id(template_id: str) -> str:
    return f"rxn:{template_id}"


def _literature_id(paper_id: str) -> str:
    return f"lit:{paper_id}"


class ReactionGraphService:
    """A deterministic, derived reaction graph over the local knowledge."""

    def __init__(self, kb, search_service=None) -> None:
        self._kb = kb
        if search_service is None:
            from crea.services.reaction_evidence import ReactionEvidenceService
            from crea.services.transformation_search import TransformationSearchService

            search_service = TransformationSearchService(
                evidence_service=ReactionEvidenceService(kb=kb))
        self._search = search_service

    # -- roots --------------------------------------------------------------

    def molecule_graph(self, entity_id: str, depth: int = 1) -> ReactionGraph:
        """Molecule -> matching ReactionTemplates -> (depth 2) Literature."""
        depth = self._clamp(depth)
        entity = self._get_entity(entity_id)
        root = _molecule_id(entity.entity_id)
        nodes: dict[str, GraphNode] = {root: self._molecule_node(entity)}
        edges: list[GraphEdge] = []
        diagnostics: list[str] = []

        smiles = entity.canonical_smiles
        if not smiles:
            diagnostics.append("molecule has no structure to match against")
            return self._finish(root, nodes, edges, depth, diagnostics)

        result = self._search.search(smiles)
        for candidate in result.candidates:
            template = candidate.reaction_template
            rid = _reaction_id(template.template_id)
            nodes[rid] = self._reaction_node(template, candidate.match_level.value,
                                             candidate.evidence_count)
            edges.append(GraphEdge(
                source=root, target=rid, type=GraphEdgeType.SUBSTRATE_OF,
                metadata={"match_level": candidate.match_level.value,
                          "matched_atoms": len(candidate.matched_substrate_atoms)}))
            if depth >= 2:
                self._add_literature(template.template_id, rid, nodes, edges)

        diagnostics.extend(self._product_diagnostic())
        return self._finish(root, nodes, edges, depth, diagnostics)

    def reaction_graph(self, template_id: str, depth: int = 1) -> ReactionGraph:
        """Reaction -> its literature evidence, and matching library molecules."""
        depth = self._clamp(depth)
        template = self._get_template(template_id)
        root = _reaction_id(template.template_id)
        nodes: dict[str, GraphNode] = {
            root: self._reaction_node(template, "", self._evidence_count(template.template_id))}
        edges: list[GraphEdge] = []

        self._add_literature(template.template_id, root, nodes, edges)

        if depth >= 2:
            for molecule in self._matching_library_molecules(template):
                mid = _molecule_id(molecule.entity_id)
                nodes[mid] = self._molecule_node(molecule)
                edges.append(GraphEdge(
                    source=mid, target=root, type=GraphEdgeType.SUBSTRATE_OF,
                    metadata={"match_level": "substrate_match"}))

        diagnostics = self._product_diagnostic()
        return self._finish(root, nodes, edges, depth, diagnostics)

    def literature_graph(self, paper_id: str, depth: int = 1) -> ReactionGraph:
        """Literature -> the reactions whose evidence cites it (Stage 4.4)."""
        depth = self._clamp(depth)
        paper = self._kb.store.get_paper(paper_id)
        if paper is None:
            raise ReactionGraphError(
                "LITERATURE_NOT_FOUND", f"no local literature record {paper_id!r}")
        root = _literature_id(paper.paper_id)
        nodes: dict[str, GraphNode] = {root: self._literature_node(paper)}
        edges: list[GraphEdge] = []

        from crea.services.reaction_evidence import ReactionEvidenceService

        evidence = ReactionEvidenceService(kb=self._kb)
        for record in evidence.evidence_for_literature(paper.paper_id):
            template = self._get_template(record.reaction_template_id)
            rid = _reaction_id(template.template_id)
            nodes[rid] = self._reaction_node(
                template, "", self._evidence_count(template.template_id))
            edges.append(GraphEdge(
                source=rid, target=root, type=GraphEdgeType.SUPPORTED_BY,
                metadata=self._evidence_metadata(record)))

        return self._finish(root, nodes, edges, depth, [])

    # -- pieces -------------------------------------------------------------

    @staticmethod
    def _clamp(depth: int) -> int:
        try:
            value = int(depth)
        except (TypeError, ValueError):
            return 1
        return max(1, min(value, MAX_DEPTH))

    def _get_entity(self, entity_id: str):
        from crea.repositories.errors import NotFound

        try:
            return self._kb.get_entity(entity_id)
        except NotFound as exc:
            raise ReactionGraphError(
                "MOLECULE_NOT_FOUND", f"no molecule {entity_id!r}") from exc

    @staticmethod
    def _get_template(template_id: str):
        from crea.services.reaction_library import get_template

        template = get_template(template_id)
        if template is None:
            raise ReactionGraphError(
                "TEMPLATE_NOT_FOUND", f"no reaction template {template_id!r}")
        return template

    def _evidence_count(self, template_id: str) -> int:
        from crea.services.reaction_evidence import ReactionEvidenceService

        return len(ReactionEvidenceService(kb=self._kb).list_evidence(template_id))

    def _add_literature(self, template_id: str, reaction_node: str,
                        nodes: dict, edges: list) -> None:
        from crea.services.reaction_evidence import ReactionEvidenceService

        evidence = ReactionEvidenceService(kb=self._kb)
        for record in evidence.list_evidence(template_id):
            if record.source.source_type is not EvidenceSourceType.LITERATURE:
                continue
            paper_id = record.source.source_id
            paper = self._kb.store.get_paper(paper_id) if paper_id else None
            lid = _literature_id(paper_id)
            nodes[lid] = self._literature_node(paper) if paper is not None \
                else GraphNode(id=lid, type=GraphNodeType.LITERATURE,
                               label=record.source.title or paper_id,
                               metadata={"paper_id": paper_id, "title": "",
                                         "year": None, "journal": "", "doi": ""})
            edges.append(GraphEdge(
                source=reaction_node, target=lid,
                type=GraphEdgeType.SUPPORTED_BY,
                metadata=self._evidence_metadata(record)))

    def _matching_library_molecules(self, template, cap: int = 25) -> list:
        """Local entities whose structure matches this template's substrate."""
        from crea.chem.functional_groups import matches_smarts
        from crea.chem.normalize import NormalizationError, normalize

        out = []
        for entity in self._kb.list_entities():
            if len(out) >= cap or not entity.canonical_smiles:
                continue
            try:
                molecule = normalize(entity.canonical_smiles)
            except NormalizationError:
                continue
            if any(matches_smarts(molecule, p)
                   for p in template.substrate_patterns):
                out.append(entity)
        return out

    # -- nodes --------------------------------------------------------------

    @staticmethod
    def _molecule_node(entity) -> GraphNode:
        label = (entity.names[0] if entity.names else
                 entity.canonical_smiles or entity.inchikey)
        return GraphNode(
            id=_molecule_id(entity.entity_id), type=GraphNodeType.MOLECULE,
            label=label,
            metadata={"entity_id": entity.entity_id,
                      "canonical_smiles": entity.canonical_smiles,
                      "inchikey": entity.inchikey,
                      "names": list(entity.names),
                      "identity_source": entity.identity_source})

    @staticmethod
    def _reaction_node(template, match_level: str, evidence_count: int) -> GraphNode:
        return GraphNode(
            id=_reaction_id(template.template_id), type=GraphNodeType.REACTION,
            label=template.name,
            metadata={"template_id": template.template_id,
                      "name": template.name,
                      "reaction_class": template.reaction_class,
                      "class_family": template.class_family.value,
                      "mechanism_class": template.mechanism_class,
                      "match_level": match_level,
                      "evidence_count": evidence_count})

    @staticmethod
    def _literature_node(paper) -> GraphNode:
        if paper is None:
            return GraphNode(id=_literature_id(""), type=GraphNodeType.LITERATURE)
        return GraphNode(
            id=_literature_id(paper.paper_id), type=GraphNodeType.LITERATURE,
            label=paper.title or paper.paper_id,
            metadata={"paper_id": paper.paper_id, "title": paper.title or "",
                      "year": paper.year, "journal": paper.journal or "",
                      "doi": paper.doi or ""})

    @staticmethod
    def _evidence_metadata(record) -> dict:
        locator = record.source.locator.model_dump(mode="json") \
            if record.source.locator else {}
        return {"evidence_id": record.evidence_id,
                "evidence_type": record.evidence_type.value,
                "support_scope": record.support_scope,
                "status": record.status.value,
                "locator": locator}

    @staticmethod
    def _product_diagnostic() -> list[str]:
        return ["product structure unavailable: no product pattern is encoded, "
                "so no Reaction -> Molecule (produces) edge is created"]

    @staticmethod
    def _finish(root: str, nodes: dict, edges: list, depth: int,
                diagnostics: list) -> ReactionGraph:
        return ReactionGraph(
            root=root,
            # deterministic order (§33): by node id, then edge (source,target,type)
            nodes=[nodes[k] for k in sorted(nodes)],
            edges=sorted(edges, key=lambda e: (e.source, e.target, e.type.value)),
            depth=depth, diagnostics=list(diagnostics))


__all__ = ["MAX_DEPTH", "ReactionGraphService"]

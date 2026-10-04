"""The evidence-aware agent's tools (CREA Stage 6.4).

Every tool is **read-only** (§八) and delegates to a deterministic service --
structural search, comparison, evidence, literature, routes. None of them
writes to the database, reads the filesystem, runs SQL, or reaches the network
directly: the literature tool goes through the `LiteratureService`, never a
provider. Outputs are structured dicts with bounded sizes (§十八/§四十八).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from crea.agent.tools import ToolRegistry, ToolSpec
from crea.core.models.agent_run import ToolPermission

#: Literature results are bounded, hard (§四十八). A search returns at most this
#: many papers regardless of what the model asks for.
MAX_LITERATURE_RESULTS = 10


class _InspectMolecule(BaseModel):
    smiles: str = Field(min_length=1, description="a structure notation")


class _SearchTransformations(BaseModel):
    substrate: str = Field(min_length=1)
    target: str = Field(default="", description="optional target SMILES")


class _GetReaction(BaseModel):
    reaction_template_id: str = Field(min_length=1)


class _CompareReactions(BaseModel):
    reaction_template_ids: list[str] = Field(min_length=1)
    substrate: str = ""
    target: str = ""


class _ReactionEvidence(BaseModel):
    reaction_template_id: str = Field(min_length=1)


class _LiteratureForPaper(BaseModel):
    paper_id: str = Field(min_length=1)


class _SearchLiterature(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=MAX_LITERATURE_RESULTS)


class _RouteSearch(BaseModel):
    start: str = Field(min_length=1)
    target: str = Field(min_length=1)
    max_steps: int = Field(default=3, ge=1, le=3)


def build_evidence_tool_registry(*, transformation_search, comparison,
                                 evidence, graph=None, route=None,
                                 literature=None) -> ToolRegistry:
    """The read-only toolset the evidence-aware agent may call (§七).

    A missing backend does not remove a tool -- the tool reports `unavailable`
    honestly rather than the agent silently losing a capability.
    """
    registry = ToolRegistry()
    registry.evidence = evidence
    registry.literature = literature

    registry.register(ToolSpec(
        name="inspect_molecule", permission=ToolPermission.READ_ONLY.value,
        description="Normalise a SMILES and report formula, weight, rings, "
                    "functional groups and scaffold. Computed, not looked up.",
        arguments_model=_InspectMolecule,
        run=lambda smiles: _inspect(smiles)))

    registry.register(ToolSpec(
        name="search_transformations", permission=ToolPermission.READ_ONLY.value,
        description="Find existing reaction templates whose substrate pattern "
                    "matches the substrate (and product pattern the target). "
                    "A match is a structural statement, never experimental "
                    "success.",
        arguments_model=_SearchTransformations,
        run=lambda substrate, target="": _transformations(
            transformation_search, substrate, target)))

    registry.register(ToolSpec(
        name="get_reaction_template", permission=ToolPermission.READ_ONLY.value,
        description="Read one reaction template: class, transformation, "
                    "reagents, conditions, mechanism, selectivity, limitations.",
        arguments_model=_GetReaction,
        run=lambda reaction_template_id: _get_reaction(reaction_template_id)))

    registry.register(ToolSpec(
        name="compare_reactions", permission=ToolPermission.READ_ONLY.value,
        description="Compare reaction templates on their known facts. Facts "
                    "only -- no best/recommended route, no success prediction.",
        arguments_model=_CompareReactions,
        run=lambda reaction_template_ids, substrate="", target="": _compare(
            comparison, list(reaction_template_ids), substrate, target)))

    registry.register(ToolSpec(
        name="reaction_evidence", permission=ToolPermission.READ_ONLY.value,
        description="The evidence standing behind a reaction template, "
                    "including literature-linked evidence and its support scope.",
        arguments_model=_ReactionEvidence,
        run=lambda reaction_template_id: _evidence_for(
            evidence, reaction_template_id)))

    registry.register(ToolSpec(
        name="literature_for_paper", permission=ToolPermission.READ_ONLY.value,
        description="Which reaction evidence cites a stored paper (the reverse "
                    "relation). Never treats a search hit as support.",
        arguments_model=_LiteratureForPaper,
        run=lambda paper_id: _evidence_for_paper(evidence, paper_id)))

    registry.register(ToolSpec(
        name="search_literature", permission=ToolPermission.READ_ONLY.value,
        description="Search literature METADATA (Crossref/OpenAlex). A hit is "
                    "discovery, not evidence: it does not mean the work supports "
                    "any reaction. Returns at most 10 records.",
        arguments_model=_SearchLiterature,
        run=lambda query, limit=5: _search_literature(literature, query, limit)))

    registry.register(ToolSpec(
        name="route_search", permission=ToolPermission.READ_ONLY.value,
        description="Deterministic forward routes from start to target over the "
                    "known reactions. A partial route is not complete; no route "
                    "is invented.",
        arguments_model=_RouteSearch,
        run=lambda start, target, max_steps=3: _route_search(
            route, start, target, max_steps)))

    return registry


# --- bodies ----------------------------------------------------------------


def _inspect(smiles: str) -> dict:
    from crea.chem.functional_groups import detect_functional_groups
    from crea.chem.normalize import normalize
    from crea.chem.scaffold import murcko_scaffold_smiles

    molecule = normalize(smiles)
    d = molecule.descriptors
    return {
        "status": "ok",
        "canonical_smiles": molecule.canonical_smiles,
        "inchikey": molecule.inchikey,
        "formula": d.molecular_formula,
        "molecular_weight": round(d.mol_wt, 4),
        "rings": d.num_rings,
        "functional_groups": sorted({g.name for g in detect_functional_groups(molecule)}),
        "scaffold": murcko_scaffold_smiles(molecule),
    }


def _transformations(service, substrate: str, target: str) -> dict:
    result = service.search(substrate, target)
    return {
        "status": "ok",
        "count": result.count,
        "candidates": [
            {"reaction_template_id": c.reaction_template.template_id,
             "name": c.reaction_template.name,
             "match_level": c.match_level.value,
             "transformation": c.transformation.note,
             "evidence_count": c.evidence_count}
            for c in result.candidates],
    }


def _get_reaction(template_id: str) -> dict:
    from crea.services.reaction_library import get_template

    template = get_template(template_id)
    if template is None:
        return {"status": "not_found", "reaction_template_id": template_id}
    return {
        "status": "ok",
        "reaction_template_id": template.template_id,
        "name": template.name,
        "class_family": template.class_family.value,
        "reaction_class": template.reaction_class,
        "transformation": template.transformation.note,
        "mechanism_class": template.mechanism_class,
        "reagents": [r.name for r in template.reagents if r.name],
        "conditions": [c.value for c in template.conditions if c.value],
        "limitations": [l.text for l in template.limitations if l.text],
    }


def _compare(service, ids: list[str], substrate: str, target: str) -> dict:
    result = service.compare(ids, substrate=substrate, target=target)
    return {
        "status": "ok",
        "entries": [
            {"reaction_template_id": e.template_id, "name": e.name,
             "substrate_compatibility": e.substrate_compatibility,
             "reagents": e.reagents, "conditions": e.conditions,
             "mechanism_class": e.mechanism_class,
             "limitations": e.limitations, "evidence_count": e.evidence_count,
             "literature_count": len(e.literature)}
            for e in result.entries],
    }


def _evidence_for(service, template_id: str) -> dict:
    records = service.list_evidence(template_id)
    return {
        "status": "ok",
        "reaction_template_id": template_id,
        "count": len(records),
        "evidence": [
            {"evidence_id": r.evidence_id, "evidence_type": r.evidence_type.value,
             "source_type": r.source.source_type.value, "status": r.status.value,
             "support_scope": r.support_scope,
             "literature_id": (r.source.source_id
                               if r.source.source_type.value == "literature" else "")}
            for r in records],
    }


def _evidence_for_paper(service, paper_id: str) -> dict:
    records = service.evidence_for_literature(paper_id)
    return {
        "status": "ok",
        "paper_id": paper_id,
        "count": len(records),
        "evidence": [
            {"evidence_id": r.evidence_id,
             "reaction_template_id": r.reaction_template_id,
             "evidence_type": r.evidence_type.value,
             "support_scope": r.support_scope}
            for r in records],
    }


def _search_literature(service, query: str, limit: int) -> dict:
    if service is None:
        return {"status": "unavailable",
                "reason": "no literature service is configured"}
    from crea.core.models.service import LiteratureSearchRequest

    result = service.search(LiteratureSearchRequest(
        query=query, limit=min(limit, MAX_LITERATURE_RESULTS)))
    return {
        "status": "ok",
        "outcome": result.outcome.value,
        "count": len(result.papers),
        "papers": [
            {"paper_id": p.paper_id, "doi": p.doi, "title": p.title,
             "year": p.year, "journal": p.journal}
            for p in result.papers[:MAX_LITERATURE_RESULTS]],
        "note": "metadata is discovery, not evidence; a hit does not mean the "
                "work supports any reaction",
    }


def _route_search(service, start: str, target: str, max_steps: int) -> dict:
    if service is None:
        return {"status": "unavailable", "reason": "no route explorer is configured"}
    result = service.explore(start, target, max_steps)
    return {
        "status": "ok",
        "complete": [{"route_id": p.route_id, "steps": len(p.steps)}
                     for p in result.paths if p.status.value == "complete"],
        "partial": [{"route_id": p.route_id, "steps": len(p.steps),
                     "blocked_reason": p.blocked_reason}
                    for p in result.paths if p.status.value == "partial"],
        "blocked": [{"route_id": p.route_id, "blocked_reason": p.blocked_reason}
                    for p in result.paths if p.status.value == "blocked"],
        "diagnostics": list(result.diagnostics),
    }


__all__ = ["MAX_LITERATURE_RESULTS", "build_evidence_tool_registry"]

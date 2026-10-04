"""The representative Chemistry Tools: thin adapters over real capabilities.

CREA 4.0 Stage C (§7-§10). Eight tools, each a boundary:

    resolve_chemical     -> SemanticResolver            (the Stage B-1 spine)
    name_structure       -> NameResolver provider chain (OPSIN worker, PubChem)
    analyze_structure    -> crea.chem (RDKit descriptors/scaffold/fragments)
    compare_structures   -> IdentityFacets + similarity + scaffold
    search_chemical      -> providers.pubchem (canonical entry, §11)
    search_literature    -> LiteratureService (fail-closed policy intact)
    search_reactions     -> the local reaction knowledge search (Stage D TODO)
    retrosynthesize      -> RetrosynthesisProvider (AiZynthFinder worker)

None of these computes chemistry of its own: no RDKit here, no name ->
structure here, no SQL here. A tool that cannot reach its backend reports
`unavailable` with diagnostics -- never a fabricated success.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, Field

from crea.tools.contract import (
    ChemistryTool,
    ExecutionMode,
    ToolError,
    ToolErrorCode,
)
from crea.tools.execution import (
    InProcessExecutor,
    IsolatedWorkerExecutor,
    RemoteProviderExecutor,
)


# ---------------------------------------------------------------------------
# argument schemas (the declared input surface, §3)
# ---------------------------------------------------------------------------


class ResolveChemicalArgs(BaseModel):
    kind: str = Field(description="SMILES | INCHI | INCHIKEY | PUBCHEM_CID "
                                  "| NAME | MOLBLOCK")
    value: str = Field(min_length=1)


class NameStructureArgs(BaseModel):
    name: str = Field(min_length=1)


class AnalyzeStructureArgs(BaseModel):
    smiles: str = Field(min_length=1)


class CompareStructuresArgs(BaseModel):
    smiles_a: str = Field(min_length=1)
    smiles_b: str = Field(min_length=1)


class SearchChemicalArgs(BaseModel):
    query: str = Field(min_length=1)
    kind: str = Field(default="name",
                      description="name | identifier | smiles | inchikey")


class SearchLiteratureArgs(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=100)


class SearchReactionsArgs(BaseModel):
    entity_id: str | None = None
    product_entity_id: str | None = None
    reactant_entity_id: str | None = None
    paper_id: str | None = None
    structure: str | None = None


class RetrosynthesizeArgs(BaseModel):
    target_smiles: str = Field(min_length=1)
    max_routes: int = Field(default=3, ge=1, le=25)


class ExploreChemicalSpaceArgs(BaseModel):
    entity_id: str | None = None
    chemical: str | None = Field(
        default=None, description="a chemical NAME, resolved through the "
                                  "name chain; never a guessed structure")
    smiles: str | None = Field(
        default=None, description="an explicit structure notation")
    max_candidates: int = Field(default=10, ge=1, le=50)


class FindChemicalTransformationsArgs(BaseModel):
    candidate: str | None = Field(
        default=None, description="the candidate entity id (reactant side)")
    target: str | None = Field(
        default=None, description="the target entity id (product side)")
    reactant_entity_ids: list[str] = Field(default_factory=list)
    product_entity_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool 1 -- resolve_chemical (§7, the core tool)
# ---------------------------------------------------------------------------


class ResolveChemicalTool(ChemistryTool):
    name = "resolve_chemical"
    description = (
        "Resolve any chemical input (structure notation, name, identifier) "
        "to ONE ChemicalEntity: status + identity evidence level + "
        "representations + provider provenance. Ambiguity is reported, "
        "never resolved by picking a candidate.")
    capabilities = ("chemical_identity", "entity_resolution",
                    "representations", "provenance")
    execution_modes = (ExecutionMode.IN_PROCESS,)
    arguments_model = ResolveChemicalArgs

    def __init__(self, resolver=None) -> None:
        self._resolver = resolver
        self._run = InProcessExecutor()

    def execute(self, args: ResolveChemicalArgs):
        if self._resolver is None:
            raise ToolError(ToolErrorCode.UNAVAILABLE,
                            "no semantic resolver is wired")
        from crea.semantic.resolver import ChemicalInput

        kind = args.kind.strip().upper()
        if kind == "MOLBLOCK":
            chemical_input = ChemicalInput.of_notation("molblock", args.value)
        else:
            chemical_input = ChemicalInput.of(kind, args.value)
        result = self._run.run(self._resolver.resolve, chemical_input)
        entity = result.entity
        return {
            "status": result.status.value,
            "identity_evidence": result.identity_evidence.value,
            "entity": (entity.model_dump(mode="json")
                       if entity is not None else None),
            "candidates": [c.model_dump(mode="json")
                           for c in result.candidates],
            "provider_results": [p.model_dump(mode="json")
                                 for p in result.provider_results],
            "diagnostics": list(result.diagnostics),
        }, {"diagnostics": list(result.diagnostics),
            "provenance": {"providers": [p.provider for p in
                                         result.provider_results]}}


# ---------------------------------------------------------------------------
# Tool 2 -- name_structure (§: name -> candidate structures)
# ---------------------------------------------------------------------------


class NameStructureTool(ChemistryTool):
    name = "name_structure"
    description = (
        "Look a chemical NAME up in the provider chain (OPSIN for "
        "systematic nomenclature, PubChem; CIR opt-in and disabled by "
        "default pending service terms). Returns candidates with their "
        "sources -- never a chosen structure.")
    capabilities = ("name_resolution", "opsin_worker", "pubchem")
    execution_modes = (ExecutionMode.IN_PROCESS, ExecutionMode.ISOLATED_WORKER,
                       ExecutionMode.REMOTE_PROVIDER)
    arguments_model = NameStructureArgs
    provenance_policy = "every candidate names the provider it came from"

    def __init__(self, name_resolver=None) -> None:
        self._resolver = name_resolver
        self._process = InProcessExecutor()

    def execute(self, args: NameStructureArgs):
        if self._resolver is None:
            raise ToolError(ToolErrorCode.UNAVAILABLE,
                            "no name resolver is wired")
        answer = self._process.run(self._resolver.resolve_name, args.name)
        return {
            "status": answer.status.value,
            "identity_evidence": answer.identity_evidence.value,
            "candidates": [c.model_dump(mode="json")
                           for c in answer.candidates],
            "selected": (answer.selected.model_dump(mode="json")
                         if answer.selected else None),
            "providers": list(answer.provider_results),
            "providers_enabled": list(self._resolver.provider_names),
            "providers_disabled": ["nci_cir: opt-in, ToS pending"],
            "diagnostics": list(answer.diagnostics),
        }, {"diagnostics": list(answer.diagnostics),
            "provenance": {"providers": list(self._resolver.provider_names)}}


# ---------------------------------------------------------------------------
# Tool 3 -- analyze_structure (§7: RDKit, no re-implementation)
# ---------------------------------------------------------------------------


class AnalyzeStructureTool(ChemistryTool):
    name = "analyze_structure"
    description = (
        "Computed analysis of a structure: formula, weight, rings, HBD/HBA, "
        "TPSA, logP, scaffold, fragments and fingerprint metadata. "
        "Everything here is computed, not literature evidence.")
    capabilities = ("descriptors", "scaffold", "fragments", "fingerprints")
    execution_modes = (ExecutionMode.IN_PROCESS,)
    arguments_model = AnalyzeStructureArgs

    def __init__(self) -> None:
        self._run = InProcessExecutor()

    def execute(self, args: AnalyzeStructureArgs):
        from crea.chem.fingerprint import compute_fingerprints
        from crea.chem.fragment import decompose_molecule
        from crea.chem.normalize import normalize, rebuild
        from crea.chem.scaffold import murcko_scaffold_smiles

        molecule = self._run.run(normalize, args.smiles)
        d = molecule.descriptors
        fingerprints = compute_fingerprints(rebuild(molecule))
        fragments = decompose_molecule(molecule, "ring_linker")
        return {
            "note": "以下为计算结果，不是文献证据",
            "canonical_smiles": molecule.canonical_smiles,
            "inchi": molecule.inchi,
            "inchikey": molecule.inchikey,
            "descriptors": {
                "molecular_formula": d.molecular_formula,
                "mol_wt": d.mol_wt, "exact_mol_wt": d.exact_mol_wt,
                "heavy_atom_count": d.heavy_atom_count,
                "num_rings": d.num_rings,
                "num_aromatic_rings": d.num_aromatic_rings,
                "tpsa": d.tpsa, "logp": d.logp,
                "hbd": d.hbd, "hba": d.hba,
                "rotatable_bonds": d.rotatable_bonds,
                "fraction_csp3": d.fraction_csp3,
                "net_charge": d.net_charge,
            },
            "scaffold": {"murcko_smiles": murcko_scaffold_smiles(molecule)},
            "fragments": [
                {"fragment_id": f.fragment_id,
                 "fragment_smiles": f.fragment_smiles,
                 "attachment_smiles": f.with_attachment_smiles,
                 "n_attachment_points": f.n_attachment_points,
                 "formula": f.formula}
                for f in fragments[:20]],
            "fingerprint_metadata": {
                "note": "bit vectors are caching artefacts, never labels",
                "vectors": sorted(fingerprints.model_dump().keys()),
            },
        }, {"provenance": {"backend": "rdkit",
                           "toolkit": molecule.rdkit_version}}


# ---------------------------------------------------------------------------
# Tool 4 -- compare_structures (§7: identity != similarity)
# ---------------------------------------------------------------------------


class CompareStructuresTool(ChemistryTool):
    name = "compare_structures"
    description = (
        "Compare two structures on FOUR separate axes: identity "
        "(IdentityFacets), fingerprint similarity (screening only), MCS "
        "alignment, and scaffold relationship. Similarity is never "
        "identity; the output says so.")
    capabilities = ("identity_facets", "similarity_screen", "mcs",
                    "scaffold")
    execution_modes = (ExecutionMode.IN_PROCESS,)
    arguments_model = CompareStructuresArgs

    def __init__(self) -> None:
        self._run = InProcessExecutor()

    def execute(self, args: CompareStructuresArgs):
        from crea.chem.fingerprint import screen_similarity
        from crea.chem.normalize import normalize
        from crea.chem.scaffold import compare_scaffolds

        mol_a = self._run.run(normalize, args.smiles_a)
        mol_b = self._run.run(normalize, args.smiles_b)

        # identity: the ONE comparison system (§5)
        from crea.semantic.identity import facets_for

        facets = facets_for(mol_a.inchi, mol_b.inchi)
        similarity = screen_similarity(mol_a, mol_b)
        scaffold = compare_scaffolds(mol_a, mol_b)
        return {
            "identity": {
                "basis": facets.basis().value,
                "facets": facets.model_dump(mode="json"),
                "same_entity_identity": facets.basis().value == "exact_structure",
            },
            "similarity": {
                "screening_only": True,
                "scores": similarity.model_dump(mode="json"),
                "note": "similarity is a screening signal, never identity "
                        "and never synthetic feasibility",
            },
            "scaffold": scaffold.model_dump(mode="json"),
        }


# ---------------------------------------------------------------------------
# Tool 5 -- search_chemical (§: unified external chemistry search)
# ---------------------------------------------------------------------------


class SearchChemicalTool(ChemistryTool):
    name = "search_chemical"
    description = (
        "Search an external compound source (PubChem, the canonical "
        "integration) by name, identifier, SMILES or InChIKey. Records "
        "come back with their source stamps; nothing is ranked and "
        "nothing is chosen.")
    capabilities = ("pubchem", "compound_lookup")
    execution_modes = (ExecutionMode.REMOTE_PROVIDER,)
    arguments_model = SearchChemicalArgs

    def __init__(self, provider=None) -> None:
        self._provider = provider
        self._remote = RemoteProviderExecutor(provider) if provider else None

    def execute(self, args: SearchChemicalArgs):
        if self._provider is None:
            raise ToolError(ToolErrorCode.UNAVAILABLE,
                            "no compound provider is wired")
        from crea.providers.base import CompoundQuery

        kind = args.kind.strip().lower()
        query = {
            "name": CompoundQuery(name=args.query),
            "smiles": CompoundQuery(smiles=args.query),
            "inchikey": CompoundQuery(inchikey=args.query),
            "identifier": CompoundQuery(identifier=args.query),
        }.get(kind)
        if query is None:
            raise ToolError(ToolErrorCode.INVALID_INPUT,
                            f"unknown search kind {args.kind!r}")
        records = self._remote.call(self._provider.lookup, query)
        return {
            "count": len(records),
            "candidates": [c.model_dump(mode="json") for c in records],
        }, {"provenance": {"provider": self._remote.provider_name},
            "diagnostics": ([f"{self._remote.provider_name} holds no entry"]
                            if not records else [])}


# ---------------------------------------------------------------------------
# Tool 6 -- search_literature (§8: policy unchanged)
# ---------------------------------------------------------------------------


class SearchLiteratureTool(ChemistryTool):
    name = "search_literature"
    description = (
        "Search the literature providers (Crossref/OpenAlex/Unpaywall). "
        "Metadata is NOT evidence and NOT fulltext permission: every "
        "result keeps its source, DOI, OA status and permission fields.")
    capabilities = ("literature", "openalex", "crossref", "unpaywall")
    execution_modes = (ExecutionMode.REMOTE_PROVIDER,)
    arguments_model = SearchLiteratureArgs
    provenance_policy = (
        "each paper keeps source/DOI/OA/permission; fail-closed access")

    def __init__(self, literature=None) -> None:
        self._literature = literature

    def execute(self, args: SearchLiteratureArgs):
        if self._literature is None:
            raise ToolError(ToolErrorCode.UNAVAILABLE,
                            "no literature service is wired")
        from crea.services.literature import LiteratureSearchRequest

        result = self._literature.search(
            LiteratureSearchRequest(query=args.query, limit=args.limit))
        papers = []
        for paper in result.papers:
            papers.append({
                "paper_id": paper.paper_id, "doi": paper.doi,
                "title": paper.title, "year": paper.year,
                "oa_status": paper.access.oa_status.value,
                "fulltext_permission": paper.access.permission.value,
                "sources": list(paper.sources),
            })
        return {
            "outcome": result.outcome.value,
            "count": len(papers),
            "papers": papers,
            "note": "metadata is discovery, never evidence (fail-closed "
                    "access policy applies)",
        }, {"provenance": {"providers": [r.name for r in result.provider_runs]},
            "diagnostics": list(result.warnings or [])}


# ---------------------------------------------------------------------------
# Tool 7 -- search_reactions (§9: adapter only; Stage D matures it)
# ---------------------------------------------------------------------------


class SearchReactionsTool(ChemistryTool):
    name = "search_reactions"
    description = (
        "Search CREA's reaction knowledge: which known reactions consume "
        "or produce a ChemicalEntity. Returns ReactionSearchResult entries "
        "-- the reaction, the entities the query matched and in which "
        "role, its evidence and provenance -- so the answer says WHY it is "
        "the answer (Stage D). Scope: established reactions; cross-"
        "literature reaction discovery is future work.")
    capabilities = ("reaction_knowledge_search", "find_by_product",
                    "find_by_reactant")
    execution_modes = (ExecutionMode.IN_PROCESS,)
    arguments_model = SearchReactionsArgs
    provenance_policy = (
        "results carry the reaction's own evidence and provenance; "
        "diagnostics say what is missing (no atom mapping, unresolved parts)")

    def __init__(self, reactions=None) -> None:
        self._reactions = reactions

    def execute(self, args: SearchReactionsArgs):
        if self._reactions is None:
            raise ToolError(ToolErrorCode.UNAVAILABLE,
                            "no reaction knowledge source is wired")
        if not any([args.entity_id, args.product_entity_id,
                    args.reactant_entity_id, args.paper_id, args.structure]):
            raise ToolError(ToolErrorCode.INVALID_INPUT,
                            "at least one filter is required")
        try:
            results = self._reactions.search_results(
                entity_id=args.entity_id,
                product_entity_id=args.product_entity_id,
                reactant_entity_id=args.reactant_entity_id,
                paper_id=args.paper_id, structure=args.structure)
        except ValueError as exc:
            raise ToolError(ToolErrorCode.INVALID_INPUT, str(exc)) from exc
        payload = [
            {
                "reaction": r.reaction.model_dump(mode="json"),
                "matched_entities": list(r.matched_entities),
                "match_role": r.match_role,
                "evidence": list(r.evidence),
                "provenance": r.provenance,
                "diagnostics": list(r.diagnostics),
            }
            for r in results
        ]
        return {
            "count": len(payload),
            "results": payload,
            "scope": "established_reaction_knowledge",
        }, {"diagnostics": ([] if payload else ["no known reaction matches"]),
            "provenance": {"source": "reaction_registry"}}


# ---------------------------------------------------------------------------
# Tool 8 -- retrosynthesize (§10: contract + normalization only)
# ---------------------------------------------------------------------------


class RetrosynthesizeTool(ChemistryTool):
    name = "retrosynthesize"
    description = (
        "Candidate routes from a retrosynthesis engine (AiZynthFinder "
        "worker). Returns CANDIDATES with engine provenance -- never "
        "validated routes, never ranked as synthetic feasibility.")
    capabilities = ("retrosynthesis", "aizynthfinder_worker")
    execution_modes = (ExecutionMode.ISOLATED_WORKER,)
    arguments_model = RetrosynthesizeArgs
    provenance_policy = (
        "routes carry the engine's own score_source; candidates only")

    def __init__(self, provider=None) -> None:
        self._provider = provider

    def execute(self, args: RetrosynthesizeArgs):
        if self._provider is None:
            raise ToolError(ToolErrorCode.UNAVAILABLE,
                            "no retrosynthesis provider is wired")
        from crea.core.models.route import RetrosynthesisRequest

        result = self._provider.retrosynthesize(
            RetrosynthesisRequest(target_smiles=args.target_smiles,
                                  max_routes=args.max_routes))
        return {
            "available": result.available,
            "status": result.status.value,
            "reason": result.reason,
            "reason_code": result.reason_code.value,
            "routes": [
                {"route_id": r.route_id,
                 "step_count": len(r.steps),
                 "score": r.score, "score_source": r.score_source.value,
                 "note": "candidate route -- not a validated synthesis"}
                for r in result.routes],
            "provenance": result.provenance.model_dump(mode="json"),
        }, {"provenance": {"provider": getattr(self._provider, "name",
                                               "retrosynthesis")},
            "diagnostics": ([result.reason] if result.reason else [])}


# ---------------------------------------------------------------------------
# Tool 9 -- explore_chemical_space (§16/§17/§18)
# ---------------------------------------------------------------------------


class ExploreChemicalSpaceTool(ChemistryTool):
    name = "explore_chemical_space"
    description = (
        "Explore the chemical space around a target: candidate structures "
        "from a chemical space, each resolved to a ChemicalEntity with its "
        "structural relationship (similarity / same scaffold / fragment "
        "related) and its reaction evidence (direct / related / none / "
        "unavailable). Structure exploration ONLY -- not retrosynthesis, not "
        "a route, not a ranking. Similarity is a screening number, never "
        "synthetic feasibility.")
    capabilities = ("structure_exploration", "similarity", "scaffold",
                    "fragment", "reaction_evidence")
    execution_modes = (ExecutionMode.IN_PROCESS,)
    arguments_model = ExploreChemicalSpaceArgs
    provenance_policy = (
        "every candidate names its source and its reaction evidence level")

    def __init__(self, explorer=None, resolver=None) -> None:
        self._explorer = explorer
        self._resolver = resolver

    def execute(self, args: ExploreChemicalSpaceArgs):
        if self._explorer is None:
            raise ToolError(ToolErrorCode.UNAVAILABLE,
                            "no chemical explorer is wired")
        entity_id = args.entity_id
        if not entity_id:
            entity_id = self._resolve_input(args)
            if entity_id is None:
                # The input could not become ONE entity. This is an honest
                # answer, not a failure: ambiguity is reported, never resolved.
                return {
                    "status": "unresolved",
                    "target": None,
                    "candidates": [],
                    "diagnostics": [
                        "the input did not resolve to a single chemical entity "
                        "(ambiguous / not found / unavailable); nothing was "
                        "explored from a guess"],
                }
        result = self._explorer.explore(entity_id,
                                        max_candidates=args.max_candidates)
        return result.model_dump(mode="json")

    def _resolve_input(self, args: ExploreChemicalSpaceArgs) -> str | None:
        if self._resolver is None:
            raise ToolError(ToolErrorCode.UNAVAILABLE,
                            "no semantic resolver is wired to resolve the input")
        from crea.semantic.resolver import ChemicalInput

        if args.smiles:
            outcome = self._resolver.resolve(
                ChemicalInput.of("SMILES", args.smiles))
        elif args.chemical:
            outcome = self._resolver.resolve(
                ChemicalInput.of("NAME", args.chemical))
        else:
            raise ToolError(ToolErrorCode.INVALID_INPUT,
                            "give entity_id, chemical, or smiles")
        return outcome.entity.entity_id if outcome.entity is not None else None


# ---------------------------------------------------------------------------
# Tool 10 -- find_chemical_transformations (§16/§17/§18)
# ---------------------------------------------------------------------------


class FindChemicalTransformationsTool(ChemistryTool):
    name = "find_chemical_transformations"
    description = (
        "Check whether a stored Reaction directly connects two entities (a "
        "candidate and a target), in either direction. Returns the "
        "transformation(s) with their reaction, direction (forward/reverse), "
        "evidence level (documented), evidence and provenance. A direct "
        "transformation is a RECORDED reaction fact -- never a structural "
        "inference, never a similarity, never a feasibility judgement. "
        "no_transformation_evidence means 'searched, found nothing', not "
        "'does not exist'; unavailable means 'could not search'.")
    capabilities = ("transformation_evidence", "reaction_lookup",
                    "evidence", "provenance")
    execution_modes = (ExecutionMode.IN_PROCESS,)
    arguments_model = FindChemicalTransformationsArgs
    provenance_policy = (
        "every transformation carries the reaction's own evidence and "
        "provenance; direction is stated, never disguised")

    def __init__(self, service=None) -> None:
        self._service = service

    def execute(self, args: FindChemicalTransformationsArgs):
        if self._service is None:
            raise ToolError(ToolErrorCode.UNAVAILABLE,
                            "no transformation service is wired")
        reactants = list(args.reactant_entity_ids)
        products = list(args.product_entity_ids)
        if args.candidate:
            reactants.append(args.candidate)
        if args.target:
            products.append(args.target)
        result = self._service.find_transformations(reactants, products)
        return result.model_dump(mode="json")


__all__ = [
    "AnalyzeStructureTool",
    "CompareStructuresTool",
    "ExploreChemicalSpaceTool",
    "FindChemicalTransformationsTool",
    "NameStructureTool",
    "ResolveChemicalTool",
    "RetrosynthesizeTool",
    "SearchChemicalTool",
    "SearchLiteratureTool",
    "SearchReactionsTool",
]

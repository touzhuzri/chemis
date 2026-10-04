"""The agent's tools.

Phase 3.0 opened with seven. Phase 3.3 added `retrosynthesize` and
`validate_route_step`, Phase 3.4 added `search_reaction_evidence`, Phase 3.5
added `route_evidence_report`, and each was added to an agent that already
existed rather than beside it.

Each one delegates. None of them re-implements chemistry, literature
retrieval, PDF parsing, evidence construction or claim validation -- those
already exist below this layer, and a second copy of any of them would be a
second set of answers to the same question.

The two tools about reaction-level evidence ask different questions and neither
answers the other's. `search_reaction_evidence` asks what a *search* turned up,
and is honest that provider metadata is usually a discovery and nothing more.
`route_evidence_report` asks what a step can *claim*, having read the search
results, the reaction records and the registry of cited evidence. A step stays
a candidate until the second tool says otherwise -- the first one never does.
"""

from __future__ import annotations

from crea.core.naming import normalize_for_search

from typing import Any

from pydantic import BaseModel, Field

from crea.agent.tools import ToolRegistry, ToolSpec
from crea.chem.normalize import normalize
from crea.chem.scaffold import murcko_scaffold_smiles
from crea.core.ids import content_hash, normalize_doi
from crea.core.models.evidence import Claim
from crea.core.models.research import SourceRef
from crea.core.models.service import LiteratureSearchRequest
from crea.services.document_evidence import search_document
from crea.services.evidence import (
    EvidenceRegistry,
    abstract_to_evidence,
    paper_to_metadata_evidence,
    validate_claim,
)
from crea.services.matching import MatchOptions, build_match_report


# --- argument models --------------------------------------------------------


class _AnalyzeMolecule(BaseModel):
    smiles: str = Field(min_length=1, description="A SMILES string to analyse.")


class _CompareStructures(BaseModel):
    smiles_a: str = Field(min_length=1)
    smiles_b: str = Field(min_length=1)


class _SearchLiterature(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=100)
    #: Return the full Paper records as well as the summary view (Phase
    #: 3.11). The workflow turns these into knowledge; the agent summary
    #: path leaves the flag off so a conversation carries only summaries.
    include_records: bool = False


class _LookupPaper(BaseModel):
    doi: str = Field(min_length=1)


class _SearchLocalDocument(BaseModel):
    document_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    section: str | None = None
    page: int | None = Field(default=None, ge=1)


class _GetDocumentEvidence(BaseModel):
    document_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    section: str | None = None
    page: int | None = Field(default=None, ge=1)
    max_evidence: int = Field(default=5, ge=1, le=20)


class _GetReactionDetails(BaseModel):
    document_id: str = Field(min_length=1)
    query: str = Field(
        default="",
        description="Text to locate the procedure with, e.g. 'Experimental'. "
                    "Ignored when evidence_ids are given.")
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="Specific evidence to read. Empty means search the "
                    "document with `query` first.")
    max_evidence: int = Field(default=4, ge=1, le=10)


class _Retrosynthesize(BaseModel):
    target_smiles: str = Field(min_length=1)
    max_routes: int = Field(default=3, ge=1, le=10)
    max_transforms: int = Field(default=6, ge=1, le=20)


class _ResolveChemicalIdentity(BaseModel):
    """Ask what the user meant. The answer is a status, not a structure.

    Phase 3.6. `kind` and `value` are the user's input as given; neither is
    inferred. `selected_candidate_index` exists for one situation only -- the
    user having looked at the candidates and chosen one -- and is rejected
    without a written record of what they said, because a selection the agent
    made up is indistinguishable from a selection the agent relays unless the
    relay leaves a trace.
    """

    kind: str = Field(
        min_length=1,
        description="What the user gave: SMILES, INCHI, INCHIKEY, "
                    "PUBCHEM_CID, NAME, or IMAGE. Never inferred from the "
                    "value -- the same string means different things in "
                    "different roles.")
    value: str = Field(
        min_length=1,
        description="The identifier, name or image path exactly as supplied.")
    selected_candidate_index: int | None = Field(
        default=None, ge=0,
        description="Set ONLY to relay a choice the USER made from a previous "
                    "AMBIGUOUS answer. Requires "
                    "user_selection_statement. Never set it to pick one "
                    "yourself.")
    user_selection_statement: str = Field(
        default="",
        description="The user's own words making the selection, quoted. "
                    "Required whenever selected_candidate_index is set. "
                    "Writing this field yourself is fabricating a user "
                    "instruction.")


class _ValidateRouteStep(BaseModel):
    step_id: str = Field(min_length=1)


class _SearchReactionEvidence(BaseModel):
    reactants: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    reaction_smiles: str = Field(
        default="",
        description="A reaction SMILES such as 'A.B>>C'. Split on '>>' and "
                    "'.' to fill in reactants and products when those are not "
                    "given. This is string splitting, not atom mapping: the "
                    "atoms are never matched to one another.")
    transformation: str = Field(
        default="",
        description="A transformation name such as 'Suzuki coupling'. Used to "
                    "build the literature query and echoed back. It is a claim "
                    "about the reaction, never a match against one, and it "
                    "cannot promote a step.")
    step_id: str = Field(default="", description="a step from retrosynthesize")
    query: str = Field(default="", help="literature query; built from the "
                                          "structures if not given")
    limit: int = Field(default=5, ge=1, le=25)


class _RecordLink(BaseModel):
    step_id: str = Field(min_length=1)
    reaction_id: str = Field(min_length=1)
    reactant_smiles: list[str] = Field(default_factory=list)
    product_smiles: list[str] = Field(default_factory=list)


class _RouteEvidenceReport(BaseModel):
    """Ask for the evidence chain behind the candidate routes held."""

    record_links: list[_RecordLink] = Field(
        default_factory=list,
        description="Attach reaction records to steps, with the structures read "
                    "for each. Empty structures means the record is reported "
                    "but does not promote its step.")


class _CreateClaim(BaseModel):
    text: str = Field(min_length=1)
    claim_type: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1,
                                    description="At least one evidence id is required.")


class _ExtractReactions(BaseModel):
    """Turn a document into reactions the policy actually validated."""

    document_id: str = Field(min_length=1)
    query: str = Field(default="Experimental")
    paper_id: str = ""
    doi: str = ""


class _GetReaction(BaseModel):
    reaction_id: str = Field(min_length=1)


class _SearchReactions(BaseModel):
    entity_id: str | None = None
    product_entity_id: str | None = None
    reactant_entity_ids: list[str] = Field(default_factory=list)
    paper_id: str | None = None
    doi: str | None = None
    reaction_key: str | None = None
    structure: str | None = None
    structure_mode: str = Field(default="exact", description="exact | substructure")


class _ValidateReaction(BaseModel):
    step_id: str = Field(min_length=1)
    reaction_id: str = Field(min_length=1)


class _GetEntity(BaseModel):
    """One chemical entity by its CREA id (§60)."""

    entity_id: str = Field(min_length=1)


class _SearchEntities(BaseModel):
    """Entity search by name / identity key / external identifier (§30/§31).

    At least one filter. A name search returns every entity carrying that
    name and never creates one.
    """

    name: str | None = None
    identity_key: str | None = None
    namespace: str | None = None
    external_id: str | None = None


class _GetPapersForEntity(BaseModel):
    entity_id: str = Field(min_length=1)


class _GetEvidenceForReaction(BaseModel):
    reaction_id: str = Field(min_length=1)


class _GetRoutesForEntity(BaseModel):
    entity_id: str = Field(min_length=1)


class _GetExistingKnowledge(BaseModel):
    """What is already established for a target (Phase 3.12, §7)."""

    entity_id: str | None = None
    name: str | None = None


class _GetKnowledgeGaps(BaseModel):
    entity_id: str | None = None
    name: str | None = None


class _SearchKnowledge(BaseModel):
    """Cross-object knowledge query. `kind` names the collection; at least
    one filter; deterministic results; never a ranking."""

    kind: str = Field(min_length=1,
                      description="entity | reaction | paper | evidence | record")
    entity_id: str | None = None
    reaction_id: str | None = None
    paper_id: str | None = None
    name: str | None = None


class _SynthesizeEvidence(BaseModel):
    """Recorded condition values side by side. Aggregation only: every
    value keeps its citation, conflicts stay conflicts (§22-§24)."""

    entity_id: str | None = None
    reaction_ids: list[str] = Field(default_factory=list)


# --- implementations -------------------------------------------------------


def build_registry(literature, documents, evidence_sink: list,
                   extractor=None, routes=None, validator=None,
                   identity=None, entities=None, reactions=None,
                   knowledge_base=None,
                   semantic_resolver=None) -> ToolRegistry:
    """Assemble the tools against the services that already exist.

    `literature` is a `LiteratureService`, `documents` a `DocumentLibrary`, and
    `evidence_sink` the list the agent accumulates Evidence into. `identity` is
    the `IdentityState` the Phase 3.6 tool resolves into; given none, one is
    created so the gate exists and reports "no identity question was asked"
    rather than being absent. `entities` is an optional
    `ChemicalEntityRegistry`; when given, an established identity is also
    registered there and the tool reports the resulting `entity_id`.
    `knowledge_base` (Phase 3.11) is the persistent knowledge the query
    tools read when one is wired; without it they answer from the session
    registries and say so when a question needs storage.
    """
    from crea.agent.workspace import IdentityState
    from crea.services.entity_registry import ChemicalEntityRegistry
    from crea.services.reaction_builder import ReactionRegistry

    registry = ToolRegistry()
    #: What the route tools were built around. Held rather than hidden in a
    #: closure so a caller that needs to extend the validator -- with evidence
    #: from a document, say -- can reach it without reaching through a tool.
    registry.routes = routes
    registry.validator = validator or getattr(routes, "validator", None)
    #: The open identity question, and the gate on chemistry that follows from
    #: it. Same reasoning as `routes`: a session fact, not a call fact.
    registry.identity = identity if identity is not None else IdentityState()
    #: The entity registry an established identity is registered with. Same
    #: reasoning again: a session fact the caller can read back.
    registry.entities = (entities if entities is not None
                         else ChemicalEntityRegistry())
    #: The reaction knowledge layer (Phase 3.10): what the reaction tools
    #: register and read back. Same reasoning as the rest: a session fact.
    registry.reactions = (reactions if reactions is not None
                          else ReactionRegistry())
    #: The persistent knowledge base (Phase 3.11), when one is wired. The
    #: query tools answer from it; without one they fall back to the
    #: session registries and never pretend storage exists.
    registry.knowledge_base = knowledge_base
    #: The semantic resolver (CREA 4.0), when wired: identity resolution
    #: flows through it (classification, provider chain, evidence level).
    if semantic_resolver is not None:
        semantic_resolver.bind_entity_registry(registry.entities)
    registry.semantic_resolver = semantic_resolver

    registry.register(ToolSpec(
        name="resolve_chemical_identity",
        description="Establish what structure the user's input names, or say "
                    "why that cannot be done yet. This is the ONLY way a "
                    "chemical name, InChIKey, CID or structure drawing becomes "
                    "a structure. The answer is a status: RESOLVED, AMBIGUOUS, "
                    "INVALID, NOT_FOUND or UNAVAILABLE. AMBIGUOUS means "
                    "several distinct structures survived and none of them has "
                    "been chosen -- present them to the user and do not pick. "
                    "While the status is not RESOLVED, retrosynthesize will "
                    "refuse to run. Never invent a SMILES to skip this step.",
        arguments_model=_ResolveChemicalIdentity,
        run=lambda kind, value, selected_candidate_index=None,
                user_selection_statement="":
            _resolve_identity(identity, kind, value,
                              selected_candidate_index,
                              user_selection_statement,
                              entities=entities,
                              semantic=semantic_resolver),
    ))

    registry.register(ToolSpec(
        name="analyze_molecule",
        description="Normalise a SMILES string and report formula, weight, "
                    "ring and scaffold information, and the computed descriptors. "
                    "Everything returned here is calculated, not looked up.",
        arguments_model=_AnalyzeMolecule,
        run=lambda smiles: _analyze(smiles),
    ))

    registry.register(ToolSpec(
        name="compare_structures",
        description="Explain how two structures differ: shared scaffold, common "
                    "substructure, differing regions, and attachment points.",
        arguments_model=_CompareStructures,
        run=lambda smiles_a, smiles_b: _compare(smiles_a, smiles_b),
    ))

    registry.register(ToolSpec(
        name="search_literature",
        description="Search the configured literature providers and return "
                    "candidate papers with provenance. No ranking is applied.",
        arguments_model=_SearchLiterature,
        run=lambda query, limit, include_records=False: _search_literature(
            literature, query, limit, include_records),
    ))

    registry.register(ToolSpec(
        name="lookup_paper",
        description="Look up one work by DOI across the configured providers.",
        arguments_model=_LookupPaper,
        run=lambda doi: _lookup_paper(literature, doi),
    ))

    registry.register(ToolSpec(
        name="search_local_document",
        description="Search a document the operator supplied. Returns matching "
                    "passages with page and section; it does not interpret them.",
        arguments_model=_SearchLocalDocument,
        run=lambda document_id, query, section=None, page=None: _search_document(
            documents, document_id, query, section, page),
    ))

    registry.register(ToolSpec(
        name="get_document_evidence",
        description="Turn passages of a local document into citable Evidence. "
                    "The quoted text comes from the file; it is never written by a model.",
        arguments_model=_GetDocumentEvidence,
        mutates_evidence=True,
        run=lambda document_id, query, section=None, page=None, max_evidence=5:
            _document_evidence(documents, evidence_sink, document_id, query,
                               section, page, max_evidence),
    ))

    registry.register(ToolSpec(
        name="get_reaction_details",
        description="Read a local document's experimental procedure and return "
                    "structured reaction conditions: reactants, reagents, "
                    "catalysts, solvents, temperature, time, yield. Every field "
                    "must cite the evidence it came from; conditions the "
                    "paper does not state are returned as not stated, never "
                    "guessed.",
        arguments_model=_GetReactionDetails,
        run=lambda document_id, query="", evidence_ids=None, max_evidence=4:
            _reaction_details(documents, evidence_sink, extractor, routes,
                              document_id, query,
                              list(evidence_ids or []), max_evidence),
    ))

    registry.register(ToolSpec(
        name="retrosynthesize",
        description="Ask a retrosynthesis provider for candidate routes to a "
                    "target structure. What comes back is a CANDIDATE: the "
                    "provider proposed it, no literature has been consulted "
                    "and no conditions are known. Use validate_route_step to "
                    "find out what supports it. Refuses to run while the "
                    "chemical identity is unresolved: an AMBIGUOUS input means "
                    "the target structure has not been established, and no "
                    "candidate is a stand-in for it.",
        arguments_model=_Retrosynthesize,
        run=lambda target_smiles, max_routes=3, max_transforms=6:
            _retrosynthesize(routes, identity, target_smiles, max_routes,
                             max_transforms),
    ))

    registry.register(ToolSpec(
        name="validate_route_step",
        description="Check what can actually be shown about a candidate route "
                    "step: an exact structural match, a related reaction, or a "
                    "reaction record with real conditions. A step with nothing "
                    "behind it stays a candidate.",
        arguments_model=_ValidateRouteStep,
        run=lambda step_id: _validate_step(routes, validator, step_id),
    ))

    registry.register(ToolSpec(
        name="search_reaction_evidence",
        description="Look for literature about a specific transformation, and "
                    "say how each candidate relates to it: exact match, one "
                    "side matching, a related transformation, nothing matched, "
                    "or a record that carries no reaction structure to compare "
                    "at all. Literature providers return metadata and "
                    "abstracts, so the common answer is DISCOVERY_ONLY -- the "
                    "work exists and nothing more is known. It never asserts "
                    "that a reaction has been shown to work.",
        arguments_model=_SearchReactionEvidence,
        run=lambda reactants=None, products=None, reaction_smiles="",
                transformation="", step_id="", query="", limit=5:
            _reaction_evidence(routes, validator, literature, evidence_sink,
                               list(reactants or []), list(products or []),
                               reaction_smiles, transformation, step_id, query,
                               limit),
    ))

    registry.register(ToolSpec(
        name="route_evidence_report",
        description="Assemble the evidence chain for the candidate routes held: "
                    "for every step, what literature reaction evidence exists, "
                    "whether an experimental record supports it, which evidence "
                    "rung it reached and why. Cite reaction records by passing "
                    "record_links with the structures read for each; a link "
                    "with no structures is reported but does not promote its "
                    "step. No success rate, no ranking, no recommendation.",
        arguments_model=_RouteEvidenceReport,
        run=lambda record_links=None: _route_evidence(
            routes, evidence_sink, list(record_links or [])),
    ))

    registry.register(ToolSpec(
        name="create_claim",
        description="State a finding and attach the evidence it rests on. "
                    "Rejected unless at least one evidence id resolves.",
        arguments_model=_CreateClaim,
        run=lambda text, claim_type, evidence_refs: _create_claim(
            evidence_sink, text, claim_type, evidence_refs),
    ))

    registry.register(ToolSpec(
        name="extract_reactions",
        description="Turn a supplied document into validated reactions: the "
                    "extraction model proposes, the policy validates, the "
                    "identity layer resolves every component name, and the "
                    "resulting reactions are registered under their reaction "
                    "keys. A document with no extractable procedure reports "
                    "discovery_only rather than an invented reaction.",
        arguments_model=_ExtractReactions,
        run=lambda document_id, query="Experimental", paper_id="", doi="":
            _extract_reactions(documents, evidence_sink, extractor, routes,
                               registry.reactions, registry.entities,
                               registry.identity, document_id, query,
                               paper_id, doi,
                               knowledge_base=knowledge_base),
    ))

    registry.register(ToolSpec(
        name="get_reaction",
        description="Read one registered reaction: its components and the "
                    "entity each resolved to, its status, its evidence.",
        arguments_model=_GetReaction,
        run=lambda reaction_id: _get_reaction(registry.reactions, reaction_id,
                                              knowledge_base=knowledge_base),
    ))

    registry.register(ToolSpec(
        name="search_reactions",
        description="Deterministic reaction search by entity, paper, doi, "
                    "reaction key or structure (exact | substructure). At "
                    "least one filter is required; an unfiltered query is "
                    "INVALID_QUERY. No ranking, no similarity scores.",
        arguments_model=_SearchReactions,
        run=lambda entity_id=None, product_entity_id=None,
                reactant_entity_ids=None, paper_id=None, doi=None,
                reaction_key=None, structure=None, structure_mode="exact":
            _search_reactions(registry.reactions, entity_id, product_entity_id,
                              reactant_entity_ids, paper_id, doi,
                              reaction_key, structure, structure_mode,
                              knowledge_base=knowledge_base),
    ))

    registry.register(ToolSpec(
        name="validate_reaction",
        description="Measure one registered reaction against one route step. "
                    "Exact matches are staged in front of the route "
                    "validation service, which alone decides what the step "
                    "earns; anything less is screening only and promotes "
                    "nothing.",
        arguments_model=_ValidateReaction,
        run=lambda step_id, reaction_id: _validate_reaction(
            routes, registry.validator, registry.reactions, evidence_sink,
            step_id, reaction_id),
    ))

    # -- persistent knowledge queries (Phase 3.11, §60/§61) ----------------
    # "What have we already established about this compound?" is a question
    # about knowledge, answered from the knowledge base -- never by
    # re-searching the internet when the answer is stored (§61).

    registry.register(ToolSpec(
        name="get_entity",
        description="Read one chemical entity by its CREA id: its identity "
                    "key, structure, names and external identifiers. The id "
                    "is the identity; a name never is.",
        arguments_model=_GetEntity,
        run=lambda entity_id: _get_entity(
            registry.entities, knowledge_base, entity_id),
    ))

    registry.register(ToolSpec(
        name="search_entities",
        description="Find chemical entities by name, identity key or external "
                    "identifier. At least one filter. A name search returns "
                    "EVERY entity carrying that name and never creates one.",
        arguments_model=_SearchEntities,
        run=lambda name=None, identity_key=None, namespace=None,
                external_id=None: _search_entities(
                    registry.entities, knowledge_base,
                    name, identity_key, namespace, external_id),
    ))

    registry.register(ToolSpec(
        name="get_papers_for_entity",
        description="Which stored papers mention this entity, via their "
                    "chemical mentions (Paper -> ChemicalMention -> Entity).",
        arguments_model=_GetPapersForEntity,
        run=lambda entity_id: _get_papers_for_entity(
            knowledge_base, entity_id),
    ))

    registry.register(ToolSpec(
        name="get_evidence_for_reaction",
        description="The evidence actually standing behind a stored reaction. "
                    "Only real citations: a reaction whose evidence never "
                    "landed returns nothing, and is not evidence-backed.",
        arguments_model=_GetEvidenceForReaction,
        run=lambda reaction_id: _get_evidence_for_reaction(
            knowledge_base, reaction_id),
    ))

    registry.register(ToolSpec(
        name="get_routes_for_entity",
        description="Which stored routes involve this entity and in what role "
                    "(target / reactant / product / participant), with the "
                    "step ids. Only measured links are followed.",
        arguments_model=_GetRoutesForEntity,
        run=lambda entity_id: _get_routes_for_entity(knowledge_base, entity_id),
    ))

    # -- knowledge-aware research (Phase 3.12, §37-§39) ---------------------
    # The order for "研究某化合物的合成方法" is: resolve identity -> query
    # existing knowledge -> identify gaps -> research the missing evidence
    # -> validate -> update knowledge -> synthesize -> report. Do not
    # mechanically repeat search_literature when the knowledge base already
    # answers the question -- unless the user explicitly asks for the
    # latest literature or a knowledge gap requires it.

    registry.register(ToolSpec(
        name="get_existing_knowledge",
        description="What is ALREADY established for this compound: entity, "
                    "papers, reactions, evidence, routes, with the state of "
                    "each facet (KNOWN / PARTIALLY_KNOWN / UNKNOWN). Answer "
                    "this BEFORE searching: existing knowledge is a record of "
                    "cited past sources, not a claim of currency (§15).",
        arguments_model=_GetExistingKnowledge,
        run=lambda entity_id=None, name=None: _get_existing_knowledge(
            knowledge_base, registry.entities, entity_id, name),
    ))

    registry.register(ToolSpec(
        name="get_knowledge_gaps",
        description="What a complete answer would have that we do not: "
                    "deterministic gaps per facet, each with a readable "
                    "sentence. Research the gaps, not what is already known.",
        arguments_model=_GetKnowledgeGaps,
        run=lambda entity_id=None, name=None: _get_knowledge_gaps(
            knowledge_base, registry.entities, entity_id, name),
    ))

    registry.register(ToolSpec(
        name="search_knowledge",
        description="Deterministic cross-object knowledge query. kind = "
                    "entity | reaction | paper | evidence | record, with at "
                    "least one filter. No ranking, no similarity.",
        arguments_model=_SearchKnowledge,
        run=lambda kind, entity_id=None, reaction_id=None, paper_id=None,
                name=None: _search_knowledge(
                    knowledge_base, kind, entity_id, reaction_id,
                    paper_id, name),
    ))

    registry.register(ToolSpec(
        name="synthesize_evidence",
        description="The experimental condition values recorded for these "
                    "reactions, side by side, each value pointing at its own "
                    "evidence and paper. Conflicts (two sources disagreeing) "
                    "are reported as CONFLICT with both citations -- never "
                    "averaged, never resolved by picking one (§22-§24).",
        arguments_model=_SynthesizeEvidence,
        run=lambda entity_id=None, reaction_ids=None: _synthesize_evidence(
            knowledge_base, entity_id, list(reaction_ids or [])),
    ))

    return registry


# --- the bodies ------------------------------------------------------------


def _analyze(smiles: str) -> dict:
    molecule = normalize(smiles)
    d = molecule.descriptors
    return {
        "canonical_smiles": molecule.canonical_smiles,
        "molecular_formula": d.molecular_formula,
        "molecular_weight": round(d.mol_wt, 4),
        "exact_molecular_weight": round(d.exact_mol_wt, 4),
        "atom_count": len(molecule.atoms),
        "heavy_atom_count": d.heavy_atom_count,
        "bond_count": len(molecule.bonds),
        "ring_count": d.num_rings,
        "aromatic_ring_count": d.num_aromatic_rings,
        "scaffold": murcko_scaffold_smiles(molecule),
        "descriptors": {
            "tpsa": round(d.tpsa, 3),
            "logp": round(d.logp, 3),
            "hbd": d.hbd,
            "hba": d.hba,
            "rotatable_bonds": d.rotatable_bonds,
            "fraction_csp3": round(d.fraction_csp3, 3),
            "net_charge": d.net_charge,
        },
        "inchikey": molecule.inchikey,
        "source": "computed",
    }


def _compare(smiles_a: str, smiles_b: str) -> dict:
    report = build_match_report(normalize(smiles_a), normalize(smiles_b),
                               options=MatchOptions())
    return {
        "verdict": report.verdict,
        "verdict_reasons": report.verdict_reasons,
        "scaffold_relation": report.scaffold.relation,
        "common_atoms": report.core.num_atoms,
        "common_bonds": report.core.num_bonds,
        "coverage_a": report.core.coverage_a,
        "coverage_b": report.core.coverage_b,
        "differing_regions": [
            {
                "side": "A",
                "smiles": c.canonical_smiles_with_star,
                "atoms": c.heavy_atom_count,
                "attaches_via": c.attachment_note,
            }
            for c in report.core.unmatched_components_a
        ] + [
            {
                "side": "B",
                "smiles": c.canonical_smiles_with_star,
                "atoms": c.heavy_atom_count,
                "attaches_via": c.attachment_note,
            }
            for c in report.core.unmatched_components_b
        ],
        "attachment_differences": [
            {
                "fragment_a": am.frag_a_id,
                "fragment_b": am.frag_b_id,
                "rcad_a": am.rcad_a,
                "rcad_b": am.rcad_b,
                "identical": am.rcad_equal,
                "position_a": am.position_a,
                "position_b": am.position_b,
            }
            for am in report.attachment_matches
        ],
        "limitations": report.limitations,
    }


def _search_literature(service, query: str, limit: int,
                       include_records: bool = False) -> dict:
    result = service.search(LiteratureSearchRequest(query=query, limit=limit))
    payload = {
        "query": query,
        "outcome": result.outcome.value,
        "paper_count": len(result.papers),
        "provider_runs": [
            {"name": r.name, "status": r.status.value, "records": r.records_found,
             "error": r.error}
            for r in result.provider_runs
        ],
        "papers": [
            {
                "paper_id": p.paper_id,
                "doi": p.identifiers.doi,
                "title": p.title,
                "journal": p.journal,
                "year": p.year,
                "sources": p.sources,
                "url": p.url,
            }
            for p in result.papers
        ],
        "warnings": result.warnings,
    }
    if include_records:
        # Full domain objects, for whoever will keep them (§16). The summary
        # above stays the agent-facing view; this is the same data the
        # literature layer already holds, passed through untouched.
        payload["paper_records"] = [
            p.model_dump(mode="json") for p in result.papers]
    return payload


def _lookup_paper(service, doi: str) -> dict:
    result = service.lookup_by_doi(doi)
    return {
        "doi": normalize_doi(doi),
        "found": result.found,
        "paper": (
            {
                "paper_id": result.paper.paper_id,
                "title": result.paper.title,
                "journal": result.paper.journal,
                "year": result.paper.year,
                "authors": [a.name for a in result.paper.authors],
                "sources": result.paper.sources,
            } if result.paper else None
        ),
        "warnings": result.warnings,
    }


def _require_document(documents, document_id: str):
    """A document that is not there is a failed call.

    Raised rather than returned as `{"error": ...}` with a success status:
    an error-shaped success is a soft failure the agent has no reason to
    record, and "I asked for a document and there was none" belongs in the trace
    as a failure.
    """
    parsed = documents.get(document_id)
    if parsed is None:
        from crea.core.errors import DocumentError
        from crea.core.models.document import DocumentErrorKind

        available = ", ".join(documents.ids()) or "(none)"
        raise DocumentError(
            f"no document with id {document_id!r}; available: {available}",
            kind=DocumentErrorKind.NOT_FOUND, path="",
        )
    return parsed


def _search_document(documents, document_id: str, query: str,
                     section: str | None, page: int | None) -> dict:
    parsed = _require_document(documents, document_id)
    hits = search_document(parsed, query, section=section, page=page)
    return {
        "found": bool(hits),
        "document_id": document_id,
        "query": query,
        "hit_count": len(hits),
        "matches": [
            {
                "page": h.page,
                "section": h.section,
                "section_kind": h.section_kind,
                "match_type": h.match_type.value,
                "position": h.position,
                "quote": h.evidence.raw_quote,
            }
            for h in hits
        ],
    }


def _document_evidence(documents, sink: list, document_id: str, query: str,
                       section: str | None, page: int | None,
                       max_evidence: int) -> dict:
    parsed = _require_document(documents, document_id)
    hits = search_document(parsed, query, section=section, page=page,
                           max_hits=max_evidence)
    created: list[str] = []
    for hit in hits:
        sink.append(hit.evidence)
        created.append(hit.evidence.evidence_id)
    return {
        "created": len(created),
        "evidence_ids": created,
        "query": query,
        "document_id": document_id,
        "document_sha256": parsed.document.sha256,
        "evidence_type": hits[0].evidence.evidence_type.value if hits else None,
        "strength": hits[0].evidence.strength.value if hits else None,
    }


def _resolve_identity(identity, kind: str, value: str,
                      selected_candidate_index: int | None,
                      user_selection_statement: str,
                      entities=None, semantic=None) -> dict:
    """Resolve the user's input, or say why no structure can be established.

    Nothing here chooses a structure. The service decides the status; this
    function only reports it, in the words the agent must say to the user, and
    records the outcome so the chemistry gate can see it. The one case where a
    candidate is promoted is a selection the *user* made, and that is only
    accepted alongside their own words making it.
    """
    from crea.core.models.identity import (
        StructureInput,
        StructureResolutionStatus,
    )
    from crea.services.identity import IdentityResolutionService

    service = getattr(identity, "service", None)
    if service is None:
        # No database was wired in. SMILES and InChI still resolve -- the
        # deterministic path needs no source -- and a name reports
        # UNAVAILABLE rather than inventing one. See §8.
        service = IdentityResolutionService()
        if identity is not None:
            identity.service = service

    if selected_candidate_index is not None and not user_selection_statement.strip():
        return {
            "status": "rejected",
            "reason_codes": ["SELECTION_WITHOUT_USER_STATEMENT"],
            "reason": "selected_candidate_index was given without "
                      "user_selection_statement. A selection is only real if "
                      "the user made it; record what they said, or leave the "
                      "index unset and present the candidates.",
            "chemistry_allowed": False,
            "agent_message": "候选结构不能由我来选。请让用户从候选列表中选择。",
            "candidates": [],
        }

    try:
        inp = StructureInput.of(kind=kind, value=value)
    except Exception as exc:  # noqa: BLE001 - a bad kind is a tool argument error
        return {
            "status": "rejected",
            "reason_codes": ["INVALID_INPUT_KIND"],
            "reason": f"cannot read the input as a chemical identity: {exc}",
            "chemistry_allowed": False,
            "agent_message": "无法识别输入类型。",
            "candidates": [],
        }

    # CREA 4.0 (§14): when a semantic resolver is wired, identity flows
    # through it -- the same deterministic service underneath, plus the
    # provider chain, the evidence level and the provider books. Without
    # one, this path is byte-for-byte what it has always been.
    semantic_view: dict = {}
    semantic_entity_id = None
    if semantic is not None:
        from crea.semantic.resolver import ChemicalInput

        outcome = semantic.resolve(ChemicalInput.from_structure_input(inp))
        resolution = outcome.resolution
        if resolution is None:  # pragma: no cover - the resolver answers
            resolution = service.resolve(inp)
        semantic_view = {
            "identity_evidence": outcome.identity_evidence.value,
            "provider_results": [p.model_dump(mode="json")
                                 for p in outcome.provider_results],
        }
        if outcome.entity is not None:
            semantic_entity_id = outcome.entity.entity_id
    else:
        resolution = service.resolve(inp)

    # A user selection is applied AFTER the resolution was produced, and only
    # to an AMBIGUOUS one. It does not change the status the service derived;
    # it records that the user settled it.
    if selected_candidate_index is not None and resolution.is_ambiguous:
        if selected_candidate_index >= len(resolution.candidates):
            return {
                "status": "rejected",
                "reason_codes": ["SELECTION_OUT_OF_RANGE"],
                "reason": f"there are {len(resolution.candidates)} candidates; "
                          f"index {selected_candidate_index} is not one of them",
                "chemistry_allowed": False,
                "agent_message": "发现 0 个候选结构，需要进一步确认。",
                "candidates": [_candidate_view(c) for c in resolution.candidates],
            }
        chosen = resolution.candidates[selected_candidate_index]
        settled = chosen.model_copy(update={
            "notes": list(chosen.notes) + [
                f"user_selection_statement: {user_selection_statement.strip()}"
            ],
        })
        resolution = resolution.model_copy(update={
            "status": StructureResolutionStatus.RESOLVED,
            "selected": settled,
            "selected_basis": chosen.identity_basis,
            "reason": (f"the user selected candidate {selected_candidate_index} "
                       f"({chosen.candidate_name or chosen.source_id}) from "
                       f"{len(resolution.candidates)} candidates"),
            "reason_codes": ["USER_SELECTED"],
            "rules_applied": list(resolution.rules_applied) + ["user_selected_candidate"],
        })

    if identity is not None:
        identity.hold(resolution)

    view = _identity_view(resolution)
    view.update(semantic_view)
    # An established identity may also be registered as a ChemicalEntity.
    # `entity_id` is an internal label for that entity -- never structure
    # evidence, never a reason to treat the identity as settled.
    view["entity_id"] = None
    if semantic_entity_id is not None:
        # the semantic resolver already minted-or-reused the entity
        view["entity_id"] = semantic_entity_id
    elif entities is not None and resolution.is_resolved:
        try:
            from crea.core.models.entity import EntityRegistrationError

            entity = entities.register_resolution(resolution)
        except EntityRegistrationError as exc:
            view["entity_error"] = str(exc)
        else:
            view["entity_id"] = entity.entity_id
            if semantic is not None:
                # representations travel through every registration door
                semantic.enrich_entity(entity, resolution)
    return view


def _candidate_view(candidate) -> dict:
    """One candidate, in the field set §9 requires a reader to be able to see.

    `input_name, candidate_name, synonyms, canonical_smiles, inchi, inchikey,
    source` -- all retained. A person deciding between three candidates needs
    all three of them describable, and a tool result that kept only the winner
    would make the choice unreviewable.
    """
    return {
        "input_name": candidate.input_name,
        "candidate_name": candidate.candidate_name,
        "synonyms": list(candidate.synonyms),
        "canonical_smiles": candidate.canonical_smiles,
        "isomeric_smiles": candidate.isomeric_smiles,
        "inchi": candidate.inchi,
        "inchikey": candidate.inchikey,
        "molecular_formula": candidate.molecular_formula,
        "molecular_weight": candidate.molecular_weight,
        "source": candidate.source,
        "source_id": candidate.source_id,
        "source_url": candidate.source_url,
        "matched_by": candidate.matched_by.value,
        "matched_synonym": candidate.matched_synonym,
        "identity_basis": candidate.identity_basis.value,
        "facets": candidate.facets.model_dump(),
        "recognition_stage": (candidate.recognition_stage.value
                              if candidate.recognition_stage else None),
        "notes": list(candidate.notes),
    }


_STATUS_MESSAGE = {
    "resolved": "已解析为一个确定结构。",
    "ambiguous": "发现 {n} 个候选结构，需要进一步确认。",
    "invalid": "输入不是有效的结构表示。",
    "not_found": "该来源没有对应条目。",
    "unavailable": "无法查询化学数据库。",
    "candidate": "识别到候选结构，但尚未确认。",
    "input": "输入已收到，尚未解析。",
}


def _identity_view(resolution) -> dict:
    """The whole resolution, shaped for a reader who must not guess.

    `chemistry_allowed` is the gate, stated in the result rather than left for
    the agent to infer -- and `agent_message` is the sentence §17 requires,
    generated from the status so the agent cannot soften it.
    """
    status = resolution.status.value
    distinct = len({c.inchikey or c.canonical_smiles
                    for c in resolution.candidates if c.describes_structure()})
    template = _STATUS_MESSAGE.get(status, status)
    message = template.format(n=distinct or len(resolution.candidates))

    return {
        "status": status,
        "reason": resolution.reason,
        "reason_codes": list(resolution.reason_codes),
        "rules_applied": list(resolution.rules_applied),
        "input": {
            "kind": resolution.input.kind.value,
            "value": resolution.input.value,
        },
        "candidates": [_candidate_view(c) for c in resolution.candidates],
        "candidate_count": len(resolution.candidates),
        "distinct_structure_count": distinct,
        "selected": (_candidate_view(resolution.selected)
                     if resolution.selected is not None else None),
        "selected_basis": resolution.selected_basis.value,
        "chemistry_allowed": resolution.is_resolved,
        "agent_message": message,
    }


def _retrosynthesize(routes, identity, target_smiles: str, max_routes: int,
                     max_transforms: int) -> dict:
    """Provider call. An unavailable engine is a result, not a route.

    Two refusals before anything is asked of the provider, both Phase 3.6:
    an open identity question, and a target that does not parse. Between them
    they close the two ways a guessed structure could reach a route search --
    a candidate picked from an `AMBIGUOUS` list, and a SMILES written from
    memory. Neither is an establishment of a structure, and the failure they
    prevent is silent: a route built for the wrong molecule is still a route.
    """
    from crea.core.models.route import RetrosynthesisRequest

    if identity is not None:
        blocked, why = identity.blocks_chemistry()
        if blocked:
            held = identity.resolution
            return {
                "status": "blocked",
                "reason": why,
                "reason_codes": ["IDENTITY_NOT_RESOLVED",
                                 (held.status.value if held else "unknown").upper()],
                "routes": [],
                "route_count": 0,
                "chemistry_allowed": False,
                "agent_message": (
                    "发现 "
                    f"{len(held.candidates) if held else 0} "
                    "个候选结构，需要进一步确认。"
                ) if (held and held.is_ambiguous) else why,
            }

    smiles = (target_smiles or "").strip()
    try:
        molecule = normalize(smiles, source="user_input")
    except Exception as exc:  # noqa: BLE001 - a parse failure is a result
        return {
            "status": "blocked",
            "reason": f"the target is not a structure RDKit can parse: {exc}",
            "reason_codes": ["TARGET_NOT_A_STRUCTURE", "INVALID"],
            "routes": [],
            "route_count": 0,
            "chemistry_allowed": False,
            "agent_message": "目标结构无法解析，不能进行逆合成。",
        }

    provider = getattr(routes, "provider", None) if routes else None
    if provider is None:
        return {"routes": [], "status": "not_configured",
                "reason": "no retrosynthesis provider is configured"}

    result = provider.retrosynthesize(RetrosynthesisRequest(
        target_smiles=molecule.canonical_smiles, max_routes=max_routes,
        max_transforms=max_transforms))
    routes.store(result)

    return {
        "status": result.status.value,
        "reason": result.reason,
        "target_smiles": molecule.canonical_smiles,
        "route_count": len(result.routes),
        "provider": result.provenance.provider,
        "provider_version": result.provenance.provider_version,
        "warnings": result.warnings,
        # the whole route, so the agent can rebuild it from what the tool
        # returned rather than from anything the model said about it
        "routes": [route.model_dump(mode="json") for route in result.routes],
    }


def _validate_step(routes, validator, step_id: str) -> dict:
    from crea.core.errors import DocumentError
    from crea.core.models.document import DocumentErrorKind
    from crea.services.route_validation import RouteValidationService, unresolved_note

    if routes is None or not routes.routes:
        raise DocumentError(
            "no candidate route is held; call retrosynthesize first",
            kind=DocumentErrorKind.NOT_FOUND, path="")
    _route, step = routes.find_step(step_id)
    if step is None:
        held = [s.step_id for r in routes.routes for s in r.steps]
        raise DocumentError(
            f"no step {step_id!r}; held steps: {held}",
            kind=DocumentErrorKind.NOT_FOUND, path="")

    validator = validator or getattr(routes, "validator", None)
    if validator is None:
        return {"step_id": step_id, "support_level": "no_support",
                "warnings": ["no route validator is configured"]}

    result = validator.validate(step)
    RouteValidationService.apply(result, step)
    if not hasattr(routes, "results"):
        routes.results = []
    routes.results.append(result)
    note = unresolved_note(result, step)
    if note:
        if not hasattr(routes, "notes"):
            routes.notes = []
        routes.notes.append(note)

    return {
        "step_id": step_id,
        "support_level": result.support_level.value,
        "match_kind": result.match_kind,
        "status": step.status.value,
        "checked": result.checked_count,
        "evidence_refs": result.evidence_refs,
        "paper_ids": result.paper_ids,
        "reaction_record_ids": result.reaction_record_ids,
        "matched_smiles": result.matched_smiles,
        "warnings": result.warnings + ([note] if note else []),
    }


def _split_reaction_smiles(reaction_smiles: str) -> tuple[list[str], list[str]]:
    """`A.B>>C` into `([A, B], [C])`.

    String splitting, and nothing more. The atoms are never matched to one
    another and no reaction centre is inferred, so this is not atom mapping in
    disguise -- it is the same split a person does when they read a scheme off
    a slide. Anything else about the arrow is left alone.
    """
    if not reaction_smiles or ">>" not in reaction_smiles:
        return [], []
    left, _, right = reaction_smiles.partition(">>")
    split = lambda side: [s for s in side.split(".") if s.strip()]  # noqa: E731
    return split(left), split(right)


def _reaction_evidence(routes, validator, literature, sink, reactants, products,
                       reaction_smiles, transformation, step_id, query,
                       limit) -> dict:
    """Literature about a transformation, and how far each candidate is from it.

    Reports a relationship, never a verdict. The strongest thing it can say is
    "this candidate's structures match this step's", and even that is not a
    claim that anyone ran the reaction.
    """
    from crea.core.models.service import LiteratureSearchRequest
    from crea.core.models.route import ReactionMatchType
    from crea.services.literature_reactions import (
        candidates_from_papers, match_candidate, reference_for,
    )

    # structures from the step when one is named, else from the arguments,
    # else from the reaction SMILES. In that order, and never merged: a step
    # the agent named is a step that exists, and silently adding SMILES it
    # also happened to pass would make the comparison about something else.
    if step_id and routes is not None:
        _route, step = routes.find_step(step_id)
        if step is not None:
            reactants = reactants or [m.canonical_smiles or m.smiles
                                     for m in step.reactants]
            products = products or [m.canonical_smiles or m.smiles
                                   for m in step.products]
    if not reactants and not products:
        parsed_r, parsed_p = _split_reaction_smiles(reaction_smiles)
        reactants, products = reactants or parsed_r, products or parsed_p
    if not reactants and not products:
        return {"candidates": [], "outcome": "no_structures",
                "warnings": ["give reactants, products, a reaction_smiles, "
                             "or a step_id"]}

    if not query:
        # built from what we know, not from a model's phrasing
        query = " ".join(products or reactants) or "reaction"
    if transformation:
        query = f"{transformation} {query}".strip()

    result = literature.search(LiteratureSearchRequest(query=query, limit=limit))

    comparisons = []
    for candidate in candidates_from_papers(result.papers):
        match = match_candidate(
            step_id=step_id or "", candidate=candidate,
            reference=reference_for(candidate),
            step_reactants=reactants, step_products=products,
            evidence_refs=[])
        comparisons.append(match.model_dump(mode="json"))

    counts: dict[str, int] = {}
    for item in comparisons:
        counts[item["match_type"]] = counts.get(item["match_type"], 0) + 1

    best = ReactionMatchType.DISCOVERY_ONLY
    for kind in ("exact", "partial", "transformation", "no_match",
                 "discovery_only"):
        if counts.get(kind):
            best = ReactionMatchType(kind)
            break

    pool = getattr(validator, "structures", None)
    held = [{"source_id": sid, "smiles": smi, "kind": kind}
            for sid, smi, kind in (pool.canonical() if pool is not None else [])]

    warnings = [
        "a structural match is not an experimental record: nothing here "
        "establishes that anyone ran this reaction",
        "DISCOVERY_ONLY means the record carried no reaction structure to "
        "compare -- metadata, an abstract and a DOI cannot support a step",
        "NO_MATCH means structures were compared and none matched; neither "
        "says that no such paper exists",
        "this tool returns no evidence ids: it read paper metadata, not "
        "text. Experimental support has to come from get_reaction_details "
        "over a document, and only then may a step be promoted",
    ]
    if transformation:
        warnings.append(
            f"'{transformation}' was used as search text and is reported as a "
            f"declared transformation. It was not verified against anything "
            f"and cannot promote a step.")

    return {
        "step_id": step_id or "",
        "query": query,
        "reactants": reactants,
        "products": products,
        "reaction_smiles": reaction_smiles,
        "declared_transformation": transformation,
        "outcome": result.outcome.value,
        "candidates": comparisons,
        "match_counts": counts,
        "best_match": best.value,
        "paper_ids": [p.paper_id for p in result.papers],
        # Structures this task holds, listed and deliberately not matched.
        # The pool records which source a structure came from, not which side
        # of a reaction it belongs to, and comparing them against both sides at
        # once would manufacture an exact match out of that gap.
        "held_structures": held,
        # Nothing is added to the evidence sink here, so nothing is quoted
        # back. This tool compares structures against papers it read the
        # metadata of; it never read a sentence out of a document, so there is
        # no quote to anchor an Evidence to. An empty list here is the honest
        # answer, and is what keeps this below every experimental rung.
        "evidence_refs": [],
        "warnings": warnings,
    }


def _route_evidence(routes, sink, record_links) -> dict:
    """The full chain, per step, with the rung each step actually reached."""
    from crea.core.errors import DocumentError
    from crea.core.models.document import DocumentErrorKind
    from crea.services.evidence import EvidenceRegistry
    from crea.services.route_evidence import (
        RouteEvidenceInputs, RouteEvidenceService, build_record_link,
    )

    if routes is None or not routes.routes:
        raise DocumentError(
            "no candidate route is held; call retrosynthesize first",
            kind=DocumentErrorKind.NOT_FOUND, path="")

    records = list(getattr(routes, "records", []) or [])
    links = []
    for raw in record_links:
        # A tool's arguments arrive after `model_dump()`, which flattens
        # nested models to plain dicts -- so the spec is rebuilt here before
        # anything reads a field off it. Passing an already-built spec
        # through `model_validate` leaves it unchanged.
        spec = _RecordLink.model_validate(raw)
        _route, step = routes.find_step(spec.step_id)
        if step is None:
            raise DocumentError(
                f"no step {spec.step_id!r}; held steps: "
                f"{[s.step_id for r in routes.routes for s in r.steps]}",
                kind=DocumentErrorKind.NOT_FOUND, path="")
        record = next((r for r in records
                       if r.reaction_id == spec.reaction_id), None)
        links.append(build_record_link(
            step, spec.reaction_id,
            list(spec.reactant_smiles or []), list(spec.product_smiles or []),
            list(record.evidence_refs) if record is not None else []))

    registry = EvidenceRegistry(items={e.evidence_id: e for e in sink})
    service = RouteEvidenceService(registry, RouteEvidenceInputs(
        candidates=[], records=records, record_links=links))

    reports = [service.build(route) for route in routes.routes]
    routes.evidence_reports = reports
    return {
        "reports": [r.model_dump(mode="json") for r in reports],
        "route_count": len(reports),
        "unresolved": [u for r in reports for u in r.unresolved],
        "warnings": [
            "no success rate, ranking or recommendation is produced; two "
            "routes are described and which to attempt is not answered here",
        ],
    }


def _reaction_summary(reaction) -> dict:
    """One reaction, as a tool result: identity, provenance, components."""
    return {
        "reaction_id": reaction.reaction_id,
        "reaction_status": reaction.status.value,
        "reaction_key": reaction.reaction_key,
        "record_id": reaction.record_id,
        "paper_id": reaction.paper_id,
        "doi": reaction.doi,
        "document_id": reaction.document_id,
        "components": [
            {
                "role": c.role.value,
                "name": c.name,
                "entity_id": c.entity_id,
                "identity_key": c.identity_key,
                "canonical_smiles": c.canonical_smiles,
                "mention_id": c.mention.mention_id if c.mention else None,
            }
            for c in reaction.components()
        ],
        "evidence_refs": list(reaction.evidence_refs),
        "unresolved_evidence_refs": list(reaction.unresolved_evidence_refs),
        "notes": list(reaction.notes),
    }


def _extract_reactions(documents, sink: list, extractor, routes, reactions,
                       entities, identity, document_id: str, query: str,
                       paper_id: str, doi: str, *,
                       knowledge_base=None) -> dict:
    """Document -> records -> reactions. The model proposes; the policy and
    the identity layer decide. Nothing here lets a model write a structure."""
    from crea.core.models.reaction import ReactionRecord
    from crea.services.mention_resolution import ChemicalMentionResolver
    from crea.services.reaction_builder import ReactionBuilder

    # the passages this extraction reads are held evidence first: a record
    # may only cite what the task actually holds
    _document_evidence(documents, sink, document_id,
                       query or "Experimental", None, None, 5)
    details = _reaction_details(documents, sink, extractor, routes,
                                document_id, query or "Experimental", [], 4)
    records = []
    for payload in details.get("records") or []:
        try:
            records.append(ReactionRecord.model_validate(payload))
        except Exception:  # noqa: BLE001 - an unreadable record is not a record
            continue

    from crea.services.mention_resolution import ParentheticalContext
    from crea.services.reaction_builder import product_mentions_from_document

    service = getattr(identity, "service", None)
    resolver = ChemicalMentionResolver(
        entities, identity=service,
        context_sources=(ParentheticalContext(),),
        held_evidence=sink)
    builder = ReactionBuilder(resolver, entity_registry=entities,
                              held_evidence=sink)
    document_text = ""
    if documents is not None and document_id:
        parsed = documents.get(document_id)
        if parsed is not None:
            document_text = chr(10).join(p.text for p in parsed.pages if p.text)
    built = []
    if knowledge_base is not None:
        # Held evidence is knowledge already (§18): a persistent registry
        # refuses a reaction whose citations do not resolve, so what the
        # extraction is about to cite is stored first, append-only.
        for item in sink:
            knowledge_base.save_evidence(item)
    for record in records:
        products = product_mentions_from_document(
            record, document_text, resolver,
            source="user_provided", source_id=document_id or "")
        reaction = builder.build(record, paper_id=paper_id or "",
                                 doi=doi or "", source_id=document_id or "",
                                 product_mentions=products)
        kept, _merged = reactions.register(reaction)
        built.append(_reaction_summary(kept))

    notes = list(details.get("notes") or [])
    if not built:
        notes.append("该文献被发现，但当前没有足够实验文本建立 Reaction。")
    return {"reactions": built, "record_count": len(records),
            "status": "discovery_only" if not built else "ok",
            "notes": notes}


def _get_reaction(reactions, reaction_id: str, *, knowledge_base=None) -> dict:
    try:
        reaction = reactions.get(reaction_id)
    except KeyError:
        if knowledge_base is None:
            return {"status": "not_found", "reaction_id": reaction_id}
        from crea.repositories.errors import NotFound

        try:
            reaction = knowledge_base.get_reaction(reaction_id)
        except NotFound:
            return {"status": "not_found", "reaction_id": reaction_id}
    return {"status": "ok", **_reaction_summary(reaction)}


def _search_reactions(reactions, entity_id, product_entity_id,
                      reactant_entity_ids, paper_id, doi, reaction_key,
                      structure, structure_mode, *,
                      knowledge_base=None) -> dict:
    try:
        found = reactions.search(
            entity_id=entity_id, product_entity_id=product_entity_id,
            reactant_entity_ids=[r for r in (reactant_entity_ids or []) if r],
            paper_id=paper_id, doi=doi, reaction_key=reaction_key,
            structure=structure, structure_mode=structure_mode)
    except ValueError as exc:
        return {"status": "invalid_query", "error": str(exc)}
    if knowledge_base is not None:
        # What earlier runs established is knowledge too (§61): the union,
        # session first, de-duplicated by reaction id, in a deterministic
        # order given deterministic stores.
        try:
            stored = knowledge_base.search_reactions(
                entity_id=entity_id, product_entity_id=product_entity_id,
                reactant_entity_ids=[r for r in (reactant_entity_ids or []) if r],
                paper_id=paper_id, doi=doi, reaction_key=reaction_key,
                structure=structure, structure_mode=structure_mode)
        except ValueError:
            stored = []
        seen = {r.reaction_id for r in found}
        found = found + [r for r in stored if r.reaction_id not in seen]
    return {"status": "ok", "count": len(found),
            "reactions": [_reaction_summary(r) for r in found]}


def _entity_view(entity) -> dict:
    """The entity in the field set a reader needs (§10): identity and names,
    with provenance on each. Serialisable, never a Python repr."""
    return {
        "entity_id": entity.entity_id,
        "identity_key": entity.identity_key,
        "identity_key_version": entity.identity_key_version,
        "identity_basis": entity.identity_basis.value,
        "canonical_smiles": entity.canonical_smiles,
        "inchi": entity.inchi,
        "inchikey": entity.inchikey,
        "molecular_formula": entity.molecular_formula,
        "molecular_weight": entity.molecular_weight,
        "names": list(entity.names),
        "external_identifiers": [
            {"namespace": i.namespace, "identifier": i.identifier,
             "source": i.source}
            for i in entity.external_identifiers],
        "identity_source": entity.identity_source,
        "source_id": entity.source_id,
        "created_at": entity.created_at,
    }


def _get_entity(entities_registry, knowledge_base, entity_id: str) -> dict:
    from crea.core.models.entity import EntityNotFound
    from crea.repositories.errors import NotFound

    entity = None
    try:
        entity = entities_registry.get(entity_id)
    except EntityNotFound:
        if knowledge_base is not None:
            try:
                entity = knowledge_base.get_entity(entity_id)
            except NotFound:
                entity = None
    if entity is None:
        return {"status": "not_found", "entity_id": entity_id}
    return {"status": "ok", **_entity_view(entity)}


def _search_entities(entities_registry, knowledge_base, name, identity_key,
                     namespace, external_id) -> dict:
    if not any([name, identity_key, namespace and external_id]):
        return {"status": "invalid_query",
                "error": "INVALID_QUERY: at least one filter is required "
                         "(name, identity_key, or namespace+external_id)"}

    found: list = []
    seen: set[str] = set()

    def offer(entity) -> None:
        if entity is not None and entity.entity_id not in seen:
            seen.add(entity.entity_id)
            found.append(entity)

    if knowledge_base is not None:
        if name:
            for entity in knowledge_base.search_entities_by_name(name):
                offer(entity)
        if identity_key:
            offer(knowledge_base.find_entity_by_identity_key(identity_key))
        if namespace and external_id:
            offer(knowledge_base.find_entity_by_external_id(namespace, external_id))
    # The session registry answers too: knowledge established this run is
    # knowledge (and with a wired knowledge base it is the same store).
    if name:
        for entity_id in entities_registry.entity_ids:
            entity = entities_registry.get(entity_id)
            if any(normalize_for_search(n) == normalize_for_search(name)
                   for n in entity.names):
                offer(entity)
    if identity_key:
        for entity_id in entities_registry.entity_ids:
            entity = entities_registry.get(entity_id)
            if entity.identity_key == identity_key:
                offer(entity)
    if namespace and external_id:
        offer(entities_registry.find_by_external_id(namespace, external_id))

    found.sort(key=lambda e: (e.created_at, e.entity_id))
    return {"status": "ok", "count": len(found),
            "entities": [_entity_view(e) for e in found]}


def _get_papers_for_entity(knowledge_base, entity_id: str) -> dict:
    if knowledge_base is None:
        return {"status": "no_knowledge_base",
                "error": "no persistent knowledge base is wired; "
                         "paper/entity relations live there"}
    from crea.repositories.errors import NotFound

    try:
        knowledge_base.get_entity(entity_id)
    except NotFound:
        return {"status": "not_found", "entity_id": entity_id}
    papers = knowledge_base.get_papers_for_entity(entity_id)
    return {"status": "ok", "count": len(papers),
            "papers": [
                {"paper_id": p.paper_id, "doi": p.doi, "title": p.title,
                 "journal": p.journal, "year": p.year}
                for p in papers]}


def _get_evidence_for_reaction(knowledge_base, reaction_id: str) -> dict:
    if knowledge_base is None:
        return {"status": "no_knowledge_base",
                "error": "no persistent knowledge base is wired; "
                         "citation relations live there"}
    from crea.repositories.errors import NotFound

    try:
        knowledge_base.get_reaction(reaction_id)
    except NotFound:
        return {"status": "not_found", "reaction_id": reaction_id}
    evidence = knowledge_base.get_evidence_for_reaction(reaction_id)
    return {"status": "ok", "count": len(evidence),
            "evidence": [
                {"evidence_id": e.evidence_id,
                 "evidence_type": e.evidence_type.value,
                 "strength": e.strength.value,
                 "source": e.source, "source_id": e.source_id,
                 "raw_quote": e.raw_quote, "paper_id": e.paper_id,
                 "doi": e.doi, "retrieved_at": e.retrieved_at}
                for e in evidence]}


def _get_routes_for_entity(knowledge_base, entity_id: str) -> dict:
    if knowledge_base is None:
        return {"status": "no_knowledge_base",
                "error": "no persistent knowledge base is wired; "
                         "route relations live there"}
    from crea.repositories.errors import NotFound

    try:
        knowledge_base.get_entity(entity_id)
    except NotFound:
        return {"status": "not_found", "entity_id": entity_id}
    hits = knowledge_base.get_routes_for_entity(entity_id)
    return {"status": "ok", "count": len(hits), "routes": list(hits)}


# -- knowledge-aware research (Phase 3.12) ---------------------------------


def _resolve_target_entity(knowledge_base, entities_registry, entity_id,
                           name):
    """The target entity, from an id or a known name. Never invented."""
    from crea.repositories.errors import NotFound

    if entity_id:
        try:
            return (knowledge_base.get_entity(entity_id)
                    if knowledge_base is not None
                    else entities_registry.get(entity_id))
        except NotFound:
            from crea.core.models.entity import EntityNotFound

            try:
                return entities_registry.get(entity_id)
            except EntityNotFound:
                return None
    if name:
        found = _search_entities(entities_registry, knowledge_base,
                                 name, None, None, None)
        entities = found.get("entities") or []
        if len(entities) == 1:
            entity_id = entities[0]["entity_id"]
            return _resolve_target_entity(knowledge_base, entities_registry,
                                          entity_id, None)
    return None


def _get_existing_knowledge(knowledge_base, entities_registry, entity_id,
                            name) -> dict:
    """What is already established (§7/§13-§15). The tool that answers
    "我们以前研究过什么" without touching the internet."""
    if knowledge_base is None:
        return {"status": "no_knowledge_base",
                "error": "no persistent knowledge base is wired; "
                         "existing knowledge lives there"}
    from crea.services.research_intelligence import assess_coverage

    entity = _resolve_target_entity(knowledge_base, entities_registry,
                                    entity_id, name)
    if entity is None:
        return {"status": "not_found", "entity_id": entity_id, "name": name}
    coverage = assess_coverage(knowledge_base, entity_id=entity.entity_id,
                               target=name or entity.entity_id)
    reactions = knowledge_base.search_reactions(entity_id=entity.entity_id)
    return {
        "status": "ok",
        "entity": _entity_view(entity),
        "note": "既有知识来自已记录来源（含 provenance/retrieved_at），"
                "不代表当前最新（§15）",
        "coverage": [f.model_dump(mode="json") for f in coverage.facets],
        "reactions": [_reaction_summary(r) for r in reactions],
        "papers": [{"paper_id": p.paper_id, "doi": p.doi, "title": p.title}
                   for p in knowledge_base.get_papers_for_entity(
                       entity.entity_id)],
        "routes": knowledge_base.get_routes_for_entity(entity.entity_id),
    }


def _get_knowledge_gaps(knowledge_base, entities_registry, entity_id,
                        name) -> dict:
    if knowledge_base is None:
        return {"status": "no_knowledge_base",
                "error": "no persistent knowledge base is wired"}
    from crea.services.research_intelligence import (
        assess_coverage, find_gaps,
    )

    entity = _resolve_target_entity(knowledge_base, entities_registry,
                                    entity_id, name)
    coverage = assess_coverage(
        knowledge_base,
        entity_id=entity.entity_id if entity is not None else (entity_id or None),
        target=name or entity_id or "")
    gaps = find_gaps(coverage)
    return {"status": "ok", "count": len(gaps),
            "gaps": [g.model_dump(mode="json") for g in gaps]}


def _search_knowledge(knowledge_base, kind, entity_id, reaction_id, paper_id,
                      name) -> dict:
    if knowledge_base is None:
        return {"status": "no_knowledge_base",
                "error": "no persistent knowledge base is wired"}
    if not any([entity_id, reaction_id, paper_id, name]):
        return {"status": "invalid_query",
                "error": "INVALID_QUERY: at least one filter is required "
                         "(entity_id, reaction_id, paper_id, name)"}
    kind = (kind or "").strip().lower()
    if kind == "entity":
        if name:
            found = knowledge_base.search_entities_by_name(name)
        elif entity_id:
            from crea.repositories.errors import NotFound

            try:
                found = [knowledge_base.get_entity(entity_id)]
            except NotFound:
                found = []
        else:
            return {"status": "invalid_query",
                    "error": "INVALID_QUERY: entity search needs name or "
                             "entity_id"}
        return {"status": "ok", "count": len(found),
                "results": [_entity_view(e) for e in found]}
    if kind == "reaction":
        try:
            found = knowledge_base.search_reactions(
                entity_id=entity_id, paper_id=paper_id)
        except ValueError as exc:
            return {"status": "invalid_query", "error": str(exc)}
        return {"status": "ok", "count": len(found),
                "results": [_reaction_summary(r) for r in found]}
    if kind == "paper":
        if paper_id:
            from crea.repositories.errors import NotFound

            try:
                found = [knowledge_base.get_paper(paper_id)]
            except NotFound:
                found = []
        elif entity_id:
            found = knowledge_base.get_papers_for_entity(entity_id)
        else:
            found = []
        return {"status": "ok", "count": len(found),
                "results": [{"paper_id": p.paper_id, "doi": p.doi,
                             "title": p.title, "year": p.year}
                            for p in found]}
    if kind == "evidence":
        if not reaction_id:
            return {"status": "invalid_query",
                    "error": "INVALID_QUERY: evidence search needs "
                             "reaction_id"}
        evidence = knowledge_base.get_evidence_for_reaction(reaction_id)
        return {"status": "ok", "count": len(evidence),
                "results": [{"evidence_id": e.evidence_id,
                             "raw_quote": e.raw_quote,
                             "source": e.source}
                            for e in evidence]}
    if kind == "record":
        if not reaction_id:
            return {"status": "invalid_query",
                    "error": "INVALID_QUERY: record search needs reaction_id"}
        records = knowledge_base.get_reaction_records(reaction_id)
        return {"status": "ok", "count": len(records),
                "results": [r.model_dump(mode="json") for r in records]}
    return {"status": "invalid_query",
            "error": f"INVALID_QUERY: unknown kind {kind!r}"}


def _synthesize_evidence(knowledge_base, entity_id, reaction_ids) -> dict:
    if knowledge_base is None:
        return {"status": "no_knowledge_base",
                "error": "no persistent knowledge base is wired"}
    from crea.repositories.errors import RepositoryError
    from crea.services.research_intelligence import synthesize_fields

    try:
        synthesis = synthesize_fields(
            knowledge_base, entity_id=entity_id,
            reaction_ids=reaction_ids or None)
    except RepositoryError as exc:
        return {"status": "invalid_query", "error": str(exc)}
    return {
        "status": "ok",
        "note": "这是各来源记录值的并置聚合，不是新事实；"
                "冲突保留双方出处，未平均、未选择（§22-§24）",
        "observations": [o.model_dump(mode="json")
                         for o in synthesis.observations],
        "conflicts": [c.model_dump(mode="json")
                      for c in synthesis.conflicts],
    }


def _validate_reaction(routes, validator, reactions, sink, step_id: str,
                       reaction_id: str) -> dict:
    """Measure a reaction against a route step and let the real services
    decide what that earns. The linker never promotes (§37/§57)."""
    from crea.services.route_reaction_linking import RouteReactionLinker

    if routes is None or not routes.routes:
        return {"status": "no_route", "error": "no candidate route is held"}
    _route, step = routes.find_step(step_id)
    if step is None:
        return {"status": "not_found", "error": f"no step {step_id!r}"}
    try:
        reaction = reactions.get(reaction_id)
    except KeyError:
        return {"status": "not_found", "reaction_id": reaction_id}

    linker = RouteReactionLinker()
    link = linker.link(step, reaction)
    record = next((r for r in (getattr(routes, "records", []) or [])
                   if r.reaction_id == reaction.record_id), None)
    if not link.screening_only and validator is not None:
        # only a measured link may put the record in front of the validator;
        # the validator decides the step's support level
        linker.stage(validator, reaction, record, evidence=sink)
        result = validator.validate(step)
        from crea.services.route_validation import RouteValidationService

        RouteValidationService.apply(result, step)
    return {
        "status": "ok",
        "link": {
            "step_id": link.step_id,
            "reaction_id": link.reaction_id,
            "match_type": link.match_type.value,
            "link_basis": link.link_basis.value,
            "screening_only": link.screening_only,
        },
        "step_status": step.status.value,
        "support_level": step.support_level.value,
    }


def _reaction_details(documents, sink: list, extractor, routes, document_id: str,
                      query: str, evidence_ids: list[str],
                      max_evidence: int) -> dict:
    """Evidence in, structured conditions out.

    The evidence is either named by the caller or found by a text search; either
    way it is evidence that already exists, and the extractor never goes
    looking for a source of its own.
    """
    from crea.services.document_evidence import search_document
    from crea.services.reaction_extraction import ReactionExtractor

    parsed = documents.get(document_id)
    if parsed is None:
        _require_document(documents, document_id)

    chosen: list = []
    by_id = {e.evidence_id: e for e in sink}
    if evidence_ids:
        missing = [r for r in evidence_ids if r not in by_id]
        if missing:
            from crea.core.errors import DocumentError
            from crea.core.models.document import DocumentErrorKind

            raise DocumentError(
                f"evidence not produced by this task: {', '.join(missing)}. "
                f"Cite evidence a tool actually returned.",
                kind=DocumentErrorKind.NOT_FOUND, path="")
        chosen = [by_id[r] for r in evidence_ids]
    else:
        text = query or "Experimental"
        chosen = [h.evidence for h in
                  search_document(parsed, text, max_hits=max_evidence)]

    if not chosen:
        return {"records": [], "outcome": "rejected",
                "notes": [f"no passage in {document_id} matched {query!r}"]}

    extractor = extractor or ReactionExtractor(None)
    result = extractor.extract(chosen, document_id=document_id)
    if routes is not None and result.records:
        # Held so `route_evidence_report` can attach them to a step later. A
        # record nobody kept could only be re-derived from the model's own
        # account of it, and that is a citation of the model.
        routes.add_records(result.records)
    return {
        "records": [r.model_dump(mode="json") for r in result.records],
        "outcome": result.outcome.value,
        "conflicts": [c.model_dump(mode="json") for c in result.conflicts],
        "rejected": [r.model_dump(mode="json") for r in result.rejected],
        "notes": result.notes,
        "document_id": document_id,
        "evidence_used": [e.evidence_id for e in chosen],
        "policy_version": result.policy_version,
    }


def _create_claim(sink: list, text: str, claim_type: str,
                  evidence_refs: list[str]) -> dict:
    """Build a Claim and check it against the evidence actually held."""
    from crea.core.errors import EvidenceIntegrityError

    claim = Claim.build(
        claim_id=content_hash(f"{claim_type}|{text}|{'|'.join(sorted(evidence_refs))}", 16),
        text=text,
        claim_type=claim_type,
        evidence_refs=list(evidence_refs),
    )
    validation = validate_claim(claim, EvidenceRegistry(
        items={e.evidence_id: e for e in sink}))
    if not validation.ok:
        validation.raise_if_invalid(claim)   # raises EvidenceIntegrityError
    return {
        "claim_id": claim.claim_id,
        "text": claim.text,
        "claim_type": claim.claim_type,
        "evidence_refs": claim.evidence_refs,
        "weak_refs": validation.weak_refs,
    }

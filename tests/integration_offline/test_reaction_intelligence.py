"""Reaction intelligence end to end: extraction to report.

Phase 3.9. The chain under test is the phase's whole thesis:

    Document -> Evidence -> (scripted extraction) -> ReactionRecord
        -> ChemicalMention -> ChemicalEntity -> Reaction
        -> Evidence / Paper -> RouteStep link -> ResearchReport

Every step is deterministic except the one that is allowed to propose: the
extraction model suggests a record and the policy decides what survives.
Nothing here consults a model for a fact, and nothing unproven gets
promoted -- the tests below assert the refusals as firmly as the successes.
"""

from __future__ import annotations

import json
import pathlib

from crea.core.models.entity import ChemicalMention
from crea.core.models.reaction import ReactionRecord
from crea.core.models.reaction_linking import LinkBasis, ReactionEvidenceLevel
from crea.core.models.reaction_knowledge import ReactionStatus
from crea.core.models.research import TaskStatus
from crea.core.models.route import ReactionMatchType, StepStatus
from crea.report.research import render_report, render_research_report
from crea.services.document_evidence import search_document
from crea.services.evidence import EvidenceRegistry
from crea.services.entity_registry import ChemicalEntityRegistry
from crea.services.identity import IdentityResolutionService
from crea.services.mention_resolution import ChemicalMentionResolver
from crea.services.reaction_builder import ReactionBuilder, ReactionRegistry
from crea.services.reaction_extraction import ReactionExtractor
from crea.services.route_evidence import (
    RouteEvidenceInputs,
    RouteEvidenceService,
    build_record_link,
)
from crea.services.route_validation import RouteValidationService
from crea.services.route_reaction_linking import RouteReactionLinker
from tests.fakes import pdfs
from tests.fakes.compound import PHENYLACETYLENE, FakeCompoundProvider


def scripted_payload(evidence_id: str) -> str:
    """A minimal valid extraction proposal for the fixture procedure.

    Only resolvable component names, and every number taken from the quote
    the evidence carries -- the policy checks both. (The fuller payload in
    test_reaction_tool names CuI/DBU/DMF too; those names have no entry in
    the test double, and a reaction with unresolvable components is PARTIAL
    -- correct, and covered in the builder unit tests.)
    """
    return json.dumps({
        "description": "coupling of phenylacetylene",
        "description_evidence_refs": [evidence_id],
        "components": [
            {"name": "phenylacetylene", "value": "phenylacetylene",
             "component_role": "reactant", "unit": "", "status": "EXPLICIT",
             "evidence_refs": [evidence_id],
             "raw_text": "Phenylacetylene (1.0 mmol),"},
        ],
        "conditions": [
            {"name": "temperature", "value": 80, "raw_value": "80",
             "unit": "C", "raw_unit": "C", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": "stirred at 80 C"},
            {"name": "yield_value", "value": 82, "raw_value": "82",
             "unit": "%", "raw_unit": "%", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": "82% yield"},
        ],
        "yield_type": "unknown",
        "yield_type_evidence_refs": [],
        "raw_experimental_text": "",
    })


class ScriptedModel:
    """An extraction model that always answers with the same proposal."""

    name = "scripted-extractor"
    model = "scripted-1"

    def __init__(self, text: str) -> None:
        self._text = text

    def complete(self, request):
        from crea.llm.base import LLMResponse

        return LLMResponse(text=self._text, provider=self.name, model=self.model)

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"
PHENYL = "C#Cc1ccccc1"
COUPLING_REACTANTS = [PHENYL, PHENYL]


def _stack(tmp_path):
    """Document + evidence + extraction + entities + builder: real services,
    with only the extraction model scripted (the established pattern: the
    model proposes, the policy validates)."""
    from crea.agent.workspace import DocumentLibrary
    from crea.services.documents import LocalDocumentService
    from crea.sources.pdf import PypdfDocumentParser

    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    path = tmp_path / "procedure.pdf"
    path.write_bytes(pdfs.make_pdf(pdfs.EXPERIMENTAL))
    parsed = library.ingest(path)
    document_id = parsed.document.document_id

    evidence = search_document(parsed, "CuI")[0].evidence
    sink = [evidence]

    entities = ChemicalEntityRegistry()
    resolver = ChemicalMentionResolver(
        entities,
        identity=IdentityResolutionService(compound_provider=FakeCompoundProvider(
            by_name={"phenylacetylene": [PHENYLACETYLENE]})),
        held_evidence=sink)
    builder = ReactionBuilder(resolver, entity_registry=entities,
                              held_evidence=sink)
    extractor = ReactionExtractor(
        ScriptedModel(scripted_payload(evidence.evidence_id)))
    return {
        "document_id": document_id, "evidence": evidence, "sink": sink,
        "entities": entities, "resolver": resolver, "builder": builder,
        "extractor": extractor, "parsed": parsed,
    }


def _extract_record(ctx) -> ReactionRecord:
    """Evidence -> (scripted proposal) -> policy-validated ReactionRecord."""
    hits = search_document(ctx["parsed"], "CuI", max_hits=4)
    result = ctx["extractor"].extract([h.evidence for h in hits],
                                      document_id=ctx["document_id"])
    assert result.records, result.notes
    return result.records[0]


def _product(text="the product", **kwargs) -> ChemicalMention:
    return ChemicalMention(mention_id="prod-" + text.replace(" ", "-"),
                           raw_text=text, normalized_text=text.lower(),
                           **kwargs)


def _coupling_step():
    """The route step the fixture proposes: 2 phenylacetylene -> diyne."""
    from crea.agent.offline import build_offline_workflow_stack

    _workflow, _library, _sink, registry = build_offline_workflow_stack()
    result = registry.execute("retrosynthesize", {"target_smiles": DIYNE})
    assert result.status.value == "ok"
    step = next(s for r in registry.routes.routes for s in r.steps
                if [m.canonical_smiles for m in s.reactants] == COUPLING_REACTANTS)
    return registry, step


# ---------------------------------------------------------------------------
# extraction -> record -> reaction -> entity
# ---------------------------------------------------------------------------


def test_extraction_to_reaction_to_entity(tmp_path):
    """The model proposes; the policy validates; the builder joins."""
    ctx = _stack(tmp_path)
    record = _extract_record(ctx)

    # the record survived policy validation: numbers from the quote only
    assert record.conditions.temperature.value == 80
    assert record.conditions.pressure.status.value == "not_found"

    reaction = ctx["builder"].build(
        record, paper_id="paper-a", doi="10.0000/synthetic.2026.0001",
        product_mentions=[_product("the product", explicit_smiles=DIYNE)])

    phenyl = next(c for c in reaction.reactants if c.name == "phenylacetylene")
    assert phenyl.entity_id == "CHEM-00000001"
    assert reaction.products[0].entity_id == "CHEM-00000002"
    assert reaction.status is ReactionStatus.EXPERIMENTALLY_SUPPORTED
    assert reaction.evidence_refs == [ctx["evidence"].evidence_id]


def test_the_same_name_is_one_entity_across_reactions(tmp_path):
    """Two records citing phenylacetylene are two reactions with one
    CHEM-id (§61: the registry reuses identity keys)."""
    ctx = _stack(tmp_path)
    registry = ReactionRegistry()
    record = _extract_record(ctx)

    first = ctx["builder"].build(
        record, paper_id="paper-a",
        product_mentions=[_product("the product", explicit_smiles=DIYNE)])
    second = ctx["builder"].build(
        record, paper_id="paper-b",
        product_mentions=[_product("the product", explicit_smiles=DIYNE)])
    registry.register(first)
    registry.register(second)

    assert len(registry) == 2          # two reports, two reactions
    ids = {c.entity_id for r in (first, second) for c in r.reactants}
    assert ids == {"CHEM-00000001"}


def test_one_paper_and_one_document_hold_many_reactions(tmp_path):
    """§18/§19: paper -> R1..R3, document -> R1..R3. Never one-to-one."""
    ctx = _stack(tmp_path)
    registry = ReactionRegistry()
    record = _extract_record(ctx)

    # three genuinely different reactions of one paper: the dedup rule
    # merges identical reports and must not swallow distinct chemistry
    distinct_products = [DIYNE, "CCO", "CCOC"]
    for index, smiles in enumerate(distinct_products):
        reaction = ctx["builder"].build(
            record.model_copy(update={"reaction_id": f"rxn-{index}"}),
            paper_id="paper-a",
            product_mentions=[_product(f"product {index}",
                                       explicit_smiles=smiles)])
        registry.register(reaction)

    held = registry.find_by_paper("paper-a")
    assert len(held) == 3
    assert all(r.document_id == ctx["document_id"] for r in held)


# ---------------------------------------------------------------------------
# reaction -> route (§36-§38)
# ---------------------------------------------------------------------------


def test_an_exact_match_can_lift_a_step_through_the_existing_services(tmp_path):
    """RouteStep -> Reaction -> Literature Evidence, with the support level
    decided by RouteValidationService -- never by this linker."""
    ctx = _stack(tmp_path)
    record = _extract_record(ctx)
    reaction = ctx["builder"].build(
        record, paper_id="paper-a",
        product_mentions=[_product("the product", explicit_smiles=DIYNE)])

    workbench_registry, step = _coupling_step()
    linker = RouteReactionLinker()
    link = linker.link(step, reaction)

    assert link.match_type is ReactionMatchType.EXACT
    assert link.link_basis is LinkBasis.EXACT_STRUCTURE
    assert link.screening_only is False

    # the existing validation service decides the step's support: its
    # structure matching is step-scoped, so this step is lifted and the
    # record is not pooled onto steps it says nothing about (§37)
    validator = RouteValidationService(evidence=ctx["sink"])
    linker.stage(validator, reaction, record, evidence=ctx["sink"])
    result = validator.validate(step)
    RouteValidationService.apply(result, step)
    # a document-sourced structure that matches this step exactly is the
    # service's own USER_DOCUMENT_SUPPORT -> EXPERIMENTALLY_SUPPORTED path
    assert step.status is StepStatus.EXPERIMENTALLY_SUPPORTED

    # and the existing evidence ladder decides the rung
    link_record = build_record_link(
        step, record.reaction_id,
        COUPLING_REACTANTS, [DIYNE], list(record.evidence_refs))
    service = RouteEvidenceService(
        EvidenceRegistry(items={e.evidence_id: e for e in ctx["sink"]}),
        RouteEvidenceInputs(candidates=[], records=[record],
                            record_links=[link_record]))
    chain = service.build(workbench_registry.routes.routes[0])
    assert chain.step(step.step_id).evidence_level is (
        ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT)


def test_a_different_transformation_is_screening_only_and_lifts_nothing(tmp_path):
    """§35/§37: similarity is discovery, never proof of equivalence."""
    ctx = _stack(tmp_path)
    record = _extract_record(ctx)
    other = ctx["builder"].build(
        record.model_copy(update={"reaction_id": "rxn-other"}),
        paper_id="paper-a",
        product_mentions=[_product("something else", explicit_smiles="CCO")])
    _registry, step = _coupling_step()

    link = RouteReactionLinker().link(step, other)

    assert link.match_type is not ReactionMatchType.EXACT
    assert link.link_basis is not LinkBasis.EXACT_STRUCTURE
    assert link.screening_only is True
    assert any("screening only" in note for note in link.notes)
    assert step.status is StepStatus.CANDIDATE


# ---------------------------------------------------------------------------
# the full offline chain -> report (§69)
# ---------------------------------------------------------------------------


def test_the_offline_chain_ends_in_a_report(tmp_path):
    """Paper -> Mention -> Entity -> Reaction -> Evidence -> Route -> Report."""
    ctx = _stack(tmp_path)
    record = _extract_record(ctx)
    reaction = ctx["builder"].build(
        record, paper_id="paper-a", doi="10.0000/synthetic.2026.0001",
        product_mentions=[_product("the product", explicit_smiles=DIYNE)])
    reaction_registry = ReactionRegistry()
    kept, merged = reaction_registry.register(reaction)
    assert merged is False

    # route linkage through the existing services
    workbench_registry, step = _coupling_step()
    linker = RouteReactionLinker()
    link = linker.link(step, reaction)
    validator = RouteValidationService(evidence=ctx["sink"])
    linker.stage(validator, reaction, record)
    result = validator.validate(step)
    RouteValidationService.apply(result, step)

    # the research report, rendered from what the chain produced
    report = render_report(
        question="analyze the procedure",
        interpretation="document to reaction to route",
        claims=[],
        evidence=ctx["sink"],
        sources=[],
        unresolved=list(reaction.notes),
        steps=[], tool_calls=0,
        status=TaskStatus.COMPLETED,
        computed_notes=[],
        reaction_details=[record],
        routes=workbench_registry.routes.routes,
        route_notes=[],
        route_evidence=workbench_registry.routes.evidence_reports or [],
    )
    markdown = render_research_report(report)

    # the report carries the chain's products
    assert "| Temperature | 80 C |" in markdown
    assert "## 6. Retrosynthesis" in markdown
    assert kept.status is ReactionStatus.EXPERIMENTALLY_SUPPORTED
    assert link.match_type is ReactionMatchType.EXACT
    assert step.status is StepStatus.EXPERIMENTALLY_SUPPORTED
    # and every provenance question is answerable (§62)
    assert kept.paper_id == "paper-a"
    assert kept.document_id == ctx["document_id"]
    assert kept.evidence_refs
    assert all(c.mention is not None for c in kept.components())

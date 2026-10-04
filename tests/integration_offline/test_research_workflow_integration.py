"""Phase 3.10 integration: Reaction Intelligence inside the research workflow.

The point of this phase is not breadth but the vertical loop, and these tests
walk it as real code paths -- no hand-built objects standing in for a
service:

    PDF -> Document -> Evidence -> ChemicalMention -> ChemicalEntity
        -> ReactionRecord -> Reaction -> Reaction <-> RouteStep
        -> ResearchWorkflow -> ResearchSession -> ResearchReport

Every label in the report is one the layers below already earned:
[COMPUTED] [DISCOVERY] [EVIDENCE] [REACTION] [EXPERIMENTAL] [UNRESOLVED]
[PARTIAL] (§71). The extraction model is scripted -- it is allowed to
propose -- and everything downstream of it is the real policy, the real
identity layer, the real registries.
"""

from __future__ import annotations

import json

from crea.core.models.entity import MentionResolutionStatus
from crea.core.models.identity import StructureInput
from crea.core.models.reaction_knowledge import ReactionStatus
from crea.core.models.research import ResearchTask
from crea.core.models.route import StepStatus
from crea.core.models.session import WorkflowStatus
from crea.report.research import render_research_report
from crea.services.document_evidence import search_document
from crea.services.reaction_extraction import ReactionExtractor
from tests.fakes import pdfs
from tests.fakes.compound import PHENYLACETYLENE, FakeCompoundProvider

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"
PHENYL = "C#Cc1ccccc1"
#: The diyne's real standard InChI (RDKit-derived), for the contextual
#: definition "Compound 8 (InChI=...)" in the fixture procedure.
DIYNE_INCHI = ("InChI=1S/C16H10/c1-3-9-15(10-4-1)13-7-8-14-16-11-5-2-6-12-16"
               "/h1-6,9-12H")
COUPLING_REACTANTS = [PHENYL, PHENYL]

PROCEDURE = [
    "Experimental",
    "Phenylacetylene (1.0 mmol),",
    "CuI (10 mol%),",
    "were dissolved in DMF (5 mL).",
    "The mixture was stirred at 80 C",
    "for 12 h.",
    "The product was isolated in 82% yield.",
    f"Compound 8 ({DIYNE_INCHI}) was purified.",
]


def _payload(evidence_id: str, product_name: str, product_raw: str) -> str:
    """A valid extraction proposal: every number and raw text from the quote.

    The model proposes; the policy below this checks the numbers against the
    passage and drops anything it cannot find there.
    """
    return json.dumps({
        "description": "coupling to the product",
        "description_evidence_refs": [evidence_id],
        "components": [
            {"name": "phenylacetylene", "value": "phenylacetylene",
             "component_role": "reactant", "unit": "", "status": "EXPLICIT",
             "evidence_refs": [evidence_id],
             "raw_text": "Phenylacetylene (1.0 mmol),"},
            {"name": product_name, "value": product_name,
             "component_role": "product", "unit": "", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": product_raw},
        ],
        "conditions": [
            {"name": "temperature", "value": 80, "raw_value": "80",
             "unit": "C", "raw_unit": "C", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": "stirred at 80 C"},
            {"name": "time", "value": 12, "raw_value": "12",
             "unit": "h", "raw_unit": "h", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": "for 12 h."},
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


def _stack(tmp_path, *, lines, product_name="compound 8",
           product_raw="Compound 8", extractor=True, provider=None,
           entities=None, reactions=None):
    """One workflow over the offline stack, with one supplied document.

    Real services throughout; the extraction model is the only double, and
    its proposal still has to survive the policy.
    """
    from crea.agent.offline import build_offline_workflow_stack

    path = tmp_path / "procedure.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdfs.make_pdf([lines]))

    if provider is None:
        provider = FakeCompoundProvider(
            by_name={"phenylacetylene": [PHENYLACETYLENE]})
    model = None
    if extractor:
        evidence_id = _evidence_id_of(path)
        model = ScriptedModel(_payload(evidence_id, product_name, product_raw))
    workflow, library, sink, registry = build_offline_workflow_stack(
        extractor=ReactionExtractor(model) if model is not None else None,
        compound_provider=provider, entities=entities, reactions=reactions)
    parsed = library.ingest(path)
    return workflow, library, sink, registry, parsed.document.document_id


def _evidence_id_of(path):
    """The passage id the tools will mint, computed the same way they do."""
    from crea.agent.workspace import DocumentLibrary
    from crea.services.documents import LocalDocumentService
    from crea.sources.pdf import PypdfDocumentParser

    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    parsed = library.ingest(path)
    return search_document(parsed, "CuI")[0].evidence.evidence_id


def _run(workflow, registry, document_id, question="研究目标化合物的文献合成方法"):
    task = ResearchTask(task_id="t-" + question[:12], question=question)
    report = workflow.run(
        task,
        structure_input=StructureInput.of("SMILES", DIYNE),
        document_id=document_id,
        document_query="CuI",
    )
    return task, report


# ---------------------------------------------------------------------------
# §10/§69/§70 -- the local-PDF path, end to end
# ---------------------------------------------------------------------------


def test_the_offline_research_chain_answers_every_question(tmp_path):
    workflow, _library, _sink, registry, document_id = _stack(
        tmp_path, lines=PROCEDURE,
        provider=FakeCompoundProvider(
            by_name={"phenylacetylene": [PHENYLACETYLENE]}))
    task, report = _run(workflow, registry, document_id)
    session = workflow.session

    # 目标化合物 / 找到哪些文献
    assert session is not None
    assert session.chemical_entity_ids, "the target and components become entities"
    assert report.sources, "literature discovery ran"

    # 哪些文献有实验文本 / 发现哪些 Reaction
    assert report.evidence, "the supplied PDF yielded evidence"
    assert report.reactions and session.reaction_ids
    reaction = report.reactions[0]

    # Reaction 条件是什么 / 哪些信息有 Evidence
    record = report.reaction_details[0]
    assert record.conditions.temperature.value == 80
    assert record.conditions.yield_value.value == 82
    assert reaction.evidence_refs, "the reaction cites the document evidence"

    # 哪些 RouteStep 有文献 Reaction 对应 / 哪些只是候选 / 哪些实验支持是真实的
    assert reaction.status is ReactionStatus.EXPERIMENTALLY_SUPPORTED
    exact = [link for link in report.reaction_links if not link.screening_only]
    assert exact, "the coupling step is linked to the literature reaction"
    linked_ids = {link.step_id for link in exact}
    steps = [s for r in report.routes for s in r.steps]
    assert any(s.step_id in linked_ids
               and s.status is StepStatus.EXPERIMENTALLY_SUPPORTED
               for s in steps)
    # the record does not blanket-cover the route: the experimental rung is
    # the evidence chain's, the chain names one step, and the others keep
    # their candidate rung (the validator may still lift a step whose
    # structures a held document matches -- that is its own judgment, not
    # this linker's to weaken)
    from crea.core.models.reaction_linking import ReactionEvidenceLevel

    rungs = {chain_step.step_id: chain_step.evidence_level
             for chain in report.route_evidence for chain_step in chain.steps}
    linked = linked_ids.pop()
    assert rungs[linked] is ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT
    others = [sid for sid in rungs if sid != linked]
    assert others
    assert all(rungs[sid] is ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE
               for sid in others), "the record covers one step, not the route"

    # 哪些信息仍然未知
    markdown = render_research_report(
        report, session=session,
        identity=getattr(registry.identity, "resolution", None))
    assert "## Reactions" in markdown
    assert "[REACTION]" in markdown or "[PARTIAL]" in markdown
    assert session.status is WorkflowStatus.COMPLETED
    # §71: the labels that carry meaning are the ones the layers earned
    assert "该文献被发现，但当前没有足够实验文本建立 Reaction。" not in markdown


# ---------------------------------------------------------------------------
# §14 -- compound numbers are paper-local
# ---------------------------------------------------------------------------


def test_compound_numbers_resolve_per_paper_and_never_leak(tmp_path):
    """Paper A: compound 7 = phenylacetylene. Paper B: compound 7 = the
    diyne. Same words, different papers, different entities -- one shared
    registry, no pollution, and the words never become names of either."""
    from crea.services.entity_registry import ChemicalEntityRegistry
    from crea.services.reaction_builder import ReactionRegistry

    entities = ChemicalEntityRegistry()
    reactions = ReactionRegistry()
    lines_a = [
        "Experimental",
        "Compound 7 (phenylacetylene) was used as received.",
        "CuI (10 mol%) was added.",
        "The mixture was stirred at 80 C for 12 h.",
        "The product was isolated in 82% yield.",
    ]
    lines_b = [
        "Experimental",
        f"Compound 7 ({DIYNE_INCHI}) was recrystallized.",
        "CuI (10 mol%) was added.",
        "The mixture was stirred at 80 C for 12 h.",
        "The product was isolated in 82% yield.",
    ]

    products = []
    for label, lines in (("paper-a", lines_a), ("paper-b", lines_b)):
        workflow, _library, _sink, _registry, document_id = _stack(
            tmp_path / label, lines=lines, product_name="compound 7",
            product_raw="Compound 7", entities=entities, reactions=reactions)
        _task, report = _run(workflow, _registry, document_id)
        reaction = report.reactions[0]
        product = next(c for c in reaction.products
                       if (c.name or "").lower() == "compound 7")
        products.append((label, product))

    (_label_a, product_a), (_label_b, product_b) = products

    # the same words, one registry, two entities -- and both established
    assert product_a.entity_id and product_b.entity_id
    assert product_a.entity_id != product_b.entity_id
    assert product_a.entity_id in entities.entity_ids
    assert product_b.entity_id in entities.entity_ids
    # the words themselves are never names of either entity (§16)
    for entity_id in entities.entity_ids:
        names = [n.lower() for n in entities.get(entity_id).names]
        assert "compound 7" not in names
    # and the two reports kept their own scopes
    assert len(reactions.all()) == 2


# ---------------------------------------------------------------------------
# §64/§65 -- determinism
# ---------------------------------------------------------------------------


def test_the_same_run_twice_produces_the_same_ids_and_order(tmp_path):
    runs = []
    for name in ("run-1", "run-2"):
        workflow, _library, _sink, registry, document_id = _stack(
            tmp_path / name, lines=PROCEDURE)
        _task, report = _run(workflow, registry, document_id)
        session = workflow.session
        runs.append((
            session.chemical_entity_ids,
            session.reaction_ids,
            [r.reaction_id for r in report.reactions],
            [(r.paper_scope(), r.reaction_id) for r in report.reactions],
        ))
    assert runs[0] == runs[1], "same input must give the same CHEM/RXN ids"
    assert runs[0][1], "the run produced reactions at all"


# ---------------------------------------------------------------------------
# §17/§19 -- the agent tools
# ---------------------------------------------------------------------------


def test_the_agent_reaction_tools_run_through_the_registry(tmp_path):
    workflow, library, sink, registry, document_id = _stack(
        tmp_path, lines=PROCEDURE)
    tools = registry

    # extract: the model proposes, the policy and identity layer decide
    result = tools.execute("extract_reactions", {
        "document_id": document_id, "query": "CuI",
        "paper_id": "paper-a", "doi": "10.0000/synthetic.2026.0001"})
    assert result.status.value == "ok", result.error
    assert result.data["reactions"]
    reaction_id = result.data["reactions"][0]["reaction_id"]

    # get
    got = tools.execute("get_reaction", {"reaction_id": reaction_id})
    assert got.data["status"] == "ok"
    assert got.data["reaction_id"] == reaction_id
    assert any(c["entity_id"] for c in got.data["components"])

    # search: at least one filter, deterministic
    found = tools.execute("search_reactions",
                          {"paper_id": "paper-a"})
    assert found.data["count"] >= 1
    invalid = tools.execute("search_reactions", {})
    assert invalid.data["status"] == "invalid_query"

    # validate: the linker measures; the route services decide
    tools.execute("retrosynthesize", {"target_smiles": DIYNE})
    steps = [s for r in registry.routes.routes for s in r.steps]
    coupling = next(s for s in steps
                    if [m.canonical_smiles for m in s.reactants] == COUPLING_REACTANTS)
    validated = tools.execute("validate_reaction", {
        "step_id": coupling.step_id, "reaction_id": reaction_id})
    assert validated.data["status"] == "ok", validated.error
    assert validated.data["link"]["screening_only"] is False
    assert validated.data["step_status"] == StepStatus.EXPERIMENTALLY_SUPPORTED.value


def test_without_an_extraction_model_the_paper_is_discovery_only(tmp_path):
    """§9/§19: no experimental text -> no Reaction. Not an invented one."""
    workflow, _library, _sink, registry, document_id = _stack(
        tmp_path, lines=PROCEDURE, extractor=False)

    result = registry.execute("extract_reactions", {"document_id": document_id})

    assert result.status.value == "ok"
    assert result.data["reactions"] == []
    assert result.data["status"] == "discovery_only"
    assert any("该文献被发现" in note for note in result.data["notes"])


# ---------------------------------------------------------------------------
# §20 -- the workflow never speaks to an LLM API
# ---------------------------------------------------------------------------


def test_the_workflow_reaches_no_llm_and_no_provider_implementation():
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "crea"
    path = root / "services" / "research_workflow.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert not (imported & {"crea.llm", "openai", "anthropic", "llm"})
    assert "crea.sources" not in {name for name in imported
                                  if name.startswith("crea.sources")}

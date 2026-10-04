"""`get_reaction_details` through the tool registry, and through the agent.

The extraction model is scripted; everything else is real. What is under test
is the plumbing between a tool result and a report -- that the tool is the only
way a ReactionRecord can appear, that a model cannot describe its way into one,
and that what reaches the report is what survived validation.
"""

import json
import pathlib
import tempfile

import pytest

from crea.agent.loop import ResearchAgent
from crea.agent.toolsets import build_registry
from crea.agent.workspace import DocumentLibrary
from crea.core.errors import DocumentError
from crea.core.models.research import ActionKind, AgentAction
from crea.llm.base import LLMResponse
from crea.llm.fake import FakeLLMProvider
from crea.services.document_evidence import search_document
from crea.services.documents import LocalDocumentService
from crea.services.literature import LiteratureService
from crea.services.reaction_extraction import ReactionExtractor
from crea.sources.pdf import PypdfDocumentParser
from tests.fakes import pdfs

QUESTION = "从这篇 PDF 中提取该反应的实验条件。"


def scripted_payload(evidence_id: str, **overrides) -> str:
    """A model answering correctly: every field cites evidence, every number is
    in the quote, and nothing is added."""
    payload = {
        "description": "CuI-catalyzed coupling of phenylacetylene",
        "description_evidence_refs": [evidence_id],
        "components": [
            {"name": "phenylacetylene", "value": "phenylacetylene",
             "component_role": "reactant", "unit": "", "status": "EXPLICIT",
             "evidence_refs": [evidence_id],
             "raw_text": "Phenylacetylene (1.0 mmol),"},
            {"name": "CuI", "value": "CuI", "component_role": "catalyst",
             "unit": "", "status": "EXPLICIT", "evidence_refs": [evidence_id],
             "raw_text": "CuI (10 mol%),"},
            {"name": "DBU", "value": "DBU", "component_role": "reagent",
             "unit": "", "status": "EXPLICIT", "evidence_refs": [evidence_id],
             "raw_text": "DBU (2.0 equiv)"},
            {"name": "DMF", "value": "DMF", "component_role": "solvent",
             "unit": "", "status": "EXPLICIT", "evidence_refs": [evidence_id],
             "raw_text": "were dissolved in DMF (5 mL)."},
        ],
        "conditions": [
            {"name": "temperature", "value": 80, "raw_value": "80", "unit": "C",
             "raw_unit": "C", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": "stirred at 80 C"},
            {"name": "time", "value": 12, "raw_value": "12", "unit": "h",
             "raw_unit": "h", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": "for 12 h."},
            {"name": "yield_value", "value": 82, "raw_value": "82", "unit": "%",
             "raw_unit": "%", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": "isolated in 82% yield."},
        ],
        "yield_type": "isolated",
        "yield_type_evidence_refs": [evidence_id],
        "raw_experimental_text": "",
    }
    payload.update(overrides)
    return json.dumps(payload)


class ScriptedModel:
    """An `LLMProvider` that answers every turn with the same extraction."""

    name = "scripted-extractor"
    model = "scripted-1"

    def __init__(self, text: str) -> None:
        self._text = text

    def complete(self, request):
        return LLMResponse(text=self._text, provider=self.name, model=self.model)


@pytest.fixture
def document():
    directory = pathlib.Path(tempfile.mkdtemp(prefix="crea-rxn-tool-"))
    path = directory / "procedure.pdf"
    path.write_bytes(pdfs.make_pdf(pdfs.EXPERIMENTAL))
    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    library.ingest(path)
    return library


@pytest.fixture
def evidence(document):
    return search_document(document.all()[0], "CuI")[0].evidence


def stack(document, model, sink):
    registry = build_registry(LiteratureService({}), document, sink,
                              extractor=ReactionExtractor(model))
    return registry


# ===========================================================================
# the tool
# ===========================================================================


def test_the_tool_extracts_and_validates(document, evidence):
    sink: list = []
    model = ScriptedModel(scripted_payload(evidence.evidence_id))
    registry = stack(document, model, sink)

    result = registry.execute("get_reaction_details", {
        "document_id": document.all()[0].document.document_id,
        "query": "CuI",
    })
    assert result.status.value == "ok", result.error
    record = result.data["records"][0]
    conditions = record["conditions"]

    assert [c["name"] for c in conditions["reactants"]] == ["phenylacetylene"]
    assert [c["name"] for c in conditions["catalysts"]] == ["CuI"]
    assert [c["name"] for c in conditions["reagents"]] == ["DBU"]
    assert [c["name"] for c in conditions["solvents"]] == ["DMF"]
    assert conditions["temperature"]["value"] == 80
    assert conditions["time"]["value"] == 12
    assert conditions["yield_value"]["value"] == 82
    assert conditions["yield_type"] == "isolated"
    assert conditions["pressure"]["status"] == "not_found"
    assert result.data["evidence_used"]


def test_the_tool_reports_what_it_rejected(document, evidence):
    """A model that invents a pressure gets its pressure rejected, not kept."""
    sink: list = []
    payload = json.loads(scripted_payload(evidence.evidence_id))
    payload["conditions"].append({
        "name": "pressure", "value": 1, "raw_value": "1", "unit": "atm",
        "raw_unit": "atm", "status": "EXPLICIT",
        "evidence_refs": [evidence.evidence_id], "raw_text": "at 1 atm",
    })
    model = ScriptedModel(json.dumps(payload))
    registry = stack(document, model, sink)

    result = registry.execute("get_reaction_details", {
        "document_id": document.all()[0].document.document_id, "query": "CuI"})
    record = result.data["records"][0]
    # `stated` is a property; what survives serialisation is the status itself
    assert record["conditions"]["pressure"]["status"] == "not_found"
    assert record["conditions"]["pressure"]["value"] is None
    assert any("pressure" in r["field"] for r in result.data["rejected"])


def test_the_tool_refuses_an_evidence_id_the_task_never_produced(document):
    sink: list = []
    registry = stack(document, ScriptedModel("{}"), sink)
    result = registry.execute("get_reaction_details", {
        "document_id": document.all()[0].document.document_id,
        "evidence_ids": ["invented-id"],
    })
    assert result.status.value == "error"
    assert "not produced by this task" in result.error
    assert result.data is None


def test_the_tool_rejects_a_missing_document(document):
    registry = stack(document, ScriptedModel("{}"), [])
    result = registry.execute("get_reaction_details", {"document_id": "nope"})
    assert result.status.value == "error"


def test_the_tool_rejects_malformed_arguments(document):
    registry = stack(document, ScriptedModel("{}"), [])
    result = registry.execute("get_reaction_details", {"document_id": ""})
    assert result.status.value == "rejected"


# ===========================================================================
# the agent
# ===========================================================================


def test_the_agent_carries_the_record_into_the_report(document, evidence):
    sink: list = []
    model = ScriptedModel(scripted_payload(evidence.evidence_id))
    registry = stack(document, model, sink)
    llm = FakeLLMProvider([
        AgentAction(action=ActionKind.TOOL_CALL, tool="get_document_evidence",
                    arguments={"document_id": document.all()[0].document.document_id,
                               "query": "CuI", "max_evidence": 1}),
        AgentAction(action=ActionKind.TOOL_CALL, tool="get_reaction_details",
                    arguments={"document_id": document.all()[0].document.document_id}),
        AgentAction(action=ActionKind.FINAL, summary="实验条件已提取。"),
    ])
    agent = ResearchAgent(llm=llm, registry=registry, documents=document,
                          evidence=sink)
    task = agent.run(QUESTION)

    assert task.report.reaction_details
    record = task.report.reaction_details[0]
    assert record.conditions.temperature.value == 80
    assert record.conditions.yield_value.value == 82
    assert record.conditions.pressure.stated is False

    from crea.report.research import format_report

    text = format_report(task.report)
    assert "## 实验条件" in text
    assert "80 C" in text
    assert "未在提供的 Experimental 证据中说明" in text


def test_a_model_cannot_describe_conditions_into_existence(document):
    """It may finish with prose about the reaction. It may not produce a record."""
    sink: list = []
    llm = FakeLLMProvider([
        AgentAction(action=ActionKind.FINAL,
                    summary="The reaction used CuI at 80 C in 82% yield.",
                    claim_specs=[{
                        "text": "该反应在 80 C、82% 收率下进行。",
                        "claim_type": "experimental_fact",
                        "from_tool": "get_reaction_details"}]),
    ])
    registry = stack(document, ScriptedModel("{}"), sink)
    agent = ResearchAgent(llm=llm, registry=registry, documents=document,
                          evidence=sink)
    task = agent.run(QUESTION)

    assert task.report.reaction_details == []
    assert all(f.kind != "evidence_backed" for f in task.report.findings)


def test_two_procedures_are_listed_not_merged_or_ranked(document):
    """The comparison case: two procedures, side by side, no verdict on which is
    better. Ranking is a later phase and deliberately absent."""
    directory = pathlib.Path(tempfile.mkdtemp(prefix="crea-rxn-two-"))
    path = directory / "two.pdf"
    path.write_bytes(pdfs.make_pdf(pdfs.TWO_PROCEDURES))
    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    library.ingest(path)

    parsed = library.all()[0]
    first = search_document(parsed, "Procedure A")[0].evidence
    second = search_document(parsed, "Procedure B")[0].evidence
    assert first.evidence_id != second.evidence_id

    sink = [first, second]
    registry = stack(library, ScriptedModel("{}"), sink)
    result = registry.execute("get_reaction_details", {
        "document_id": parsed.document.document_id,
        "evidence_ids": [first.evidence_id, second.evidence_id],
    })
    # nothing was proposed, so nothing was invented
    assert result.data["records"] == []
    assert result.data["outcome"] == "rejected"
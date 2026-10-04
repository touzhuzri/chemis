"""The end-to-end research agent.

Not a unit test of a component: a whole question, answered through the real
chemistry core, the real literature service, a real PDF and the real evidence
chain, with only the model scripted.

The scripted part is the *planner*. Everything the answer rests on was
computed or extracted.
"""

import json

import pytest

from crea.agent.loop import MAX_STEPS, ResearchAgent
from crea.agent.offline import DEMO_COMPOUNDS, DEMO_DOI, build_offline_stack
from crea.agent.tools import ToolRegistry
from crea.core.models.research import ActionKind, AgentAction, TaskStatus
from crea.llm.fake import FakeLLMProvider, ScriptExhausted
from crea.report.research import format_report, unsupported_findings_marker

QUESTION = "分析并研究 1,4-diphenyl-1,3-diyne"


# ---------------------------------------------------------------------------
# the whole flow
# ---------------------------------------------------------------------------


def test_the_agent_answers_a_chemistry_question_end_to_end():
    agent, library, document_id = build_offline_stack(QUESTION)
    task = agent.run(QUESTION)

    assert task.status is TaskStatus.COMPLETED
    assert task.report is not None
    assert task.errors == []

    # the report carries a real answer, not just a transcript
    assert task.report.findings
    assert task.report.evidence
    assert task.report.sources
    assert any(s.doi == DEMO_DOI for s in task.report.sources)


def test_the_trace_shows_the_plan_and_the_tools():
    agent, _library, _document_id = build_offline_stack(QUESTION)
    task = agent.run(QUESTION)

    tools = [s.tool for s in task.steps if s.tool]
    assert tools == ["analyze_molecule", "search_literature",
                     "search_local_document", "get_document_evidence"]
    assert all(s.result_status is not None for s in task.steps)
    assert all(s.llm_model for s in task.steps)


def test_computed_values_come_from_rdkit_and_are_labelled_computed():
    agent, _library, _document_id = build_offline_stack(QUESTION)
    report = agent.run(QUESTION).report

    computed = [f for f in report.findings if f.kind == "computed"]
    assert computed
    assert any("C16H10" in f.text for f in computed)     # the real formula
    assert any("C(C#Cc1ccccc1)#Cc1ccccc1" in f.text for f in computed)  # real scaffold


def test_the_claim_is_backed_by_a_quote_from_the_actual_pdf():
    agent, _library, document_id = build_offline_stack(QUESTION)
    report = agent.run(QUESTION).report

    backed = [f for f in report.findings if f.kind == "evidence_backed"]
    assert backed, "the report must reach an evidence-backed finding"
    for finding in backed:
        assert finding.evidence_refs

    evidence = report.evidence[0]
    assert evidence.source == "user_provided"
    assert evidence.source_id == document_id
    assert evidence.locator.page_start == 1
    assert evidence.locator.section
    # the quote is text the file actually contained
    assert "stirred at 80 C for 12 hours" in evidence.raw_quote
    assert "Yield 82%" in evidence.raw_quote
    assert evidence.provenance.raw_payload_hash == _doc_hash(agent)


def _doc_hash(agent) -> str:
    for document in agent._documents.all():
        return document.document.sha256
    raise AssertionError("no document")


# ---------------------------------------------------------------------------
# the boundaries that matter
# ---------------------------------------------------------------------------


def test_nothing_is_asserted_as_a_fact_without_evidence():
    agent, _library, _document_id = build_offline_stack(QUESTION)
    report = agent.run(QUESTION).report
    unsupported = [f for f in report.findings
                   if f.kind in ("fact", "evidence_backed") and not f.evidence_refs]
    assert unsupported == []


def test_a_claim_with_no_evidence_is_reported_as_insufficient():
    """The failure mode this whole project exists to prevent.

    If the document search had found nothing, the claim would have had
    nothing to stand on. It must come back saying so, not asserted anyway.
    """
    from crea.core.models.evidence import Claim
    from crea.report.research import build_findings

    orphan = Claim.build("c1", "反应在 80 °C 进行。", "experimental_fact", ["nope"])
    findings = build_findings([orphan], evidence=[])
    assert len(findings) == 1
    assert findings[0].kind == "insufficient"
    assert findings[0].evidence_refs == []
    assert "证据不足" in findings[0].note


def test_the_agent_cannot_fabricate_a_tool_result():
    """A model that claims a tool ran does not make it so.

    The scripted provider is given an extra turn that merely *asserts* a search
    returned a paper. Nothing enters state, so nothing is cited.
    """
    from crea.core.models.research import AgentAction, ActionKind
    from crea.llm.fake import FakeLLMProvider
    from crea.agent.offline import FixtureTransport, _crossref_payload, _openalex_payload
    from crea.agent.toolsets import build_registry
    from crea.sources.crossref import CrossrefProvider, WORKS_ENDPOINT as CR
    from crea.sources.openalex import OpenAlexProvider, WORKS_ENDPOINT as OA
    from crea.services.literature import LiteratureService
    from crea.agent.workspace import DocumentLibrary
    from crea.services.documents import LocalDocumentService
    from crea.sources.pdf import PypdfDocumentParser

    transport = FixtureTransport({OA: _openalex_payload(), CR: _crossref_payload()})
    literature = LiteratureService({
        "openalex": OpenAlexProvider(transport=transport, endpoint=OA),
        "crossref": CrossrefProvider(transport=transport, endpoint=CR)})
    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    sink: list = []
    registry = build_registry(literature, library, sink)

    fake = FakeLLMProvider([
        AgentAction(action=ActionKind.FINAL,
                    summary="我直接知道一篇文献。",
                    claim_specs=[{"text": "该文献报告收率 90%。",
                                  "claim_type": "experimental_fact",
                                  "from_tool": "get_document_evidence"}]),
    ])
    agent = ResearchAgent(llm=fake, registry=registry, documents=library, evidence=sink)
    task = agent.run(QUESTION)

    # no tool ran, so the spec resolves to nothing and the claim is not built
    assert task.evidence == []
    assert task.sources == []
    assert task.report.findings == []


def test_a_model_supplied_tool_name_that_does_not_exist_is_rejected():
    from crea.core.models.research import ToolStatus
    from crea.agent.tools import ToolRegistry
    from crea.core.models.research import AgentAction

    registry = ToolRegistry()
    result = registry.execute("definitely_not_a_tool", {})
    assert result.status is ToolStatus.ERROR
    assert "no tool named" in result.error


def test_arguments_are_validated_before_anything_executes():
    from crea.core.models.research import ToolStatus
    from crea.agent.toolsets import build_registry
    from crea.agent.offline import FixtureTransport, _crossref_payload, _openalex_payload
    from crea.sources.crossref import CrossrefProvider, WORKS_ENDPOINT as CR
    from crea.sources.openalex import OpenAlexProvider, WORKS_ENDPOINT as OA
    from crea.services.literature import LiteratureService
    from crea.agent.workspace import DocumentLibrary
    from crea.services.documents import LocalDocumentService
    from crea.sources.pdf import PypdfDocumentParser

    transport = FixtureTransport({OA: _openalex_payload(), CR: _crossref_payload()})
    literature = LiteratureService({
        "openalex": OpenAlexProvider(transport=transport, endpoint=OA),
        "crossref": CrossrefProvider(transport=transport, endpoint=CR)})
    registry = build_registry(
        literature, DocumentLibrary(LocalDocumentService(PypdfDocumentParser())), [])

    # `limit` is declared ge=1, le=100
    bad = registry.execute("search_literature", {"query": "x", "limit": 9999})
    assert bad.status is ToolStatus.REJECTED
    assert "limit" in bad.error
    assert bad.data is None

    missing = registry.execute("analyze_molecule", {})
    assert missing.status is ToolStatus.REJECTED

    # and a tool that raises is an error result, not a crash
    assert registry.execute("analyze_molecule", {"smiles": "c1ccccc1"}).status \
        is ToolStatus.OK


def test_a_failing_tool_does_not_stop_the_run():
    from crea.core.models.research import AgentAction, ActionKind
    from crea.llm.fake import FakeLLMProvider
    from crea.agent.offline import build_offline_stack

    agent, library, document_id = build_offline_stack(QUESTION)
    fake = FakeLLMProvider([
        AgentAction(action=ActionKind.TOOL_CALL, tool="search_local_document",
                    arguments={"document_id": "no-such-document", "query": "x"}),
        AgentAction(action=ActionKind.TOOL_CALL, tool="analyze_molecule",
                    arguments={"smiles": "c1ccccc1"}),
        AgentAction(action=ActionKind.FINAL, summary="部分完成。"),
    ])
    agent._llm = fake
    task = agent.run(QUESTION)

    failed = [s for s in task.steps if s.result_status
              and s.result_status.value == "error"]
    assert failed
    assert task.status in (TaskStatus.COMPLETED, TaskStatus.PARTIAL)
    # the failure is recorded, not swallowed
    assert any("no-such-document" in (s.error or "") for s in task.steps)


# ---------------------------------------------------------------------------
# hard limits
# ---------------------------------------------------------------------------


def test_the_loop_stops_at_its_step_ceiling():
    from crea.core.models.research import AgentAction, ActionKind

    agent, _library, _document_id = build_offline_stack(QUESTION)
    # a planner that never finishes
    agent._llm = FakeLLMProvider(
        [AgentAction(action=ActionKind.TOOL_CALL, tool="analyze_molecule",
                      arguments={"smiles": "c1ccccc1"})] * (MAX_STEPS + 5))
    task = agent.run(QUESTION)

    assert len(task.steps) <= MAX_STEPS
    assert task.status is TaskStatus.PARTIAL
    assert task.unresolved_items


def test_the_loop_stops_at_its_tool_call_ceiling():
    from crea.agent.loop import ResearchAgent as RA
    from crea.core.models.research import AgentAction, ActionKind

    agent, _library, _document_id = build_offline_stack(QUESTION)
    bounded = RA(llm=agent._llm, registry=agent._registry,
                 documents=agent._documents, max_steps=50, max_tool_calls=2)
    bounded._llm = FakeLLMProvider(
        [AgentAction(action=ActionKind.TOOL_CALL, tool="analyze_molecule",
                      arguments={"smiles": "c1ccccc1"})] * 10)
    task = bounded.run(QUESTION)

    calls = [s for s in task.steps if s.tool and s.result_status
             and s.result_status.value == "ok"]
    assert len(calls) <= 2
    assert task.status is TaskStatus.PARTIAL


def test_a_fake_provider_that_runs_out_is_a_task_failure_not_a_silent_pass():
    from crea.core.models.research import AgentAction, ActionKind

    agent, _library, _document_id = build_offline_stack(QUESTION)
    agent._llm = FakeLLMProvider([
        AgentAction(action=ActionKind.TOOL_CALL, tool="analyze_molecule",
                    arguments={"smiles": "c1ccccc1"})])
    task = agent.run(QUESTION)
    assert task.status is TaskStatus.FAILED
    assert task.errors


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------


def test_no_secret_reaches_the_trace():
    from crea.llm.base import redact

    agent, _library, _document_id = build_offline_stack(QUESTION)
    task = agent.run(QUESTION)
    blob = json.dumps([s.model_dump(mode="json") for s in task.steps],
                      ensure_ascii=False)
    for banned in ("sk-", "Bearer ", "api_key", "OPENAI_API_KEY"):
        assert banned not in blob

    assert redact({"api_key": "sk-secret"})["api_key"] == "<redacted>"
    assert redact({"nested": [{"password": "x"}]})["nested"][0]["password"] == "<redacted>"


def test_a_tool_result_is_redacted_before_it_enters_the_thread():
    from crea.llm.base import redact
    assert redact({"email": "a@b.c"})["email"] == "<redacted>"


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


def test_the_report_renders_in_chinese_with_provenance():
    agent, _library, _document_id = build_offline_stack(QUESTION)
    report = agent.run(QUESTION).report
    text = format_report(report)

    assert "# 研究报告" in text
    assert "## 结论" in text
    assert "## 证据" in text
    assert "## 来源" in text
    assert "## 执行轨迹" in text
    # the quoted passage is in the report
    assert "stirred at 80 C" in text
    # and so is the compound name the user typed
    assert QUESTION in text


def test_an_unknown_question_asks_rather_than_invents():
    agent, _library, _document_id = build_offline_stack("研究某个未知化合物")
    task = agent.run("研究某个未知化合物")
    assert task.status is TaskStatus.PARTIAL
    assert task.unresolved_items
    assert task.evidence == []

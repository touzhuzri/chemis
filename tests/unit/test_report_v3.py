"""Report v3: the knowledge-aware rendering, as a pure renderer.

Phase 3.12 §98-§100. v2 output is byte-identical when no knowledge is
attached (the API is additive); with knowledge, the report separates
**existing** from **new**, shows **conflicts** with both citations, and
names the **gaps** in readable sentences. The renderer consumes models
and produces Markdown -- it never reaches for a database (§99).
"""

from __future__ import annotations

from crea.core.models.knowledge import (
    FacetKnowledge,
    FieldConflict,
    FieldObservation,
    KnowledgeCoverage,
    KnowledgeFacet,
    KnowledgeGap,
    KnowledgeReport,
    KnowledgeState,
)
from crea.core.models.research import ResearchReport, TaskStatus
from crea.report.research import (
    render_research_report,
    render_research_report_v3,
)


def _report() -> ResearchReport:
    return ResearchReport(question="研究 phenylacetylene 的合成方法",
                          status=TaskStatus.COMPLETED)


def _knowledge() -> KnowledgeReport:
    coverage = KnowledgeCoverage(
        entity_id="CHEM-00000001", target="phenylacetylene",
        facets=[
            FacetKnowledge(facet=KnowledgeFacet.ENTITY, state=KnowledgeState.KNOWN,
                           detail="已建立实体 CHEM-00000001",
                           object_ids=["CHEM-00000001"]),
            FacetKnowledge(facet=KnowledgeFacet.REACTION, state=KnowledgeState.KNOWN,
                           detail="1 个已建立的反应", object_ids=["RXN-000001"]),
            FacetKnowledge(facet=KnowledgeFacet.EXPERIMENTAL_EVIDENCE,
                           state=KnowledgeState.PARTIALLY_KNOWN,
                           detail="2 条引用证据，但没有实验级支持"),
        ])
    return KnowledgeReport(
        coverage=coverage,
        gaps=[KnowledgeGap(
            facet=KnowledgeFacet.EXPERIMENTAL_EVIDENCE,
            reason="已有反应结构信息，但尚未找到可验证的实验条件证据。",
            suggested_action="inspect local document")],
        conflicts=[FieldConflict(
            field="temperature",
            observations=[
                FieldObservation(field="temperature", value=80,
                                 raw_value="80", unit="C", raw_unit="C",
                                 evidence_id="EV-000021", paper_id="paper-a",
                                 reaction_id="RXN-a"),
                FieldObservation(field="temperature", value=60,
                                 raw_value="60", unit="C", raw_unit="C",
                                 evidence_id="EV-000022", paper_id="paper-b",
                                 reaction_id="RXN-b"),
            ])],
        existing={"entities": ["CHEM-00000001"],
                  "reactions": ["RXN-000001"],
                  "evidence": ["EV-000020"]},
        new={"evidence": ["EV-000021"], "reactions": []})


def test_v2_output_is_untouched_without_knowledge():
    report = _report()
    assert render_research_report_v3(report) == render_research_report(report)


def test_v3_separates_existing_new_conflicts_and_gaps():
    markdown = render_research_report_v3(_report(), knowledge=_knowledge())

    assert "## Existing Knowledge" in markdown
    assert "RXN-000001" in markdown.split("## Existing Knowledge")[1]
    assert "## New Knowledge" in markdown
    assert "EV-000021" in markdown.split("## New Knowledge")[1]
    assert "## Conflicts" in markdown
    assert "## Knowledge Gaps" in markdown
    assert "## Knowledge State" in markdown

    # §100: the gap sentence, verbatim; no confidence numbers anywhere
    assert "已有反应结构信息，但尚未找到可验证的实验条件证据。" in markdown
    assert "置信度" not in markdown and "confidence" not in markdown.lower()

    # §22-§24: the conflict keeps BOTH values with BOTH citations --
    # no average, no winner
    conflict_block = markdown.split("## Conflicts")[1].split("##")[0]
    assert "80" in conflict_block and "60" in conflict_block
    assert "EV-000021" in conflict_block and "EV-000022" in conflict_block
    assert "CONFLICT" in conflict_block
    assert "70" not in conflict_block  # the average never appears


def test_existing_knowledge_is_marked_as_a_record_not_as_currency():
    markdown = render_research_report_v3(_report(), knowledge=_knowledge())
    assert "不代表当前最新" in markdown, "§15: known is recorded, not current"


def test_a_first_run_says_so():
    knowledge = KnowledgeReport(
        coverage=KnowledgeCoverage(entity_id=None, target="x"),
        gaps=[], conflicts=[],
        existing={}, new={"entities": ["CHEM-00000001"]})
    markdown = render_research_report_v3(_report(), knowledge=knowledge)
    assert "本次研究第一次建立知识" in markdown

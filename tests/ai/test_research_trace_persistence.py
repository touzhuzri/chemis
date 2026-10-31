"""M2.0.1 -- the audit trail that outlives the process that wrote it.

The defect these tests pin: before M2.0.1 the `DecisionRecord`s, tool
results, observations and interpretations of an AI research run lived
only in the `_LiveRun` dict of the process that ran it. A restart made
the run unreadable (`SESSION_NOT_FOUND: ... in this process`), and the
only reason the first acceptance run could be audited at all is that its
script happened to dump JSON before exiting.

Pinned here: every audited turn is written to SQLite while it happens;
a second process (a new application object, and one true subprocess)
reads the same rows back, restores the DecisionRecord order, and renders
the same report; the tool result and the model's interpretation stay in
separate fields; a re-write never duplicates the trail; a failed write
never fakes a success; credentials never reach a row; the GoldenCase
agent view stays sealed; and the persistence layer itself can execute
nothing -- it has no registry, no model, no loop.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest
from pydantic import BaseModel, Field

from crea.agent.intelligence import (
    ResearchBudget,
    ResearchIntelligence,
    run_result_from_trace,
)
from crea.agent.tools import ExecutionPolicy, ToolRegistry, ToolSpec
from crea.app.research_intelligence import ResearchIntelligenceApplication
from crea.core.errors import ResearchStudyError
from crea.core.models.research_trace import (
    ResearchRunTrace,
    ResearchTurnTrace,
)
from crea.llm.fake import TextScriptedLLMProvider
from crea.services.golden_case_store import GoldenCaseStore
from crea.services.knowledge_base import KnowledgeBase
from crea.services.research_trace import ResearchTraceService


class _LiteratureArgs(BaseModel):
    query: str = Field(default="")
    limit: int = Field(default=10)
    api_key: str = Field(default="", description="provider credential slot")


class Script:
    def __init__(self, payloads: list) -> None:
        self.payloads = list(payloads)

    def __call__(self, **kwargs):
        payload = self.payloads.pop(0) if self.payloads else {"status": "ok"}
        if isinstance(payload, Exception):
            raise payload
        return payload


def build_stack(*, literature: Script | None = None):
    literature = literature or Script([
        {"status": "ok", "count": 1,
         "papers": [{"title": "A green route to the target"}],
         "evidence_ids": ["ev-1"]},
        {"status": "ok", "count": 1,
         "papers": [{"title": "Second run"}], "evidence_ids": ["ev-2"]},
    ])
    registry = ToolRegistry(enforce_policy=True,
                            policy=ExecutionPolicy(allow_network=True))
    registry.register(ToolSpec(
        name="search_literature", description="有界文献发现",
        arguments_model=_LiteratureArgs, run=literature, access=("local",),
        sets_facts=("literature_searched",)))
    return registry, None, []


def scripted(*payloads) -> TextScriptedLLMProvider:
    return TextScriptedLLMProvider(
        [p if isinstance(p, str) else json.dumps(p, ensure_ascii=False)
         for p in payloads])


def tool_call(query, interpretation="", api_key=""):
    return {"kind": "continue_with_tool", "tool_call": {
        "tool": "search_literature",
        "arguments": {"query": query, "api_key": api_key},
        "decision_summary": f"检索 {query}", "purpose": "定位报道",
        "expected_information": "候选论文",
        "related_hypothesis_ids": [], "relevant_evidence_ids": []},
        "observation_interpretation": interpretation}


def complete(reason, sufficiency="partial"):
    return {"kind": "complete", "completion": {
        "completion_reason": reason, "achieved_objectives": [],
        "unresolved_questions": [], "evidence_sufficiency": sufficiency,
        "limitations": ["未做实验验证"]}}


@pytest.fixture()
def two_turn_run(tmp_path):
    """One completed two-turn run and its database file."""
    db = tmp_path / "trace.db"
    application = ResearchIntelligenceApplication(
        db_path=db,
        llm_factory=lambda: scripted(
            tool_call("阿司匹林 绿色合成",
                      interpretation="命中一条绿色合成路线报道"),
            complete("已有候选文献", sufficiency="partial")),
        stack_factory=lambda document_ids=None: build_stack())
    serialized = application.start("研究阿司匹林")
    return application, db, serialized


# ---------------------------------------------------------------------------
# The trail is on disk, turn by turn
# ---------------------------------------------------------------------------


def test_every_turn_and_the_run_summary_are_written_to_sqlite(two_turn_run):
    _, db, serialized = two_turn_run
    session_id = serialized["session_id"]
    study_id = serialized["study_id"]

    with KnowledgeBase(db) as kb:
        service = ResearchTraceService(kb)
        run_row = service.run(study_id, session_id)
        turns = service.turns(study_id, session_id)

    assert run_row is not None
    assert run_row.outcome == "completed"
    assert run_row.goal == "研究阿司匹林"
    assert run_row.record_count == 2
    assert run_row.model_provider == "text-scripted"
    assert run_row.budget["max_tool_calls"] == ResearchBudget().max_tool_calls
    assert [t.record.decision_type.value for t in turns] == [
        "continue_with_tool", "complete"]
    assert turns[0].record.validation_result.value == "accepted"


def test_tool_result_observation_and_interpretation_stay_separate(
        two_turn_run):
    _, db, serialized = two_turn_run
    session_id = serialized["session_id"]
    study_id = serialized["study_id"]

    with KnowledgeBase(db) as kb:
        turns = ResearchTraceService(kb).turns(study_id, session_id)
    first = turns[0]

    # the execution half
    assert first.tool_name == "search_literature"
    assert first.arguments["query"] == "阿司匹林 绿色合成"
    assert first.step is not None and first.step.step_id == first.step_id
    assert first.tool_result is not None
    assert first.tool_result.status.value == "ok"
    assert first.tool_result.data["papers"][0]["title"] == (
        "A green route to the target")
    # the observer's reading, as its own JSON
    assert first.observation["kind"] == "success"
    assert first.observation["step_id"] == first.step_id
    # the model's reading, in its own field -- never inside the observation
    assert first.interpretation is not None
    assert "绿色合成路线" in first.interpretation.interpretation
    assert "interpretation" not in first.observation
    assert "绿色合成路线" not in json.dumps(first.tool_result.model_dump(
        mode="json"), ensure_ascii=False)
    # the join the report relies on
    assert first.record.associated_step_id == first.step_id


def test_the_same_session_keeps_a_stable_order_across_reads(two_turn_run):
    _, db, serialized = two_turn_run
    session_id = serialized["session_id"]
    study_id = serialized["study_id"]

    with KnowledgeBase(db) as kb:
        first = ResearchTraceService(kb).turns(study_id, session_id)
        second = ResearchTraceService(kb).turns(study_id, session_id)

    assert [t.decision_id for t in first] == [t.decision_id for t in second]
    assert [t.record.turn for t in first] == [1, 2]
    assert [t.position for t in first] == sorted(t.position for t in first)


def test_writing_the_same_turn_twice_never_duplicates_the_trail(tmp_path):
    from crea.core.ids import content_hash
    from crea.core.models.research_decision import DecisionRecord

    with KnowledgeBase(tmp_path / "dup.db") as kb:
        kb.initialize()
        kb.save_study(_study("study-dup"))
        service = ResearchTraceService(kb)
        turn = ResearchTurnTrace(
            study_id="study-dup", session_id="sess-1", turn=1,
            decision_id="dec-fixed",
            record=DecisionRecord(decision_id="dec-fixed",
                                  session_id="sess-1", turn=1))
        service.record_turn(turn)
        service.record_turn(turn.model_copy(update={
            "record": turn.record.model_copy(update={
                "associated_step_id": "step-1"})}))

        turns = service.turns("study-dup", "sess-1")

    assert len(turns) == 1
    assert turns[0].record.associated_step_id == "step-1"


def test_the_run_summary_is_upserted_not_duplicated(tmp_path):
    with KnowledgeBase(tmp_path / "run.db") as kb:
        kb.initialize()
        kb.save_study(_study("study-run"))
        service = ResearchTraceService(kb)
        base = dict(study_id="study-run", session_id="sess-1",
                    outcome="partial", reason="第一次")
        service.record_run(ResearchRunTrace(**base))
        service.record_run(ResearchRunTrace(**{**base, "outcome": "completed",
                                               "reason": "第二次"}))

        assert len(service.runs("study-run")) == 1
        assert service.run("study-run", "sess-1").outcome == "completed"


# ---------------------------------------------------------------------------
# Cross-process reads
# ---------------------------------------------------------------------------


def test_a_fresh_application_reads_the_run_it_never_saw(two_turn_run):
    """A new application object on the same database -- the in-process
    half of cross-process: nothing from the first object's memory is
    involved, and the audit answers anyway."""
    _, db, serialized = two_turn_run
    session_id = serialized["session_id"]

    fresh = ResearchIntelligenceApplication(db_path=db)
    decisions = fresh.decisions(session_id)
    fetched = fresh.get(session_id)
    report = fresh.report(session_id)

    assert [d["decision_type"] for d in decisions] == [
        "continue_with_tool", "complete"]
    assert decisions[0]["selected_tool"] == "search_literature"
    assert decisions[0]["validation_result"] == "accepted"
    assert fetched["outcome"] == "completed"
    assert fetched["evidence_ids"] == ["ev-1"]
    assert report.startswith("# AI 研究报告")
    assert "AI 解读" in report
    assert "命中一条绿色合成路线报道" in report
    # a rebuilt record is readable, not continuable
    with pytest.raises(ResearchStudyError) as excinfo:
        fresh.answer(session_id, answers={"x": "y"})
    assert excinfo.value.code in ("SESSION_NOT_FOUND", "NOT_AWAITING_INPUT")


def test_a_true_second_process_reads_the_trail_and_renders_the_report(
        two_turn_run):
    """The subprocess half: a real new interpreter, a real new database
    connection, the same audit."""
    _, db, serialized = two_turn_run
    session_id = serialized["session_id"]

    code = textwrap.dedent(f"""
        import json
        from pathlib import Path
        from crea.app.research_intelligence import ResearchIntelligenceApplication
        app = ResearchIntelligenceApplication(db_path=Path(r"{db}"))
        decisions = app.decisions({session_id!r})
        report = app.report({session_id!r})
        print(json.dumps({{
            "types": [d["decision_type"] for d in decisions],
            "first_tool": decisions[0]["selected_tool"],
            "head": report.splitlines()[0],
            "has_reading": "AI 解读" in report,
        }}, ensure_ascii=False))
        """)
    completed = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, text=True, timeout=120)

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["types"] == ["continue_with_tool", "complete"]
    assert payload["first_tool"] == "search_literature"
    assert payload["head"] == "# AI 研究报告"
    assert payload["has_reading"] is True


def test_the_rebuilt_result_matches_the_live_one_field_for_field(
        two_turn_run):
    """Semantic equality: what a reader gets from the audit trail is what
    the original process held -- same gate rulings, same observations,
    same interpretations, same stop reason."""
    application, _, serialized = two_turn_run
    session_id = serialized["session_id"]

    live = application.live_result(session_id)
    fresh = ResearchIntelligenceApplication(db_path=application.db_path)
    rebuilt = fresh.live_result(session_id)

    assert rebuilt.session_id == live.session_id
    assert rebuilt.study_id == live.study_id
    assert rebuilt.outcome == live.outcome
    assert rebuilt.reason == live.reason
    assert rebuilt.status is live.status
    assert rebuilt.mode is live.mode
    assert ([r.decision_id for r in rebuilt.decision_records]
            == [r.decision_id for r in live.decision_records])
    assert ([r.validation_result for r in rebuilt.decision_records]
            == [r.validation_result for r in live.decision_records])
    assert ([o.summary for o in rebuilt.observations]
            == [o.summary for o in live.observations])
    assert ([o.kind for o in rebuilt.observations]
            == [o.kind for o in live.observations])
    assert rebuilt.observations[0].data == live.observations[0].data
    assert ([i.interpretation for i in rebuilt.interpretations]
            == [i.interpretation for i in live.interpretations])
    assert rebuilt.tool_results[0].data == live.tool_results[0].data
    assert rebuilt.decisions_used == live.decisions_used
    assert rebuilt.evidence_ids == live.evidence_ids


def test_the_rebuild_helper_never_lets_an_interpretation_become_data(
        two_turn_run):
    _, db, serialized = two_turn_run
    session_id = serialized["session_id"]
    study_id = serialized["study_id"]

    with KnowledgeBase(db) as kb:
        service = ResearchTraceService(kb)
        run_row = service.run(study_id, session_id)
        turns = service.turns(study_id, session_id)
    rebuilt = run_result_from_trace(run_row, turns)

    observation_text = json.dumps(
        rebuilt.observations[0].model_dump(mode="json", exclude={"raw"}),
        ensure_ascii=False)
    assert "绿色合成路线" not in observation_text
    assert "绿色合成路线" in rebuilt.interpretations[0].interpretation
    assert rebuilt.tool_results[0].data["papers"][0]["title"] == (
        "A green route to the target")


# ---------------------------------------------------------------------------
# Honesty under failure, isolation, and redaction
# ---------------------------------------------------------------------------


def test_a_failed_trace_write_is_a_diagnostic_never_a_fake_success(tmp_path):
    class ExplodingSink:
        def __init__(self):
            self.turns = 0

        def record_turn(self, turn):
            self.turns += 1
            raise RuntimeError("disk full")

        def record_run(self, run):
            raise RuntimeError("disk full")

    registry, _, _ = build_stack()
    sink = ExplodingSink()
    intelligence = ResearchIntelligence(
        registry, scripted(
            tool_call("阿司匹林", interpretation="读到了结果"),
            complete("结束"),
        ), study_id="study-fail", trace_sink=sink)
    result = intelligence.run("研究阿司匹林", session_id="sess-fail")

    # the research itself completed honestly ...
    assert result.outcome == "completed"
    assert sink.turns >= 1
    # ... and the failures are on the record, not swallowed
    failures = [d for d in result.diagnostics if "trace write failed" in d]
    assert len(failures) >= 2


def test_no_sink_and_no_study_means_no_trace_rows_and_no_crash():
    registry, _, _ = build_stack()
    intelligence = ResearchIntelligence(
        registry, scripted(tool_call("q"), complete("done")))
    result = intelligence.run("研究X")

    assert result.outcome == "completed"
    assert result.decision_records


def test_tool_arguments_carrying_credentials_never_reach_the_database(
        tmp_path):
    """A credential in tool arguments or in a tool payload is scrubbed on
    the way into the audit tables (§87/§88) and redacted on the way out."""
    db = tmp_path / "secret.db"
    literature = Script([
        {"status": "ok", "count": 1, "api_key": "sk-live-abc123",
         "papers": [{"title": "P"}], "evidence_ids": ["ev-9"]},
    ])
    application = ResearchIntelligenceApplication(
        db_path=db,
        llm_factory=lambda: scripted(
            tool_call("q", api_key="sk-request-xyz789"),
            complete("done")),
        stack_factory=lambda document_ids=None: build_stack(
            literature=literature))
    serialized = application.start("研究X")
    session_id = serialized["session_id"]
    study_id = serialized["study_id"]

    raw = (db.read_bytes())
    assert b"sk-live-abc123" not in raw
    assert b"sk-request-xyz789" not in raw

    with KnowledgeBase(db) as kb:
        turns = ResearchTraceService(kb).turns(study_id, session_id)
    payload = json.dumps(
        [t.model_dump(mode="json") for t in turns], ensure_ascii=False)
    assert "sk-live-abc123" not in payload
    assert "sk-request-xyz789" not in payload
    decisions_text = json.dumps(application.decisions(session_id),
                                ensure_ascii=False)
    assert "sk-request-xyz789" not in decisions_text


def test_the_trail_is_scoped_to_its_study(tmp_path):
    with KnowledgeBase(tmp_path / "scope.db") as kb:
        kb.initialize()
        kb.save_study(_study("study-a"))
        kb.save_study(_study("study-b"))
        service = ResearchTraceService(kb)
        service.record_run(ResearchRunTrace(study_id="study-a",
                                            session_id="sess-a",
                                            outcome="completed"))
        service.record_turn(ResearchTurnTrace(
            study_id="study-a", session_id="sess-a", turn=1,
            decision_id="dec-a",
            record=_decision_record("dec-a")))

        assert service.turns("study-b", "sess-a") == []
        assert service.run("study-b", "sess-a") is None
        assert len(service.turns("study-a", "sess-a")) == 1
        # the session-id lookup is a lookup, not an enumeration
        assert service.find_run("sess-a").study_id == "study-a"
        assert service.find_run("sess-nothing") is None


def test_a_trace_row_without_a_study_is_refused(tmp_path):
    with KnowledgeBase(tmp_path / "refuse.db") as kb:
        kb.initialize()
        service = ResearchTraceService(kb)
        with pytest.raises(ResearchStudyError):
            service.record_turn(ResearchTurnTrace(
                study_id="", session_id="s", turn=1, decision_id="d",
                record=_decision_record("d")))
        with pytest.raises(ResearchStudyError):
            service.record_run(ResearchRunTrace(study_id="study-x",
                                                session_id=""))


def test_an_existing_study_context_database_gains_the_tables_idempotently(
        tmp_path):
    """A pre-M2.0.1 database (same schema version) is upgraded in place
    by `initialize()` -- additive tables, nothing rewritten."""
    db = tmp_path / "legacy.db"
    from crea.core.models.research_study import ResearchStudy, StudyStatus

    with KnowledgeBase(db) as kb:
        kb.initialize()
        kb.save_study(ResearchStudy(
            study_id="study-old", question="老问题",
            status=StudyStatus.ACTIVE, created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z"))
    # reopen: initialize runs again, tables already exist
    with KnowledgeBase(db) as kb:
        kb.initialize()
        kb.save_study(_study("study-new"))
        service = ResearchTraceService(kb)
        service.record_run(ResearchRunTrace(study_id="study-new",
                                            session_id="sess-n",
                                            outcome="completed"))
        assert service.find_run("sess-n") is not None
        assert kb.get_study("study-old").question == "老问题"


# ---------------------------------------------------------------------------
# GoldenCase isolation and the persistence layer's own boundaries
# ---------------------------------------------------------------------------


def test_the_golden_case_agent_view_stays_sealed_beside_an_ai_run(
        tmp_path):
    """An AI research run writing audit rows to the same database does
    not open any door into a golden case's hidden reference material."""
    from crea.core.models.golden_case import (
        GoldenCase,
        GoldenCaseStatus,
        GoldenCaseType,
        ReferenceMaterial,
        ReferenceMaterialKind,
    )
    from crea.core.models.golden_case_access import BlindInput

    db = tmp_path / "mixed.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    store = GoldenCaseStore(kb)
    case = GoldenCase(
        case_id="case-sealed", name="咖啡因盲测",
        case_type=GoldenCaseType.BLIND_REPRODUCTION,
        description="盲测", case_version="1",
        status=GoldenCaseStatus.EXECUTABLE,
        blind_input=BlindInput(goal="研究咖啡因"))
    store.create(case)
    store.save_reference("case-sealed", ReferenceMaterial(
        reference_id="ref-hidden", material_type=ReferenceMaterialKind.EXPERT_ROUTE,
        title="专家答案", doi="", content_locator="HIDDEN-REFERENCE-CONTENT.txt",
        content_hash="", hidden=True))
    store.save_reference("case-sealed", ReferenceMaterial(
        reference_id="ref-visible", material_type=ReferenceMaterialKind.EXPERT_ROUTE,
        title="可见说明", doi="", content_locator="visible.txt",
        content_hash="", hidden=False))

    application = ResearchIntelligenceApplication(
        db_path=db,
        llm_factory=lambda: scripted(tool_call("q"), complete("done")),
        stack_factory=lambda document_ids=None: build_stack())
    serialized = application.start("研究咖啡因")

    view_text = json.dumps(store.get_agent_view("case-sealed").model_dump(
        mode="json"), ensure_ascii=False)
    assert "HIDDEN-REFERENCE-CONTENT" not in view_text
    assert "ref-hidden" not in view_text
    # and the audit trail of the AI run does not carry case material either
    trace_text = json.dumps(application.decisions(
        serialized["session_id"]), ensure_ascii=False)
    assert "HIDDEN-REFERENCE" not in trace_text
    kb.close()


def test_the_trace_service_imports_no_agent_loop_and_no_tool_registry():
    """The persistence layer executes nothing: no registry, no provider,
    no observer, no loop. It stores rows and reads rows."""
    import ast
    from pathlib import Path

    path = (Path(__file__).parents[2] / "src" / "crea" / "services" /
            "research_trace.py")
    imported = {node.names[0].name if isinstance(node, ast.Import)
                else node.module
                for node in ast.walk(ast.parse(path.read_text("utf-8")))
                if isinstance(node, (ast.Import, ast.ImportFrom))}
    for banned in ("crea.agent", "crea.llm", "crea.repositories",
                   "ToolRegistry", "ResearchIntelligence"):
        assert not any(name and (name == banned or name.startswith(banned + "."))
                       for name in imported), banned
    text = path.read_text("utf-8")
    assert ".execute(" not in text
    assert "complete(" not in text


def test_the_store_methods_for_the_trail_only_read_and_write_rows():
    """The new SQLite methods contain no execution path: no `run(`, no
    registry, no model -- SELECT/INSERT/UPDATE and JSON codecs only."""
    import ast
    from pathlib import Path

    path = Path(__file__).parents[2] / "src" / "crea" / "repositories" / \
        "sqlite.py"
    tree = ast.parse(path.read_text("utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "SQLiteStore":
            for item in node.body:
                if (isinstance(item, ast.FunctionDef)
                        and item.name.startswith(("save_ai_research",
                                                  "list_ai_research",
                                                  "get_ai_research",
                                                  "find_ai_research"))):
                    text = ast.unparse(item)
                    assert ".run(" not in text, item.name
                    assert "execute_tool" not in text, item.name


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _study(study_id: str):
    from crea.core.models.research_study import (
        ResearchStudy,
        StudyStatus,
    )
    return ResearchStudy(
        study_id=study_id, question="测试问题", status=StudyStatus.ACTIVE,
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z")


def _decision_record(decision_id: str):
    from crea.core.models.research_decision import DecisionRecord
    return DecisionRecord(decision_id=decision_id, session_id="s", turn=1)

"""M1.7.2 -- GoldenCaseStore: persistence, versions, and the two views.

What is pinned here: create refuses to overwrite an existing case_id;
updates require the caller to have read the current version and to bump
the content version; status moves without touching content; references,
traces and evaluations stay keyed to their case and keep their frozen
version links; a corrupt stored record is reported, never returned
half-formed; everything survives a store restart; and the two views are
the two views -- the agent one structurally cannot carry a reference,
the manager one reads everything and unseals nothing.
"""

from __future__ import annotations

import pathlib

import pytest

from crea.core.errors import GoldenCaseError
from crea.core.models.golden_case import (
    GOLDEN_CASE_SCHEMA_VERSION,
    BlindInput,
    DimensionEvaluation,
    EvaluationDimension,
    EvaluationResult,
    EvaluationStatus,
    GoldenCase,
    GoldenCaseStatus,
    GoldenCaseType,
    ReferenceMaterial,
    ReferenceMaterialKind,
    ResearchTrace,
)
from crea.core.models.golden_case_access import (
    GoldenCaseAgentView,
    GoldenCaseManagerView,
    UnsealOutcome,
    UnsealRequest,
)
from crea.core.models.research import TaskStatus
from crea.services.golden_case_store import GoldenCaseStore
from crea.services.golden_case_unseal import GoldenCaseUnsealService
from crea.services.knowledge_base import KnowledgeBase


@pytest.fixture()
def kb(tmp_path):
    store = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    store.initialize()
    yield store
    store.close()


@pytest.fixture()
def store(kb):
    return GoldenCaseStore(kb)


def _case(case_id="case-1", name="阿司匹林盲测", version="1",
          status=GoldenCaseStatus.EXECUTABLE) -> GoldenCase:
    return GoldenCase(
        case_id=case_id, name=name, case_type=GoldenCaseType.BLIND_REPRODUCTION,
        description="一篇论文的路线复现", case_version=version, status=status,
        blind_input=BlindInput(goal="研究阿司匹林的合成路线"),
        tags=["aspirin"], domains=["药物合成"])


def _reference(reference_id="ref-1", hidden=True) -> ReferenceMaterial:
    return ReferenceMaterial(
        reference_id=reference_id,
        material_type=ReferenceMaterialKind.EXPERT_ROUTE,
        title="专家路线", doi="10.1000/x",
        content_locator="refs/route.txt", content_hash="",
        hidden=hidden)


def _trace(case_id="case-1", case_version="1",
           trace_id="trace-1") -> ResearchTrace:
    return ResearchTrace(trace_id=trace_id, case_id=case_id,
                         case_version=case_version, session_id="sess-1",
                         status=TaskStatus.COMPLETED)


def _evaluation(case_id="case-1", case_version="1", trace_id="trace-1",
                evaluation_id="eval-1") -> EvaluationResult:
    return EvaluationResult(
        evaluation_id=evaluation_id, case_id=case_id,
        case_version=case_version, trace_id=trace_id,
        evaluation_type=GoldenCaseType.BLIND_REPRODUCTION,
        evaluator="expert:张三", status=EvaluationStatus.PASS,
        dimensions=[DimensionEvaluation(
            dimension=EvaluationDimension.TARGET_IDENTITY,
            verdict=EvaluationStatus.PASS)])


# ---------------------------------------------------------------------------
# A. Storage: create, read, update, list, versions, associations, restart
# ---------------------------------------------------------------------------


def test_create_and_get_round_trips_the_full_case(store):
    created = store.create(_case())

    fetched = store.get("case-1")
    assert fetched == created
    assert fetched.schema_version == GOLDEN_CASE_SCHEMA_VERSION
    assert fetched.blind_input.goal == "研究阿司匹林的合成路线"


def test_creating_an_existing_case_id_is_an_error_not_an_overwrite(store):
    store.create(_case(name="第一版"))

    with pytest.raises(GoldenCaseError) as excinfo:
        store.create(_case(name="第二版"))
    assert excinfo.value.code == "CASE_EXISTS"
    assert store.get("case-1").name == "第一版", "原案例未被覆盖"


def test_getting_an_absent_case_is_typed_not_none(store):
    with pytest.raises(GoldenCaseError) as excinfo:
        store.get("case-missing")
    assert excinfo.value.code == "CASE_NOT_FOUND"
    with pytest.raises(GoldenCaseError) as excinfo:
        store.get_version("case-missing")
    assert excinfo.value.code == "CASE_NOT_FOUND"


def test_an_update_must_bump_the_version(store):
    store.create(_case())

    with pytest.raises(GoldenCaseError) as excinfo:
        store.update(_case(version="1").model_copy(
            update={"description": "改了但没升版本"}))
    assert excinfo.value.code == "CASE_VERSION_CONFLICT"

    updated = store.update(_case(version="2").model_copy(
        update={"description": "新描述"}))
    assert updated.case_version == "2"
    assert store.get("case-1").description == "新描述"


def test_an_update_requires_the_version_the_caller_read(store):
    store.create(_case(version="1"))
    store.update(_case(version="2"))

    # A caller who read version 1 tries to write after someone moved to 2.
    with pytest.raises(GoldenCaseError) as excinfo:
        store.update(_case(version="3"), expected_version="1")
    assert excinfo.value.code == "CASE_VERSION_CONFLICT"
    assert store.get_version("case-1") == "2"


def test_an_update_stamps_updated_at_itself(store):
    store.create(_case())
    updated = store.update(_case(version="2"))

    assert updated.updated_at >= _case().updated_at
    assert store.get("case-1").updated_at == updated.updated_at


def test_schema_version_cannot_change_through_an_update(store):
    store.create(_case())

    moved = _case(version="2").model_copy(
        update={"schema_version": "golden-case-v0"})
    with pytest.raises(GoldenCaseError) as excinfo:
        store.update(moved)
    assert excinfo.value.code == "SCHEMA_MISMATCH"
    assert store.get("case-1").schema_version == GOLDEN_CASE_SCHEMA_VERSION


def test_a_case_declaring_a_foreign_schema_is_never_stored(store):
    alien = _case().model_copy(update={"schema_version": "golden-case-v99"})
    with pytest.raises(GoldenCaseError) as excinfo:
        store.create(alien)
    assert excinfo.value.code == "SCHEMA_MISMATCH"


def test_status_moves_without_touching_content_version(store):
    store.create(_case())

    archived = store.set_status("case-1", GoldenCaseStatus.ARCHIVED)
    assert archived.status is GoldenCaseStatus.ARCHIVED
    assert archived.case_version == "1", "归档不是内容变更"
    assert store.get("case-1").status is GoldenCaseStatus.ARCHIVED


def test_archived_is_terminal(store):
    store.create(_case())
    store.set_status("case-1", GoldenCaseStatus.ARCHIVED)

    with pytest.raises(GoldenCaseError) as excinfo:
        store.set_status("case-1", GoldenCaseStatus.EXECUTABLE)
    assert excinfo.value.code == "CASE_ARCHIVED"


def test_list_filters_by_type_and_status(store):
    store.create(_case(case_id="case-1"))
    store.create(_case(case_id="case-2", name="开放文献",
                       status=GoldenCaseStatus.READY).model_copy(
        update={"case_type": GoldenCaseType.OPEN_LITERATURE_RESEARCH}))
    store.create(_case(case_id="case-3", name="另一个盲测",
                       status=GoldenCaseStatus.DRAFT))

    assert [c.case_id for c in store.list_cases()] == [
        "case-1", "case-2", "case-3"]
    assert [c.case_id for c in store.list_cases(
        case_type=GoldenCaseType.OPEN_LITERATURE_RESEARCH)] == ["case-2"]
    assert [c.case_id for c in store.list_cases(
        status=GoldenCaseStatus.EXECUTABLE)] == ["case-1"]
    assert [c.case_id for c in store.list_cases(
        status=GoldenCaseStatus.DRAFT)] == ["case-3"]


def test_references_belong_to_exactly_one_case(store):
    store.create(_case(case_id="case-1"))
    store.create(_case(case_id="case-2", name="另一个"))
    store.save_reference("case-1", _reference())

    assert len(store.list_references("case-1")) == 1
    assert store.list_references("case-2") == []
    with pytest.raises(GoldenCaseError) as excinfo:
        store.get_reference("case-2", "ref-1")
    assert excinfo.value.code == "REFERENCE_NOT_FOUND"


def test_saving_a_reference_requires_the_case(store):
    with pytest.raises(GoldenCaseError) as excinfo:
        store.save_reference("case-nope", _reference())
    assert excinfo.value.code == "CASE_NOT_FOUND"


def test_a_reference_update_preserves_the_sealed_state(store):
    store.create(_case())
    store.save_reference("case-1", _reference(hidden=True))

    # A metadata edit that tries to quietly unseal is refused the flag.
    edited = _reference(hidden=False).model_copy(update={"title": "新标题"})
    saved = store.save_reference("case-1", edited)

    assert saved.hidden is True
    assert store.get_reference("case-1", "ref-1").hidden is True
    assert store.get_reference("case-1", "ref-1").title == "新标题"


def test_a_trace_keeps_the_case_version_it_ran(store):
    store.create(_case(version="1"))
    store.save_trace(_trace(case_version="1"))
    # The case then moves on.
    store.update(_case(version="2"))

    trace = store.get_trace("trace-1")
    assert trace.case_version == "1", "历史轨迹保留其运行时版本"
    assert [t.trace_id for t in store.list_traces("case-1")] == ["trace-1"]


def test_saving_a_trace_requires_the_case(store):
    with pytest.raises(GoldenCaseError) as excinfo:
        store.save_trace(_trace(case_id="case-nope"))
    assert excinfo.value.code == "CASE_NOT_FOUND"


def test_an_evaluation_must_judge_the_traces_case_version(store):
    store.create(_case(version="1"))
    store.save_trace(_trace(case_version="1"))
    store.update(_case(version="2"))

    # A verdict written against version 2 cannot judge a version-1 run.
    with pytest.raises(GoldenCaseError) as excinfo:
        store.save_evaluation(_evaluation(case_version="2"))
    assert excinfo.value.code == "EVALUATION_VERSION_MISMATCH"

    # The correct verdict, pinned to the version the trace ran, is kept.
    store.save_evaluation(_evaluation(case_version="1"))
    stored = store.get_evaluation("eval-1")
    assert stored.case_version == "1"


def test_an_evaluation_names_the_right_case_and_trace(store):
    store.create(_case(case_id="case-1"))
    store.create(_case(case_id="case-2", name="另一个"))
    store.save_trace(_trace(trace_id="trace-1"))
    store.save_trace(_trace(case_id="case-2", trace_id="trace-2"))

    with pytest.raises(GoldenCaseError) as excinfo:
        store.save_evaluation(_evaluation(case_id="case-2"))
    assert excinfo.value.code == "TRACE_CASE_MISMATCH"

    with pytest.raises(GoldenCaseError) as excinfo:
        store.save_evaluation(_evaluation(trace_id="trace-missing"))
    assert excinfo.value.code == "TRACE_NOT_FOUND"


def test_the_store_survives_a_restart(tmp_path):
    db = pathlib.Path(tmp_path) / "crea.db"
    first = KnowledgeBase(db)
    first.initialize()
    store = GoldenCaseStore(first)
    store.create(_case())
    store.save_reference("case-1", _reference())
    store.save_trace(_trace())
    store.save_evaluation(_evaluation())
    first.close()

    second = KnowledgeBase(db)
    second.initialize()
    reopened = GoldenCaseStore(second)

    assert reopened.get("case-1").blind_input.goal == \
        "研究阿司匹林的合成路线"
    assert reopened.get_reference("case-1", "ref-1").title == "专家路线"
    assert reopened.get_trace("trace-1").session_id == "sess-1"
    assert reopened.get_evaluation("eval-1").status is EvaluationStatus.PASS
    second.close()


def test_a_corrupt_stored_record_is_reported_not_returned(kb, store):
    store.create(_case())
    # Reach into storage the way a damaged file would leave it.
    kb._store._execute(
        "UPDATE golden_cases SET content_json = ? WHERE case_id = ?",
        ("{not json at all", "case-1"))

    with pytest.raises(GoldenCaseError) as excinfo:
        store.get("case-1")
    assert excinfo.value.code == "CASE_RECORD_CORRUPT"


def test_cases_and_research_studies_do_not_touch_each_other(kb, store):
    """Golden-case tables have no foreign key into study data: a case
    write leaves study rows alone, and the reverse."""
    store.create(_case())
    store.save_trace(_trace())

    study_tables = kb._store._query(
        "SELECT name FROM sqlite_master WHERE type='table' AND "
        "name LIKE 'study_context%'")
    facts = kb._store.list_study_facts("study-anyone")
    assert facts == []
    assert study_tables, "study tables still exist, untouched"
    # And a trace's session_id is a plain string, not a study FK: no
    # study row was created by the case write.
    studies = kb._store._query(
        "SELECT * FROM research_studies WHERE study_id LIKE 'sess-%'")
    assert studies == []


# ---------------------------------------------------------------------------
# B/C. The two views
# ---------------------------------------------------------------------------


def test_the_agent_view_carries_only_the_visible_half(store):
    store.create(_case())
    store.save_reference("case-1", _reference())

    view = store.get_agent_view("case-1")

    assert isinstance(view, GoldenCaseAgentView)
    assert view.case_id == "case-1"
    assert view.goal == "研究阿司匹林的合成路线"
    assert view.status is GoldenCaseStatus.EXECUTABLE
    assert set(view.model_dump()) == {
        "case_id", "case_type", "case_version", "schema_version",
        "status", "blind_input",
    }


def test_an_agent_view_cannot_be_built_for_a_case_without_input(store):
    draft = GoldenCase(case_id="case-draft", name="草稿",
                       case_type=GoldenCaseType.BLIND_REPRODUCTION)
    store.create(draft)

    with pytest.raises(GoldenCaseError) as excinfo:
        store.get_agent_view("case-draft")
    assert excinfo.value.code == "CASE_INPUT_MISSING"


def test_the_manager_view_reads_everything_and_unseals_nothing(store):
    store.create(_case())
    store.save_reference("case-1", _reference(hidden=True))
    store.save_trace(_trace())
    store.save_evaluation(_evaluation())

    view = store.get_manager_view("case-1")

    assert isinstance(view, GoldenCaseManagerView)
    assert view.case.case_id == "case-1"
    assert [r.reference_id for r in view.references] == ["ref-1"]
    assert [t.trace_id for t in view.traces] == ["trace-1"]
    assert [e.evaluation_id for e in view.evaluations] == ["eval-1"]
    assert view.unseal_events == []
    assert store.get_reference("case-1", "ref-1").hidden is True, (
        "读取管理者视图不解封")


def test_a_second_store_on_one_database_sees_the_same_cases(tmp_path):
    db = pathlib.Path(tmp_path) / "crea.db"
    kb1 = KnowledgeBase(db); kb1.initialize()
    kb2 = KnowledgeBase(db); kb2.initialize()
    writer, reader = GoldenCaseStore(kb1), GoldenCaseStore(kb2)

    writer.create(_case())
    assert reader.get("case-1").name == "阿司匹林盲测"
    kb1.close(); kb2.close()

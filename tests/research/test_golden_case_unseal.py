"""M1.7.2 -- explicit unseal, the audit trail, and the isolation chain.

What is pinned here: references stay hidden until somebody with a name
and a reason opens them; every attempt -- unsealed, already unsealed,
failed -- is an append-only event carrying case id, case version,
reference id, actor, reason and before/after state; reading the manager
view unseals nothing; ordinary reference updates cannot flip the flag;
and the sealed state never leaks back into an agent view, before or
after unsealing.
"""

from __future__ import annotations

import pathlib

import pytest

from crea.core.errors import GoldenCaseError
from crea.core.models.golden_case import (
    BlindInput,
    GoldenCase,
    GoldenCaseStatus,
    GoldenCaseType,
    ReferenceMaterial,
    ReferenceMaterialKind,
)
from crea.core.models.golden_case_access import (
    UnsealOutcome,
    UnsealRequest,
)
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


@pytest.fixture()
def unseal(store):
    return GoldenCaseUnsealService(store)


@pytest.fixture()
def ready(store):
    """A case with two sealed references, ready to be opened."""
    store.create(GoldenCase(
        case_id="case-1", name="阿司匹林盲测",
        case_type=GoldenCaseType.BLIND_REPRODUCTION,
        status=GoldenCaseStatus.EXECUTABLE,
        blind_input=BlindInput(goal="研究阿司匹林的合成路线")))
    store.save_reference("case-1", ReferenceMaterial(
        reference_id="ref-route", material_type=ReferenceMaterialKind.EXPERT_ROUTE,
        title="专家路线", content_locator="refs/route.txt",
        content_hash="sha256:aa", hidden=True))
    store.save_reference("case-1", ReferenceMaterial(
        reference_id="ref-si", material_type=ReferenceMaterialKind.SUPPORTING_INFORMATION,
        title="SI", content_locator="refs/si.txt",
        content_hash="sha256:bb", hidden=True))
    return store


def _request(**kwargs) -> UnsealRequest:
    kwargs.setdefault("case_id", "case-1")
    kwargs.setdefault("actor", "evaluator:张三")
    kwargs.setdefault("reason", "研究完成，进入专家对照评价阶段")
    return UnsealRequest(**kwargs)


# ---------------------------------------------------------------------------
# Sealed by default; explicit unseal with a full audit
# ---------------------------------------------------------------------------


def test_references_start_hidden(ready):
    assert all(r.hidden for r in ready.list_references("case-1"))


def test_a_named_reasoned_unseal_opens_and_records(ready, unseal):
    events = unseal.unseal(_request(reference_ids=["ref-route"]))

    assert len(events) == 1
    event = events[0]
    assert event.outcome is UnsealOutcome.UNSEALED
    assert event.actor == "evaluator:张三"
    assert event.reason == "研究完成，进入专家对照评价阶段"
    assert event.case_id == "case-1"
    assert event.case_version == "1"
    assert event.reference_id == "ref-route"
    assert event.previous_hidden is True and event.new_hidden is False
    assert event.at  # a timestamp exists

    stored = ready.get_reference("case-1", "ref-route")
    assert stored.hidden is False
    assert ready.get_reference("case-1", "ref-si").hidden is True, (
        "只解封点名的那一个")


def test_unseal_without_a_name_or_reason_cannot_be_constructed():
    with pytest.raises(ValueError):
        UnsealRequest(case_id="case-1", actor="", reason="x")
    with pytest.raises(ValueError):
        UnsealRequest(case_id="case-1", actor="a", reason="   ")
    with pytest.raises(ValueError):
        UnsealRequest(case_id="", actor="a", reason="r")


def test_unseal_all_opens_every_reference_and_audits_each(ready, unseal):
    events = unseal.unseal_all("case-1", actor="evaluator:李四",
                               reason="全部解封")

    assert {e.reference_id for e in events} == {"ref-route", "ref-si"}
    assert all(e.outcome is UnsealOutcome.UNSEALED for e in events)
    assert all(not r.hidden for r in ready.list_references("case-1"))


def test_a_repeat_unseal_is_recorded_and_changes_nothing(ready, unseal):
    unseal.unseal(_request(reference_ids=["ref-route"]))
    events = unseal.unseal(_request(reference_ids=["ref-route"]))

    assert events[0].outcome is UnsealOutcome.ALREADY_UNSEALED
    assert events[0].previous_hidden is False
    assert events[0].new_hidden is False
    history = ready.list_unseal_events("case-1", reference_id="ref-route")
    assert [e.outcome for e in history] == [
        UnsealOutcome.UNSEALED, UnsealOutcome.ALREADY_UNSEALED]
    assert history[0].event_id != history[1].event_id, "重复解封是新记录，不是改写"


def test_unsealing_an_unknown_reference_fails_and_leaves_history(
        ready, unseal):
    with pytest.raises(GoldenCaseError) as excinfo:
        unseal.unseal(_request(reference_ids=["ref-missing"]))
    assert excinfo.value.code == "REFERENCE_NOT_FOUND"

    # The refused attempt is still an audited event...
    events = ready.list_unseal_events("case-1")
    assert len(events) == 1
    assert events[0].outcome is UnsealOutcome.FAILED
    assert events[0].reference_id == "ref-missing"
    assert "ref-missing" in events[0].diagnostics
    assert events[0].actor == "evaluator:张三"
    # ...and nothing was opened.
    assert all(r.hidden for r in ready.list_references("case-1"))


def test_unsealing_against_a_wrong_case_is_typed(store, unseal):
    store.create(GoldenCase(case_id="case-1", name="A",
                            case_type=GoldenCaseType.BLIND_REPRODUCTION,
                            blind_input=BlindInput(goal="g")))
    store.create(GoldenCase(case_id="case-2", name="B",
                            case_type=GoldenCaseType.BLIND_REPRODUCTION,
                            blind_input=BlindInput(goal="g")))
    store.save_reference("case-2", ReferenceMaterial(
        reference_id="ref-1", material_type=ReferenceMaterialKind.PAPER,
        hidden=True))

    # The reference belongs to case-2, not case-1.
    with pytest.raises(GoldenCaseError) as excinfo:
        unseal.unseal(_request(case_id="case-1",
                               reference_ids=["ref-1"]))
    assert excinfo.value.code == "REFERENCE_NOT_FOUND"
    assert store.get_reference("case-2", "ref-1").hidden is True


def test_unsealing_an_empty_reference_list_on_a_referenceless_case(store,
                                                                   unseal):
    store.create(GoldenCase(case_id="case-1", name="A",
                            case_type=GoldenCaseType.BLIND_REPRODUCTION,
                            blind_input=BlindInput(goal="g")))

    with pytest.raises(GoldenCaseError) as excinfo:
        unseal.unseal(_request())
    assert excinfo.value.code == "REFERENCE_NOT_FOUND"


# ---------------------------------------------------------------------------
# The audit is append-only, and the manager view is not an unseal
# ---------------------------------------------------------------------------


def test_audit_events_survive_case_updates_and_cannot_be_rewritten(
        ready, unseal):
    unseal.unseal(_request(reference_ids=["ref-route"]))
    before = ready.list_unseal_events("case-1")

    # A content update (version bump) leaves the history byte-identical.
    case = ready.get("case-1")
    ready.update(case.model_copy(update={"case_version": "2"}))
    after = ready.list_unseal_events("case-1")

    assert after == before
    assert after[0].case_version == "1", "事件保留其发生时的版本"


def test_there_is_no_api_that_deletes_an_audit_event(store, unseal, kb):
    store.create(GoldenCase(case_id="case-1", name="A",
                            case_type=GoldenCaseType.BLIND_REPRODUCTION,
                            blind_input=BlindInput(goal="g")))
    store.save_reference("case-1", ReferenceMaterial(
        reference_id="ref-1", material_type=ReferenceMaterialKind.PAPER,
        hidden=True))
    unseal.unseal(_request(reference_ids=["ref-1"]))

    assert not hasattr(store, "delete_unseal_event")
    assert not hasattr(store, "update_unseal_event")
    # And the repository surface is equally append-only.
    assert not hasattr(kb._store, "update_unseal_event")
    assert not hasattr(kb._store, "delete_unseal_event")
    assert len(store.list_unseal_events("case-1")) == 1


def test_reading_the_manager_view_never_unseals(ready):
    view = ready.get_manager_view("case-1")

    assert [r.hidden for r in view.references] == [True, True]
    assert view.unseal_events == []
    assert all(r.hidden for r in ready.list_references("case-1"))


def test_saving_an_evaluation_never_unseals(ready):
    from crea.core.models.golden_case import (
        EvaluationResult, ResearchTrace, TaskStatus)

    ready.save_trace(ResearchTrace(trace_id="t1", case_id="case-1",
                                   case_version="1", status=TaskStatus.COMPLETED))
    ready.save_evaluation(EvaluationResult(
        evaluation_id="e1", case_id="case-1", case_version="1",
        trace_id="t1", evaluation_type=GoldenCaseType.BLIND_REPRODUCTION,
        evaluator="expert:x"))
    assert all(r.hidden for r in ready.list_references("case-1"))


def test_an_ordinary_reference_update_cannot_unseal_or_reseal(ready):
    stored = ready.get_reference("case-1", "ref-route")
    # A quiet attempt to mark it open through the metadata door:
    ready.save_reference("case-1",
                         stored.model_copy(update={"hidden": False}))
    assert ready.get_reference("case-1", "ref-route").hidden is True
    # And the reverse: an open reference cannot be quietly re-sealed.
    unseal = GoldenCaseUnsealService(ready)
    unseal.unseal(_request(reference_ids=["ref-route"]))
    opened = ready.get_reference("case-1", "ref-route")
    ready.save_reference("case-1",
                         opened.model_copy(update={"hidden": True}))
    assert ready.get_reference("case-1", "ref-route").hidden is False


# ---------------------------------------------------------------------------
# The agent view stays clean, sealed or not
# ---------------------------------------------------------------------------


def test_an_agent_view_never_carries_a_reference_sealed_or_open(ready,
                                                                unseal):
    sealed_view = ready.get_agent_view("case-1")
    unseal.unseal_all("case-1", actor="evaluator:张三", reason="对照评价")
    open_view = ready.get_agent_view("case-1")

    for view in (sealed_view, open_view):
        dumped = view.model_dump_json()
        assert "ref-route" not in dumped
        assert "专家路线" not in dumped
        assert "sha256:" not in dumped
        assert "refs/" not in dumped
    # A view fetched before the unseal is a snapshot: it does not grow
    # references retroactively.
    assert set(sealed_view.model_dump()) == set(open_view.model_dump())


def test_the_request_model_refuses_extra_smuggling():
    with pytest.raises(ValueError):
        UnsealRequest(case_id="c", actor="a", reason="r",
                      reference_ids=[], sneaky="field")

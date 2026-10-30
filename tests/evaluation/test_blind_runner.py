"""M1.7.2 -- the blind research runner: agent view in, trace out.

What is pinned here: `run_blind_case` touches the store through exactly
two doors (`get_agent_view`, `save_trace`) -- never the manager view,
never the unseal side; the runner object itself holds no store and
cannot reach a reference; the trace freezes the case_id, case_version
and session_id of the run it describes, and records the declared
environment, the executed steps with their observation kinds, the
re-planning events and the evidence the tools returned; a paused run
traces its pause as a warning, never as a user answer; and the ordinary
`CoreAgentRuntime` entry is untouched by any of this.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from pydantic import BaseModel, Field

from crea.agent.runtime import CoreAgentRuntime
from crea.agent.tools import ExecutionPolicy, ToolRegistry, ToolSpec
from crea.core.errors import GoldenCaseError
from crea.core.models.golden_case import (
    BlindInput,
    GoldenCase,
    GoldenCaseStatus,
    GoldenCaseType,
    ReferenceMaterial,
    ReferenceMaterialKind,
    ResearchTrace,
)
from crea.core.models.golden_case_access import GoldenCaseAgentView
from crea.core.models.knowledge import (
    ResearchPlan,
    ResearchPlanAction,
    ResearchPlanStep,
)
from crea.core.models.research import TaskStatus
from crea.core.models.research_intent import (
    EntityIdentityStatus,
    IntentEntity,
    MissingInformation,
    MissingKind,
    ResearchIntent,
    ResearchIntentKind,
)
from crea.evaluation import (
    BlindResearchRunner,
    RunEnvironment,
    run_blind_case,
)
from crea.services.golden_case_store import GoldenCaseStore
from crea.services.golden_case_unseal import GoldenCaseUnsealService
from crea.services.knowledge_base import KnowledgeBase
from crea.core.models.golden_case import (
    TraceDataSourceVersion,
    TraceModelInfo,
    TraceToolVersion,
)

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "crea"


class _Args(BaseModel):
    query: str = Field(default="")


class _Script:
    def __init__(self, payloads: list) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads.pop(0) if self.payloads else {"status": "ok"}
        if isinstance(payload, Exception):
            raise payload
        return payload


@pytest.fixture()
def kb(tmp_path):
    store = KnowledgeBase(tmp_path / "crea.db")
    store.initialize()
    yield store
    store.close()


@pytest.fixture()
def store(kb):
    return GoldenCaseStore(kb)


def _registry(*specs: ToolSpec) -> ToolRegistry:
    registry = ToolRegistry(enforce_policy=True, policy=ExecutionPolicy())
    for spec in specs:
        registry.register(spec)
    return registry


def _spec(name: str, run, **kwargs) -> ToolSpec:
    kwargs.setdefault("access", ("local",))
    return ToolSpec(name=name, description=name, arguments_model=_Args,
                    run=run, **kwargs)


def _factory(knowledge: _Script, literature: _Script):
    """A runtime factory whose tools match the deterministic base plan
    (assess -> search -> report), so a run completes end to end."""

    def build() -> CoreAgentRuntime:
        registry = _registry(
            _spec("get_existing_knowledge", knowledge),
            _spec("search_literature", literature),
        )
        return CoreAgentRuntime(registry)

    return build


def _create_case(store, *, goal="研究阿司匹林的合成路线",
                 intent: ResearchIntent | None = None) -> None:
    store.create(GoldenCase(
        case_id="case-1", name="阿司匹林盲测",
        case_type=GoldenCaseType.BLIND_REPRODUCTION,
        case_version="1", status=GoldenCaseStatus.EXECUTABLE,
        blind_input=BlindInput(goal=goal, intent=intent)))
    store.save_reference("case-1", ReferenceMaterial(
        reference_id="ref-route",
        material_type=ReferenceMaterialKind.EXPERT_ROUTE,
        title="专家路线", content_locator="refs/route.txt",
        content_hash="sha256:aa", hidden=True))


ENV = RunEnvironment(
    models=[TraceModelInfo(name="test-model", version="1", provider="x")],
    tools=[TraceToolVersion(tool="search_literature", version="2")],
    data_sources=[TraceDataSourceVersion(source="openalex", version="2026")],
    git_commit="5784ac5")


# ---------------------------------------------------------------------------
# F. Runner isolation
# ---------------------------------------------------------------------------


def test_run_blind_case_touches_only_two_store_doors(store):
    _create_case(store)
    knowledge, literature = _Script([{"status": "ok"}]), _Script([
        {"status": "ok", "evidence_ids": ["ev-1"]}])

    used: list[str] = []
    original_get_agent_view = store.get_agent_view
    original_save_trace = store.save_trace

    def spy_view(case_id):
        used.append("get_agent_view")
        return original_get_agent_view(case_id)

    def spy_save(trace):
        used.append("save_trace")
        return original_save_trace(trace)

    store.get_agent_view = spy_view
    store.save_trace = spy_save
    outcome = run_blind_case(store, "case-1",
                             runtime_factory=_factory(knowledge, literature),
                             environment=ENV)

    assert used == ["get_agent_view", "save_trace"]
    # And the store still offers the doors the runner must never open.
    assert hasattr(store, "get_manager_view")
    assert hasattr(store, "get")
    assert outcome.trace.case_id == "case-1"


def test_the_runner_object_holds_no_store_and_sees_only_the_view(store):
    _create_case(store)
    runner = BlindResearchRunner(_factory(_Script([{"status": "ok"}]),
                                          _Script([{"status": "ok"}])))
    assert not hasattr(runner, "_store")
    assert not hasattr(runner, "store")

    view = store.get_agent_view("case-1")
    assert isinstance(view, GoldenCaseAgentView)
    outcome = runner.run(view, environment=ENV)

    # The runner ran from the view: the trace echoes the view's identity.
    assert outcome.trace.case_id == view.case_id
    assert outcome.trace.case_version == view.case_version
    dumped = view.model_dump_json()
    assert "ref-route" not in dumped and "专家路线" not in dumped


def test_the_runner_module_never_imports_the_unseal_or_content_side():
    """Structural, not by promise: the runner cannot call what it cannot
    name. The access *models* (`GoldenCaseAgentView`) are the runner's
    input type and fair game; the store is named only by the
    orchestration function's signature; the unseal and content
    *services* -- the sealed side -- must stay unnameable here."""
    path = SRC / "evaluation" / "blind_runner.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for banned in ("golden_case_unseal", "reference_content",
                   "repositories"):
        assert not any(banned in name for name in imported), (
            f"blind_runner.py imports {banned}")
    # The runner *class* never names the store; only run_blind_case's
    # signature does. Checked on the class's own annotations (not its
    # prose): no attribute of BlindResearchRunner is typed on the store.
    cls_node = next(node for node in tree.body
                    if isinstance(node, ast.ClassDef)
                    and node.name == "BlindResearchRunner")
    for node in ast.walk(cls_node):
        if isinstance(node, ast.arg) and node.annotation is not None:
            assert "Store" not in ast.unparse(node.annotation)
        if isinstance(node, ast.AnnAssign):
            assert "Store" not in ast.unparse(node)


def test_the_agent_cannot_reach_the_manager_view_through_the_runner(store):
    _create_case(store)
    runner = BlindResearchRunner(_factory(_Script([{"status": "ok"}]),
                                          _Script([{"status": "ok"}])))
    view = store.get_agent_view("case-1")

    assert not hasattr(view, "references")
    assert not hasattr(view, "case")
    assert not hasattr(view, "unseal_events")
    assert not hasattr(runner, "unseal")
    assert not hasattr(runner, "get_manager_view")
    with pytest.raises(TypeError):
        runner.run(view, unseal=True)  # no such path exists


# ---------------------------------------------------------------------------
# Trace fidelity
# ---------------------------------------------------------------------------


def test_the_trace_records_case_session_and_environment(store):
    _create_case(store)
    knowledge = _Script([{"status": "ok", "evidence_ids": ["ev-k"]}])
    literature = _Script([{"status": "ok", "evidence_ids": ["ev-1"]}])
    outcome = run_blind_case(store, "case-1",
                             runtime_factory=_factory(knowledge, literature),
                             environment=ENV, session_id="sess-9")

    trace = outcome.trace
    assert isinstance(trace, ResearchTrace)
    assert trace.case_id == "case-1"
    assert trace.case_version == "1"
    assert trace.session_id == "sess-9"
    assert trace.status is TaskStatus.COMPLETED
    assert trace.started_at <= trace.ended_at
    assert trace.git_commit == "5784ac5"
    assert trace.models[0].name == "test-model"
    assert trace.tools[0].version == "2"
    assert trace.data_sources[0].source == "openalex"


def test_the_trace_freezes_the_version_the_view_froze(store):
    _create_case(store)
    outcome = run_blind_case(store, "case-1",
                             runtime_factory=_factory(
                                 _Script([{"status": "ok"}]),
                                 _Script([{"status": "ok"}])))
    # The case then moves on.
    case = store.get("case-1")
    store.update(case.model_copy(update={"case_version": "2"}))

    stored = store.get_trace(outcome.trace.trace_id)
    assert stored.case_version == "1", "轨迹保留其运行时版本"
    assert store.get_version("case-1") == "2"
    assert outcome.trace.trace_id.startswith("trace-")


def test_the_trace_records_steps_observations_and_evidence(store):
    _create_case(store)
    knowledge = _Script([{"status": "ok"}])
    literature = _Script([{"status": "ok", "evidence_ids": ["ev-1"]}])
    outcome = run_blind_case(store, "case-1",
                             runtime_factory=_factory(knowledge, literature))

    trace = outcome.trace
    assert [c.tool for c in trace.tool_calls] == [
        "get_existing_knowledge", "search_literature"]
    assert all(c.observation_kind == "success" for c in trace.tool_calls)
    assert trace.evidence_ids == ["ev-1"]
    assert trace.plan is not None
    assert [s.step_id for s in trace.steps] == [
        "assess-knowledge", "search-literature", "report"]


def test_a_replanning_event_reaches_the_trace(store):
    """A tool that refuses without an identity makes the planner add a
    resolve step -- an `add_step` change the trace must record."""
    _create_case(store)
    knowledge = _Script([{"status": "ok"}])
    literature = _Script([{"status": "blocked",
                           "chemistry_allowed": False}])
    resolve_calls = _Script([{"status": "ok", "chemistry_allowed": True}])

    def build() -> CoreAgentRuntime:
        registry = _registry(
            _spec("get_existing_knowledge", knowledge),
            _spec("search_literature", literature),
            _spec("resolve_chemical_identity", resolve_calls),
        )
        return CoreAgentRuntime(registry)

    outcome = run_blind_case(store, "case-1", runtime_factory=build)

    assert any("add_step" in change.change_kinds
               for change in outcome.trace.plan_changes), (
        "计划变更进入轨迹")
    assert resolve_calls.calls, "身份前置步骤确实执行了"


def test_a_failed_run_traces_as_failed_not_smoothed_over(store):
    _create_case(store)
    knowledge = _Script([RuntimeError("boom")])
    literature = _Script([{"status": "error", "error": "upstream broke"}])
    outcome = run_blind_case(store, "case-1",
                             runtime_factory=_factory(knowledge, literature))

    trace = outcome.trace
    assert outcome.result.outcome == "failed"
    assert trace.failures, "失败原因进入轨迹"
    assert trace.status is TaskStatus.FAILED
    kinds = {c.observation_kind for c in trace.tool_calls}
    assert "error" in kinds, "轨迹记录错误观察，不粉饰"


def test_a_paused_run_records_the_pause_as_a_warning_not_an_answer(store):
    _create_case(store)
    knowledge = _Script([{"status": "ok"}])
    literature = _Script([{"status": "ambiguous", "chemistry_allowed": False}])
    outcome = run_blind_case(store, "case-1",
                             runtime_factory=_factory(knowledge, literature))

    trace = outcome.trace
    assert outcome.result.outcome == "input_required"
    assert any("paused" in w for w in trace.warnings)
    assert trace.user_inputs == [], "没有回答的问题不是用户输入"
    saved = store.get_trace(trace.trace_id)
    assert saved.warnings == trace.warnings


def test_the_session_id_comes_back_from_the_runtime_when_not_given(store):
    _create_case(store)
    outcome = run_blind_case(store, "case-1",
                             runtime_factory=_factory(
                                 _Script([{"status": "ok"}]),
                                 _Script([{"status": "ok"}])))

    assert outcome.result.session_id.startswith("sess-")
    assert outcome.trace.session_id == outcome.result.session_id


def test_a_case_whose_intent_is_inconsistent_never_runs(store):
    _create_case(store, intent=ResearchIntent(
        raw_input="研究X", goal="研究X",
        primary_intent=ResearchIntentKind.RETROSYNTHESIS,
        # A resolved entity with no kind/value: inconsistent on purpose.
        entities=[IntentEntity(name="X",
                               identity_status=EntityIdentityStatus.RESOLVED)],
        validated=False))

    with pytest.raises(GoldenCaseError) as excinfo:
        run_blind_case(store, "case-1", runtime_factory=_factory(
            _Script([{"status": "ok"}]), _Script([{"status": "ok"}])))
    assert excinfo.value.code == "CASE_INPUT_INVALID"
    assert store.list_traces("case-1") == [], "拒绝的运行不产生轨迹"


def test_an_intent_bearing_case_travels_through_the_m16_bridge(store):
    _create_case(store, intent=ResearchIntent(
        raw_input="研究阿司匹林的合成路线", goal="研究阿司匹林的合成路线",
        primary_intent=ResearchIntentKind.RETROSYNTHESIS, validated=True,
        missing=[MissingInformation(kind="missing_target",
                                    severity=MissingKind.DEFERRABLE)]))
    knowledge, literature = _Script([{"status": "ok"}]), _Script([
        {"status": "ok"}])
    seen: dict = {}

    def build() -> CoreAgentRuntime:
        registry = _registry(
            _spec("get_existing_knowledge", knowledge),
            _spec("search_literature", literature),
            _spec("retrosynthesize",
                  _Script([{"status": "no_result"}])),
        )
        runtime = CoreAgentRuntime(registry)
        original_run = runtime.run

        def capture(goal, **kwargs):
            seen.update(kwargs)
            seen["goal"] = goal
            return original_run(goal, **kwargs)

        runtime.run = capture
        return runtime

    outcome = run_blind_case(store, "case-1", runtime_factory=build)

    assert seen["wants_routes"] is True
    assert seen["context"]["golden_case_id"] == "case-1"
    assert seen["context"]["golden_case_version"] == "1"
    assert outcome.trace.status in (TaskStatus.COMPLETED, TaskStatus.PARTIAL)


# ---------------------------------------------------------------------------
# G. Architecture: the ordinary entry is untouched, layers hold
# ---------------------------------------------------------------------------


def test_the_ordinary_runtime_entry_needs_no_golden_case_anything():
    """CoreAgentRuntime.run works exactly as before: the runner is a new
    caller, not a change to the runtime."""
    knowledge = _Script([{"status": "ok"}])
    literature = _Script([{"status": "ok"}])
    registry = _registry(
        _spec("get_existing_knowledge", knowledge),
        _spec("search_literature", literature),
    )
    result = CoreAgentRuntime(registry).run("普通研究入口")

    assert result.status is TaskStatus.COMPLETED
    assert result.session_id.startswith("sess-")
    assert len(knowledge.calls) == 1
    assert len(literature.calls) == 1


def test_the_evaluation_package_imports_nothing_below_its_layer():
    forbidden = ("sqlite3", "rdkit", "crea.repositories.sqlite",
                 "crea.providers", "crea.sources", "httpx", "requests")
    for path in (SRC / "evaluation").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: set[str] = set()
            if isinstance(node, ast.Import):
                names = {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module}
            for name in names:
                assert not any(name == f or name.startswith(f + ".")
                               for f in forbidden), (
                    f"{path.name} imports {name}")


def test_a_blind_run_and_the_unseal_side_share_only_the_store(store):
    """End-to-end: run blind, verify the trace, then unseal -- the two
    flows meet at the store and nowhere else."""
    _create_case(store)
    outcome = run_blind_case(store, "case-1", runtime_factory=_factory(
        _Script([{"status": "ok"}]), _Script([{"status": "ok"}])))

    # The manager view can now see the trace; the reference is still sealed.
    manager = store.get_manager_view("case-1")
    assert [t.trace_id for t in manager.traces] == [outcome.trace.trace_id]
    assert manager.references[0].hidden is True

    events = GoldenCaseUnsealService(store).unseal_all(
        "case-1", actor="evaluator:张三", reason="研究完成，进入对照评价")
    assert len(events) == 1
    assert store.get_manager_view("case-1").references[0].hidden is False
    # And the agent view is still clean after the unseal.
    assert "ref-route" not in store.get_agent_view(
        "case-1").model_dump_json()

"""M1.7.1 -- the golden-case data contract: five objects, one boundary.

What is pinned here: the five models exist and validate; the three version
layers (schema / case content / rubric) stay separate; `BlindInput`
reuses M1.6's intent models and the identity layer's `StructureInput`
rather than growing a second understanding vocabulary; `ResearchTrace`
reuses the plan/step models and records the environment; `EvaluationResult`
speaks five verdicts across ten dimensions and never forces a score. And
the isolation this stage *can* enforce, enforced structurally: a
`GoldenCase` serializes to ids, never to reference metadata or body text;
`BlindInput` has no field that reaches a reference; no model in the schema
has any field that could hold paper text at all.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from pydantic import ValidationError

from crea.core.models.golden_case import (
    GOLDEN_CASE_SCHEMA_VERSION,
    AnswerContentKind,
    BlindInput,
    CaseConstraints,
    DimensionEvaluation,
    EvaluationConfig,
    EvaluationDimension,
    EvaluationResult,
    EvaluationStatus,
    GoldenCase,
    GoldenCaseStatus,
    GoldenCaseType,
    ReferenceAccess,
    ReferenceMaterial,
    ReferenceMaterialKind,
    ResearchTrace,
    ResourceScope,
    TraceDataSourceVersion,
    TraceModelInfo,
    TracePlanChange,
    TraceProduct,
    TraceToolCall,
    TraceToolVersion,
    TraceUserInput,
)
from crea.core.models.identity import StructureInput
from crea.core.models.knowledge import (
    ResearchPlan,
    ResearchPlanAction,
    ResearchPlanStep,
)
from crea.core.models.research import ResearchStep, TaskStatus, ToolStatus
from crea.core.models.research_intent import (
    EntityIdentityStatus,
    IntentEntity,
    IntentSource,
    ResearchConstraint,
    ResearchIntent,
    ResearchIntentKind,
)

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "crea"


def _intent() -> ResearchIntent:
    return ResearchIntent(
        raw_input="研究阿司匹林的合成路线，近五年文献",
        goal="研究阿司匹林的合成路线",
        primary_intent=ResearchIntentKind.RETROSYNTHESIS,
        entities=[IntentEntity(
            name="阿司匹林", kind="smiles",
            value="CC(=O)Oc1ccccc1C(=O)O",
            identity_status=EntityIdentityStatus.NAME_RECOGNIZED)],
        constraints=[
            ResearchConstraint(kind="time_range", value="近五年",
                               source=IntentSource.USER_EXPLICIT),
            ResearchConstraint(kind="scope", value="绿色路线",
                               source=IntentSource.MODEL_INFERRED),
        ],
        validated=True,
    )


def _blind_input() -> BlindInput:
    return BlindInput(
        goal="研究阿司匹林的合成路线，近五年文献",
        intent=_intent(),
        target_structure=StructureInput.of(kind="SMILES",
                                           value="CC(=O)Oc1ccccc1C(=O)O"),
        resource_scope=ResourceScope(allowed_tools=["search_literature"],
                                     allowed_data_sources=["openalex"],
                                     offline_only=True),
    )


def _reference(marker: str = "HIDDEN-ANSWER-MARKER") -> ReferenceMaterial:
    return ReferenceMaterial(
        reference_id="ref-expert-1",
        material_type=ReferenceMaterialKind.EXPERT_ROUTE,
        title=f"Expert route for aspirin ({marker})",
        doi="10.1000/example.doi",
        content_locator=f"sealed://cases/aspirin/route.txt#{marker}",
        content_hash="sha256:deadbeef",
        answer_content=[AnswerContentKind.EXPERT_ROUTE,
                        AnswerContentKind.YIELDS],
        hidden=True,
        access=ReferenceAccess.EVALUATION_MANAGER_ONLY,
    )


def _plan() -> ResearchPlan:
    return ResearchPlan(
        goal="研究阿司匹林的合成路线",
        steps=[
            ResearchPlanStep(action=ResearchPlanAction.RESOLVE_IDENTITY,
                             step_id="s1", tool="resolve_chemical_identity"),
            ResearchPlanStep(action=ResearchPlanAction.REPORT, step_id="report"),
        ],
        revision=1,
    )


def _trace() -> ResearchTrace:
    return ResearchTrace(
        trace_id="trace-1", case_id="case-1", case_version="1",
        session_id="sess-1", started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:10:00+00:00",
        status=TaskStatus.PARTIAL,
        models=[TraceModelInfo(name="test-model", version="1",
                               provider="example", knowledge_cutoff="2024-06")],
        tools=[TraceToolVersion(tool="resolve_chemical_identity", version="1")],
        data_sources=[TraceDataSourceVersion(source="openalex", version="2026-01")],
        git_commit="567a498",
        plan=_plan(), plan_revision=1,
        steps=[ResearchStep(step_id="s1", index=0,
                            tool="resolve_chemical_identity",
                            result_status=ToolStatus.OK)],
        tool_calls=[TraceToolCall(step_id="s1", tool="resolve_chemical_identity",
                                  observation_kind="success",
                                  summary="resolved", evidence_ids=["ev-1"])],
        user_inputs=[TraceUserInput(step_id="s1", question="哪个结构？",
                                    answer="用户说：邻位产物",
                                    kind="approval")],
        plan_changes=[TracePlanChange(plan_revision=1,
                                      change_kinds=["add_step"],
                                      reason="身份未解析，补前置步骤")],
        products=[TraceProduct(kind="report", ref="report-1", label="研究报告")],
        evidence_ids=["ev-1"],
        warnings=["一条文献没有全文"],
        failures=["retrosynthesis provider unavailable"],
    )


def _evaluation(status: EvaluationStatus = EvaluationStatus.PASS
                ) -> EvaluationResult:
    return EvaluationResult(
        evaluation_id="eval-1", case_id="case-1", case_version="1",
        trace_id="trace-1", evaluation_version="rubric-1",
        evaluation_type=GoldenCaseType.BLIND_REPRODUCTION,
        evaluator="expert:张三", status=status,
        dimensions=[
            DimensionEvaluation(
                dimension=EvaluationDimension.TARGET_IDENTITY,
                verdict=EvaluationStatus.PASS,
                note="目标分子一致", evidence_ids=["ref-expert-1"]),
            DimensionEvaluation(
                dimension=EvaluationDimension.CONDITION_SUPPORT,
                verdict=EvaluationStatus.INSUFFICIENT_EVIDENCE,
                note="条件部分缺失", score=None, score_basis=""),
            DimensionEvaluation(
                dimension=EvaluationDimension.ROUTE_CHEMISTRY,
                verdict=EvaluationStatus.PARTIAL,
                note="路线合理但步骤冗余", score=0.7,
                score_basis="三步中两步有文献支持"),
        ],
        rationale="目标与断键正确，条件证据不足",
        evidence_ids=["ref-expert-1", "ev-1"],
        unresolved_issues=["高压步骤缺压力数据"],
        warnings=["论文未开放获取"],
    )


# ---------------------------------------------------------------------------
# GoldenCase: metadata, enums, ids, and the three version layers
# ---------------------------------------------------------------------------


def test_a_golden_case_carries_its_metadata_and_honest_defaults():
    case = GoldenCase(case_id="case-1", name="阿司匹林盲测",
                      case_type=GoldenCaseType.BLIND_REPRODUCTION,
                      description="一篇 2019 年论文的路线复现",
                      tags=["aspirin", "blind"], domains=["药物合成"])

    assert case.schema_version == GOLDEN_CASE_SCHEMA_VERSION
    assert case.status is GoldenCaseStatus.DRAFT
    assert case.case_version == "1"
    assert case.reference_ids == [], "草稿案例默认不引用任何资料"
    assert case.blind_input is None
    assert case.evaluation_config.dimensions == list(EvaluationDimension)
    assert case.constraints.model_policy == "record_only"


def test_the_case_type_is_exactly_the_two_regimes():
    assert {t.value for t in GoldenCaseType} == {
        "blind_reproduction", "open_literature_research"}
    with pytest.raises(ValidationError):
        GoldenCase(case_id="c", name="n", case_type="secret_agent_mission")


def test_the_case_status_walks_the_five_lifecycle_values():
    assert {s.value for s in GoldenCaseStatus} == {
        "draft", "ready", "executable", "completed", "archived"}
    with pytest.raises(ValidationError):
        GoldenCase(case_id="c", name="n",
                   case_type=GoldenCaseType.BLIND_REPRODUCTION,
                   status="running")


def test_the_case_id_is_stable_for_its_slot_and_differs_per_case():
    a = GoldenCase.build("阿司匹林盲测", GoldenCaseType.BLIND_REPRODUCTION)
    same = GoldenCase.build("阿司匹林盲测", GoldenCaseType.BLIND_REPRODUCTION)
    other = GoldenCase.build("其他案例", GoldenCaseType.BLIND_REPRODUCTION)
    other_type = GoldenCase.build("阿司匹林盲测",
                                  GoldenCaseType.OPEN_LITERATURE_RESEARCH)

    assert a.case_id == same.case_id, "同一案例槽位的 id 稳定"
    assert a.case_id.startswith("case-")
    assert a.case_id != other.case_id
    assert a.case_id != other_type.case_id


def test_editing_case_content_keeps_the_id_and_moves_only_the_case_version():
    v1 = GoldenCase.build("阿司匹林盲测", GoldenCaseType.BLIND_REPRODUCTION)
    v2 = GoldenCase.build("阿司匹林盲测", GoldenCaseType.BLIND_REPRODUCTION,
                          case_version="2", description="补充了资源约束")

    assert v2.case_id == v1.case_id
    assert v2.case_version == "2" and v1.case_version == "1"
    assert v2.schema_version == v1.schema_version == GOLDEN_CASE_SCHEMA_VERSION


def test_an_unnamed_or_unidentifiable_case_cannot_be_built():
    with pytest.raises(ValidationError):
        GoldenCase(case_id="", name="n",
                   case_type=GoldenCaseType.BLIND_REPRODUCTION)
    with pytest.raises(ValidationError):
        GoldenCase(case_id="c", name="   ",
                   case_type=GoldenCaseType.BLIND_REPRODUCTION)


# ---------------------------------------------------------------------------
# BlindInput: the visible half, in reused vocabularies
# ---------------------------------------------------------------------------


def test_a_blind_input_carries_the_research_question():
    inp = BlindInput(goal="比较布洛芬与阿司匹林的合成路线")

    assert inp.goal == "比较布洛芬与阿司匹林的合成路线"
    assert inp.intent is None and inp.target_structure is None
    assert inp.source is IntentSource.USER_EXPLICIT
    assert inp.input_version == "1"


def test_a_blind_input_carries_a_target_structure_in_the_identity_vocabulary():
    inp = _blind_input()

    assert isinstance(inp.target_structure, StructureInput)
    assert inp.target_structure.kind.value == "smiles"
    assert inp.target_structure.value == "CC(=O)Oc1ccccc1C(=O)O"


def test_a_blind_input_without_any_structure_is_valid():
    inp = BlindInput(goal="综述近五年的 C-H 活化方法",
                     intent=ResearchIntent(raw_input="综述近五年的 C-H 活化方法",
                                           goal="综述近五年的 C-H 活化方法",
                                           primary_intent=(
                                               ResearchIntentKind.LITERATURE_RESEARCH)))

    assert inp.target_structure is None
    assert inp.intent is not None


def test_constraint_sources_survive_verbatim_into_the_blind_input():
    inp = _blind_input()

    sources = {c.kind: c.source for c in inp.intent.constraints}
    assert sources == {"time_range": IntentSource.USER_EXPLICIT,
                       "scope": IntentSource.MODEL_INFERRED}


def test_the_blind_input_reuses_the_m16_intent_and_entity_models():
    inp = _blind_input()

    assert isinstance(inp.intent, ResearchIntent)
    entity = inp.intent.entity("阿司匹林")
    assert isinstance(entity, IntentEntity)
    assert entity.identity_status is EntityIdentityStatus.NAME_RECOGNIZED


def test_an_empty_goal_is_not_an_input():
    with pytest.raises(ValidationError):
        BlindInput(goal="   ")


def test_the_blind_input_has_no_field_that_reaches_a_reference():
    """The visible half names no reference, id or answer slot -- checked
    on the schema, not on a sample object."""
    assert set(BlindInput.model_fields) == {
        "goal", "intent", "target_structure", "resource_scope",
        "source", "input_version", "created_at",
    }


# ---------------------------------------------------------------------------
# ReferenceMaterial: metadata, sealing, and no body-text slot
# ---------------------------------------------------------------------------


def test_paper_metadata_and_public_access():
    paper = ReferenceMaterial(
        reference_id="ref-paper-1", material_type=ReferenceMaterialKind.PAPER,
        title="A green route to aspirin", doi="10.1000/paper",
        authors=["A. Author", "B. Author"], published_at="2019-05-01",
        access=ReferenceAccess.PUBLIC, hidden=False)

    assert paper.material_type is ReferenceMaterialKind.PAPER
    assert paper.authors == ["A. Author", "B. Author"]
    assert paper.access is ReferenceAccess.PUBLIC and not paper.hidden


def test_supporting_information_is_its_own_material_kind():
    si = ReferenceMaterial(
        reference_id="ref-si-1",
        material_type=ReferenceMaterialKind.SUPPORTING_INFORMATION,
        title="SI for aspirin route", doi="10.1000/si",
        answer_content=[AnswerContentKind.EXPERIMENTAL_CONDITIONS,
                        AnswerContentKind.YIELDS])

    assert si.material_type is ReferenceMaterialKind.SUPPORTING_INFORMATION
    assert set(si.answer_content) == {AnswerContentKind.EXPERIMENTAL_CONDITIONS,
                                      AnswerContentKind.YIELDS}


def test_an_expert_route_material_is_sealed_by_default():
    route = ReferenceMaterial(
        reference_id="ref-route-1",
        material_type=ReferenceMaterialKind.EXPERT_ROUTE,
        title="专家路线", content_locator="sealed://r/1",
        content_hash="sha256:00ff",
        answer_content=[AnswerContentKind.EXPERT_ROUTE])

    assert route.hidden is True, "参考材料默认隐藏"
    assert route.access is ReferenceAccess.EVALUATION_MANAGER_ONLY


def test_every_access_state_is_representable():
    states = {a.value for a in ReferenceAccess}
    assert states == {"public", "evaluation_manager_only",
                      "authorized_for_agent", "inaccessible", "unknown"}
    with pytest.raises(ValidationError):
        ReferenceMaterial(reference_id="r",
                          material_type=ReferenceMaterialKind.PAPER,
                          access="everyone")


def test_a_reference_records_a_locator_and_a_hash_but_holds_no_body():
    ref = _reference()

    assert ref.content_locator.startswith("sealed://")
    assert ref.content_hash == "sha256:deadbeef"
    assert ref.provenance is None
    # The schema itself cannot carry text: no body field exists at all.
    assert not set(ReferenceMaterial.model_fields) & {
        "content", "body", "text", "fulltext", "raw_quote", "body_text"}


def test_the_built_reference_id_is_stable_for_the_same_work():
    a = ReferenceMaterial.build(ReferenceMaterialKind.PAPER, doi="10.1/x")
    b = ReferenceMaterial.build(ReferenceMaterialKind.PAPER, doi="10.1/X")
    other = ReferenceMaterial.build(ReferenceMaterialKind.PAPER, doi="10.2/x")

    assert a.reference_id == b.reference_id
    assert a.reference_id != other.reference_id
    assert a.reference_id.startswith("ref-")


# ---------------------------------------------------------------------------
# ResearchTrace: the process, not just the product
# ---------------------------------------------------------------------------


def test_a_trace_records_the_execution_metadata():
    trace = _trace()

    assert trace.case_id == "case-1" and trace.case_version == "1"
    assert trace.session_id == "sess-1"
    assert trace.started_at < trace.ended_at
    assert trace.status is TaskStatus.PARTIAL
    assert trace.git_commit == "567a498"


def test_a_trace_reuses_the_existing_plan_and_step_models():
    trace = _trace()

    assert isinstance(trace.plan, ResearchPlan)
    assert [s.step_id for s in trace.plan.steps] == ["s1", "report"]
    assert isinstance(trace.steps[0], ResearchStep)
    assert trace.steps[0].result_status is ToolStatus.OK
    assert trace.plan_revision == 1


def test_tool_calls_carry_the_observation_classification():
    trace = _trace()

    call = trace.tool_calls[0]
    assert call.observation_kind == "success", "M1.2 六值分类以数据记录"
    assert call.evidence_ids == ["ev-1"]
    assert {c.observation_kind for c in trace.tool_calls} <= {
        "success", "no_result", "needs_input", "unavailable", "partial",
        "error"}


def test_person_inputs_and_replanning_events_are_recorded():
    trace = _trace()

    assert trace.user_inputs[0].kind == "approval"
    assert trace.user_inputs[0].answer == "用户说：邻位产物"
    assert trace.plan_changes[0].change_kinds == ["add_step"]
    assert "补前置步骤" in trace.plan_changes[0].reason


def test_model_tool_and_data_source_versions_are_recorded():
    trace = _trace()

    assert trace.models[0].knowledge_cutoff == "2024-06"
    assert trace.tools[0].tool == "resolve_chemical_identity"
    assert trace.data_sources[0] == TraceDataSourceVersion(
        source="openalex", version="2026-01")


def test_failures_and_warnings_are_kept_apart():
    trace = _trace()

    assert trace.failures == ["retrosynthesis provider unavailable"]
    assert trace.warnings == ["一条文献没有全文"]


def test_products_and_evidence_are_referenced_not_copied():
    trace = _trace()

    assert trace.products[0] == TraceProduct(kind="report", ref="report-1",
                                            label="研究报告")
    assert trace.evidence_ids == ["ev-1"]


def test_a_trace_without_identity_cannot_be_built():
    with pytest.raises(ValidationError):
        ResearchTrace(trace_id="", case_id="case-1", case_version="1")
    with pytest.raises(ValidationError):
        ResearchTrace(trace_id="t", case_id="", case_version="1")


# ---------------------------------------------------------------------------
# EvaluationResult: five verdicts, ten dimensions, no forced number
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", list(EvaluationStatus))
def test_every_verdict_state_is_representable(status):
    result = _evaluation(status)

    assert result.status is status


def test_an_unfilled_evaluation_decides_nothing():
    result = EvaluationResult(
        evaluation_id="eval-2", case_id="case-1", case_version="1",
        trace_id="trace-1",
        evaluation_type=GoldenCaseType.OPEN_LITERATURE_RESEARCH)

    assert result.status is EvaluationStatus.NOT_EVALUABLE
    assert result.dimensions == [] and result.evidence_ids == []
    assert result.score is None and result.score_basis == ""


def test_dimensions_carry_verdicts_notes_and_evidence():
    result = _evaluation()

    assert result.verdict_for(EvaluationDimension.TARGET_IDENTITY) is (
        EvaluationStatus.PASS)
    assert result.verdict_for(EvaluationDimension.CONDITION_SUPPORT) is (
        EvaluationStatus.INSUFFICIENT_EVIDENCE)
    assert result.verdict_for(EvaluationDimension.ROUTE_CHEMISTRY) is (
        EvaluationStatus.PARTIAL)
    assert result.dimensions[0].evidence_ids == ["ref-expert-1"]


def test_an_unaddressed_dimension_never_reads_as_a_pass():
    result = _evaluation()

    assert result.verdict_for(EvaluationDimension.TRACEABILITY) is (
        EvaluationStatus.NOT_EVALUABLE), "未评价的维度不是通过"


def test_a_missing_score_is_honest_and_a_present_score_carries_its_basis():
    result = _evaluation()

    missing = next(d for d in result.dimensions
                   if d.dimension is EvaluationDimension.CONDITION_SUPPORT)
    scored = next(d for d in result.dimensions
                  if d.dimension is EvaluationDimension.ROUTE_CHEMISTRY)
    assert missing.score is None and missing.score_basis == ""
    assert scored.score == 0.7 and "文献支持" in scored.score_basis


def test_two_evaluation_versions_coexist_for_one_trace():
    under_old = _evaluation()
    under_new = EvaluationResult(
        evaluation_id="eval-3", case_id="case-1", case_version="1",
        trace_id="trace-1", evaluation_version="rubric-2",
        evaluation_type=GoldenCaseType.BLIND_REPRODUCTION,
        evaluator="rule:v2", status=EvaluationStatus.PARTIAL)

    assert under_old.evaluation_version == "rubric-1"
    assert under_new.evaluation_version == "rubric-2"
    assert under_old.trace_id == under_new.trace_id


def test_an_evaluation_without_identity_cannot_be_built():
    with pytest.raises(ValidationError):
        EvaluationResult(evaluation_id="", case_id="c", case_version="1",
                         trace_id="t",
                         evaluation_type=GoldenCaseType.BLIND_REPRODUCTION)


def test_the_evaluation_type_names_the_regime():
    assert {t.value for t in GoldenCaseType} == {
        e.value for e in {GoldenCaseType.BLIND_REPRODUCTION,
                          GoldenCaseType.OPEN_LITERATURE_RESEARCH}}
    result = _evaluation()
    assert result.evaluation_type is GoldenCaseType.BLIND_REPRODUCTION


# ---------------------------------------------------------------------------
# The isolation this stage can enforce, enforced by structure
# ---------------------------------------------------------------------------


def test_a_golden_case_serializes_to_ids_and_never_to_reference_metadata():
    """A full case + its sealed reference: the case's own JSON contains
    the reference's *id* and not one byte of its metadata or marker."""
    ref = _reference()
    case = GoldenCase.build(
        "阿司匹林盲测", GoldenCaseType.BLIND_REPRODUCTION,
        blind_input=_blind_input(), reference_ids=[ref.reference_id])

    dumped = case.model_dump_json()
    assert ref.reference_id in dumped
    assert "HIDDEN-ANSWER-MARKER" not in dumped
    assert "Expert route" not in dumped, "参考资料标题不得进入案例序列化"
    assert "sealed://" not in dumped, "定位符不得进入案例序列化"


def test_a_blind_run_receives_exactly_the_visible_half():
    """What a runner would hand the agent -- case.blind_input -- contains
    no reference id, no marker, and no field that could name one."""
    ref = _reference()
    case = GoldenCase.build(
        "阿司匹林盲测", GoldenCaseType.BLIND_REPRODUCTION,
        blind_input=_blind_input(), reference_ids=[ref.reference_id])

    visible = case.blind_input.model_dump_json()
    assert "HIDDEN-ANSWER-MARKER" not in visible
    assert ref.reference_id not in visible
    assert "ref-" not in visible


def test_the_case_object_cannot_embed_a_reference_material():
    """Checked on the schema: no field of GoldenCase has (or could have
    via its declared type) a ReferenceMaterial."""
    assert set(GoldenCase.model_fields) == {
        "case_id", "name", "description", "case_type", "case_version",
        "schema_version", "status", "created_at", "updated_at", "tags",
        "domains", "blind_input", "reference_ids", "evaluation_config",
        "constraints", "provenance",
    }
    assert GoldenCase.model_fields["reference_ids"].annotation == list[str]


def test_no_model_in_this_schema_has_a_field_that_could_hold_body_text():
    for cls_ in (GoldenCase, BlindInput, ReferenceMaterial, ResearchTrace,
                 EvaluationResult):
        banned = set(cls_.model_fields) & {
            "content", "body", "body_text", "fulltext", "full_text",
            "raw_quote", "paper_text", "si_text",
        }
        assert not banned, f"{cls_.__name__} gained a body-text slot: {banned}"


def test_the_schema_round_trips_through_json():
    case = GoldenCase.build(
        "阿司匹林盲测", GoldenCaseType.BLIND_REPRODUCTION,
        blind_input=_blind_input(), reference_ids=["ref-expert-1"])
    trace = _trace()
    result = _evaluation()
    ref = _reference()

    for obj in (case, trace, result, ref):
        again = type(obj).model_validate_json(obj.model_dump_json())
        assert again == obj


def test_the_new_module_imports_nothing_below_core_and_no_network():
    """Explicit, so the isolation claim is tested here and not only in the
    architecture sweep: pydantic, stdlib and crea.core.* -- nothing else."""
    path = SRC / "core" / "models" / "golden_case.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)
    for name in imported:
        root = name.split(".")[0]
        assert root in {"enum", "pydantic", "crea", "__future__"}, name
        if root == "crea":
            assert name.split(".")[:2] == ["crea", "core"], name


def test_the_evaluation_config_pins_the_ten_reserved_dimensions():
    config = EvaluationConfig()

    assert len(config.dimensions) == 10
    assert config.require_score is False, "评分不是默认要求"
    assert config.evaluation_version == "1"


def test_case_constraints_record_the_manager_policy_not_the_agent_sandbox():
    constraints = CaseConstraints(model_policy="fixed_model")

    assert "model" in constraints.required_recordings
    assert "git_commit" in constraints.required_recordings
    assert not hasattr(constraints, "allowed_tools"), (
        "工具沙箱属于 BlindInput.resource_scope，不在评估政策里重复")

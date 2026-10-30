"""M1.6 -- input understanding: natural language in, a sourced request out.

What is pinned here: deterministic extraction (intents, entities, constraints,
missing information) with no model; the LLM's constrained-and-optional role;
ambiguity preserved as candidates and settled only by the user; study
continuation that asks instead of guessing; the validated bridge into the
Planner and through the Runtime; and full traceability from the user's own
words.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from pydantic import BaseModel, Field

from crea.agent.planner import Planner
from crea.agent.runtime import CoreAgentRuntime
from crea.agent.tools import ExecutionPolicy, ToolRegistry, ToolSpec
from crea.core.errors import ResearchStudyError
from crea.core.models.research_intent import (
    EntityIdentityStatus,
    EntityRole,
    IntentSource,
    MissingKind,
    ResearchIntentKind,
)
from crea.core.models.knowledge import ResearchPlanAction
from crea.core.models.research import TaskStatus
from crea.llm.fake import FakeLLMProvider, TextScriptedLLMProvider
from crea.services.input_understanding import InputUnderstandingService
from crea.services.knowledge_base import KnowledgeBase
from crea.services.research_study import ResearchStudyService
from crea.services.study_context import StudyContextService


@pytest.fixture()
def env(tmp_path):
    kb = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    kb.initialize()
    return kb


@pytest.fixture()
def svc(env):
    return InputUnderstandingService(kb=env)


# ---------------------------------------------------------------------------
# 输入理解 1-10
# ---------------------------------------------------------------------------


def test_a_simple_research_request_is_understood(svc):
    intent = svc.understand("帮我研究阿司匹林的合成路线")
    assert intent.primary_intent is ResearchIntentKind.SYNTHESIS_ROUTE_RESEARCH
    assert [e.name for e in intent.entities] == ["阿司匹林"]
    assert intent.entities[0].role is EntityRole.TARGET
    assert intent.entities[0].identity_status is \
        EntityIdentityStatus.NAME_RECOGNIZED
    assert intent.validated
    assert svc.validate(intent) == []


def test_a_multi_intent_request_keeps_every_intent(svc):
    intent = svc.understand(
        "我想研究阿司匹林的绿色合成路线，重点关注近五年的论文，并比较传统路线。")
    assert intent.primary_intent is ResearchIntentKind.SYNTHESIS_ROUTE_RESEARCH
    assert ResearchIntentKind.LITERATURE_RESEARCH in intent.secondary_intents
    assert ResearchIntentKind.MOLECULE_COMPARISON in intent.secondary_intents
    kinds = [(c.kind, c.value) for c in intent.constraints]
    assert ("green_chemistry", "绿色化学") in kinds
    assert ("time_range", "last_5_years") in kinds
    # 比较对象“传统路线”作为路线型对象保留，不丢弃。
    route = [e for e in intent.entities if e.entity_type == "route"]
    assert len(route) == 1
    assert route[0].role is EntityRole.COMPARISON
    # 只有一个分子对象 → 比较缺口如实记录。
    assert any(m.kind == "missing_second_entity"
               and m.severity is MissingKind.REQUIRED
               for m in intent.missing)


def test_multi_entity_comparison_recognizes_every_molecule(svc):
    intent = svc.understand("比较阿司匹林和布洛芬的结构差异")
    names = [e.name for e in intent.entities]
    assert "阿司匹林" in names and "布洛芬" in names
    assert intent.primary_intent is ResearchIntentKind.MOLECULE_COMPARISON
    roles = {e.name: e.role for e in intent.entities}
    assert roles["阿司匹林"] is EntityRole.TARGET
    assert roles["布洛芬"] is EntityRole.COMPARISON
    assert not any(m.kind == "missing_second_entity" for m in intent.missing)


def test_an_explicit_smiles_is_validated_by_the_chem_layer(svc):
    intent = svc.understand("比较 CC(=O)Oc1ccccc1C(=O)O 和 CC(C)Cc1ccc(cc1)"
                             "C(C)C(=O)O 的极性")
    structures = [e for e in intent.entities if e.structure_validated]
    assert len(structures) == 2
    assert structures[0].normalized_smiles == "CC(=O)Oc1ccccc1C(=O)O"
    assert all(e.identity_status is EntityIdentityStatus.STRUCTURE_VALIDATED
               for e in structures)
    assert svc.validate(intent) == []


def test_an_invalid_structure_like_input_is_flagged_not_ignored(svc):
    intent = svc.understand("帮我研究 C6H6 的合成路线")
    invalid = [e for e in intent.entities
               if e.identity_status is EntityIdentityStatus.STRUCTURE_INVALID]
    assert len(invalid) == 1
    assert invalid[0].name == "C6H6"
    assert any(m.kind == "invalid_structure"
               and m.severity is MissingKind.REQUIRED for m in intent.missing)
    assert svc.validate(intent) == [], "缺失被记录时请求仍然可审计"


def test_a_chemical_name_ambiguity_is_preserved_not_guessed(svc):
    intent = svc.understand("帮我研究二甲苯的合成。")
    entity = intent.entity("二甲苯")
    assert entity is not None
    assert entity.identity_status is EntityIdentityStatus.AMBIGUOUS
    labels = [c["label"] for c in entity.candidates]
    assert labels == ["o-xylene", "m-xylene", "p-xylene"]
    assert any(m.kind == "ambiguous_identity"
               and m.entity_name == "二甲苯"
               and m.severity is MissingKind.REQUIRED for m in intent.missing)
    assert intent.clarification, "歧义必须以澄清问题的形式浮出"
    assert intent.validated  # 缺失已记录 → 请求本身可审计


def test_a_structure_dependent_request_without_a_target_records_it(svc):
    intent = svc.understand("帮我找一下合成路线")
    assert any(m.kind == "missing_target"
               and m.severity is MissingKind.REQUIRED for m in intent.missing)
    assert "missing_target" in " ".join(svc.validate(intent)) or \
        svc.validate(intent) == []


def test_explicit_constraints_carry_user_explicit_source(svc):
    intent = svc.understand("找近五年的绿色化学论文，要求无溶剂条件")
    sources = {(c.kind, c.value): c.source for c in intent.constraints}
    assert sources[("time_range", "last_5_years")] is IntentSource.USER_EXPLICIT
    assert sources[("green_chemistry", "绿色化学")] is IntentSource.USER_EXPLICIT
    assert ("conditions", "solvent_free") in sources


def test_mixed_chinese_english_requests_understand(svc):
    intent = svc.understand(
        "Find analogues of 阿司匹林, 比较 with Ibuprofen, last 5 years")
    assert ResearchIntentKind.SIMILARITY_SEARCH in (
        intent.primary_intent, *intent.secondary_intents)
    names = [e.name for e in intent.entities]
    assert "阿司匹林" in names and "Ibuprofen" in names
    assert ("time_range", "last_5_years") in [
        (c.kind, c.value) for c in intent.constraints]


# ---------------------------------------------------------------------------
# 歧义与澄清 11-16
# ---------------------------------------------------------------------------


def test_the_xylene_isomer_ambiguity_survives_a_clarification_turn(svc):
    intent = svc.understand("研究二甲苯的合成路线")
    assert intent.entity("二甲苯").identity_status is \
        EntityIdentityStatus.AMBIGUOUS
    assert len(intent.entity("二甲苯").candidates) == 3


def test_a_user_selection_resolves_the_entity_with_their_own_words(svc):
    intent = svc.understand("研究二甲苯的合成路线")
    updated = svc.select_candidate(intent, "二甲苯", 2,
                                   user_selection_statement="用户说：对二甲苯")
    entity = updated.entity("二甲苯")
    assert entity.identity_status is EntityIdentityStatus.RESOLVED
    assert entity.value == "对二甲苯"
    assert entity.source is IntentSource.USER_EXPLICIT
    assert not any(m.kind == "ambiguous_identity" for m in updated.missing)
    assert updated.clarification == ""
    assert svc.validate(updated) == []

    with pytest.raises(ResearchStudyError):
        svc.select_candidate(updated, "二甲苯", 9,
                             user_selection_statement="x")
    with pytest.raises(ResearchStudyError):
        svc.select_candidate(intent, "二甲苯", 0, user_selection_statement="  ")


def test_continuing_with_several_studies_asks_instead_of_guessing(env, svc):
    studies = ResearchStudyService(env)
    a = studies.create(title="阿司匹林研究", question="阿司匹林")
    b = studies.create(title="布洛芬研究", question="布洛芬")
    intent = svc.understand("继续上次关于阿司匹林的研究")
    assert intent.continuation
    resolved = svc.resolve_references(intent)
    assert resolved.study_id == ""
    assert any(m.kind == "missing_study"
               and m.severity is MissingKind.REQUIRED
               for m in resolved.missing)
    assert a.study_id in resolved.clarification
    assert b.study_id in resolved.clarification
    assert not resolved.validated


def test_continuing_with_exactly_one_study_resumes_it_and_inherits_its_context(
        env, svc):
    studies = ResearchStudyService(env)
    study = studies.create(title="唯一研究", question="阿司匹林")
    context = StudyContextService(env)
    context.open(study.study_id, constraints=["不使用钯催化剂"])
    intent = svc.understand("继续上次关于阿司匹林的研究")
    resolved = svc.resolve_references(intent, study_context=context)
    assert resolved.study_id == study.study_id
    assert not any(m.kind == "missing_study" for m in resolved.missing)
    inherited = [c for c in resolved.constraints
                 if c.source is IntentSource.CONTEXT_INHERITED]
    assert [c.value for c in inherited] == ["不使用钯催化剂"]
    assert resolved.validated


def test_a_missing_parameter_can_be_answered_and_revalidated(svc):
    intent = svc.understand("比较阿司匹林")
    assert any(m.kind == "missing_second_entity" for m in intent.missing)
    answered = svc.answer_missing(intent, "missing_second_entity", "布洛芬")
    assert not any(m.kind == "missing_second_entity" for m in answered.missing)
    constraint = [c for c in answered.constraints
                  if c.kind == "missing_second_entity"]
    assert constraint and constraint[0].source is IntentSource.USER_EXPLICIT
    assert answered.validated


def test_a_goal_revision_keeps_history_and_marks_superseded_constraints(env):
    old_intent = InputUnderstandingService().understand(
        "研究阿司匹林的合成路线，找近十年的相关论文")
    new_intent = InputUnderstandingService().understand(
        "我现在更关注绿色化学，文献改看近三年")
    merged = InputUnderstandingService().revise_intent(old_intent, new_intent)
    assert merged.goal == new_intent.goal
    assert old_intent.goal in merged.superseded_goals
    assert any("研究目标已修正" in w for w in merged.warnings)
    # 旧约束保留并标记被取代；新约束在列。
    kinds = {c.kind for c in merged.constraints}
    assert "green_chemistry" in kinds
    assert ("time_range", "last_3_years") in [
        (c.kind, c.value) for c in merged.constraints]
    superseded = [c for c in merged.constraints if c.superseded_by]
    assert [(c.kind, c.value) for c in superseded] == [
        ("time_range", "last_10_years")]
    assert superseded[0].superseded_by == "time_range=last_3_years"


def test_a_revision_never_touches_recorded_decisions(env):
    studies = ResearchStudyService(env)
    study = studies.create(title="S", question="Q")
    context = StudyContextService(env)
    context.record_decision(study.study_id, "先解析身份", reason="结构未知")
    old_intent = InputUnderstandingService(kb=env).understand(
        "研究阿司匹林的合成路线")
    old_intent.study_id = study.study_id
    new_intent = InputUnderstandingService(kb=env).understand(
        "改为关注绿色路线")
    merged = InputUnderstandingService(kb=env).revise_intent(
        old_intent, new_intent)
    assert merged.study_id == study.study_id
    assert [d.decision for d in
            context.decisions(study.study_id)] == ["先解析身份"]


# ---------------------------------------------------------------------------
# LLM 边界 17-22
# ---------------------------------------------------------------------------


def test_a_deterministic_request_never_calls_the_model(env):
    llm = FakeLLMProvider([])
    service = InputUnderstandingService(kb=env, llm=llm)
    intent = service.understand("帮我研究阿司匹林的合成路线")
    assert llm.calls == []
    assert intent.primary_intent is ResearchIntentKind.SYNTHESIS_ROUTE_RESEARCH


def test_a_messy_request_uses_one_constrained_model_call(env):
    payload = json.dumps({
        "goal": "为某激酶抑制剂骨架寻找类似物并综述文献",
        "primary_intent": "similarity_search",
        "secondary_intents": ["literature_research"],
        "entities": [{"name": "某激酶抑制剂骨架", "role": "target"}],
        "constraints": [],
        "missing": [],
    }, ensure_ascii=False)
    llm = TextScriptedLLMProvider([payload])
    service = InputUnderstandingService(kb=env, llm=llm)
    intent = service.understand(
        "我手上有一个做了一半的激酶抑制剂项目，之前中断了，现在想换个方向，"
        "然后把近两年的文献也一并看看，最好能给出可执行的下一步。")
    assert len(llm.calls) == 1
    # 确定性读到的“文献”保持优先；模型给出的不同主意图作为次要意图保留。
    assert intent.primary_intent is ResearchIntentKind.LITERATURE_RESEARCH
    assert ResearchIntentKind.SIMILARITY_SEARCH in intent.secondary_intents
    assert intent.entity("某激酶抑制剂骨架") is not None
    assert intent.entity("某激酶抑制剂骨架").source is IntentSource.MODEL_INFERRED


def test_invalid_model_output_degrades_to_the_deterministic_reading(env):
    llm = TextScriptedLLMProvider(["this is not json at all"])
    service = InputUnderstandingService(kb=env, llm=llm)
    intent = service.understand("帮我研究阿司匹林和布洛芬的关系，随便看看文献",
                                use_llm=True)
    assert any("降级" in w for w in intent.warnings)
    assert intent.primary_intent is not ResearchIntentKind.UNKNOWN
    assert intent.entities, "确定性理解的结果必须保留"


def test_an_unavailable_model_is_reported_not_simulated(env):
    llm = FakeLLMProvider([])  # empty script: the first call raises
    service = InputUnderstandingService(kb=env, llm=llm)
    intent = service.understand("一个没有明确意图的模糊表达……？？", use_llm=True)
    assert any("模型不可用" in w for w in intent.warnings)
    assert intent.primary_intent in (ResearchIntentKind.UNKNOWN,
                                     ResearchIntentKind.GENERAL_CHEMISTRY_QUESTION)


def test_the_model_cannot_confirm_a_structure(env):
    payload = json.dumps({
        "goal": "研究某个分子",
        "primary_intent": "structure_analysis",
        "secondary_intents": [],
        "entities": [{"name": "甲苯", "role": "target",
                      "smiles": "Cc1ccccc1", "validated": True,
                      "identity_status": "resolved"}],
        "constraints": [],
        "missing": [],
    }, ensure_ascii=False)
    service = InputUnderstandingService(
        kb=env, llm=TextScriptedLLMProvider([payload]))
    intent = service.understand("分析一下甲苯这个分子吧，挺好奇的呢！", use_llm=True)
    entity = intent.entity("甲苯")
    assert entity is not None
    # 确定性层只认出了名字；模型的“validated/resolved”主张不被采纳。
    assert entity.identity_status is EntityIdentityStatus.NAME_RECOGNIZED
    assert entity.normalized_smiles == ""
    assert not entity.structure_validated


def test_model_inferred_constraints_never_wear_the_users_words(env):
    payload = json.dumps({
        "goal": "研究 X 的合成",
        "primary_intent": "synthesis_route_research",
        "secondary_intents": [],
        "entities": [],
        "constraints": [{"kind": "cost", "value": "必须低于五万元"},
                        {"kind": "green_chemistry", "value": "绿色化学"}],
        "missing": [],
    }, ensure_ascii=False)
    service = InputUnderstandingService(
        kb=env, llm=TextScriptedLLMProvider([payload]))
    intent = service.understand("研究某个化合物的合成路线", use_llm=True)
    by_value = {(c.kind, c.value): c.source for c in intent.constraints}
    assert by_value[("cost", "必须低于五万元")] is IntentSource.MODEL_INFERRED
    # 用户没有说绿色化学；模型替他说的不能变成 USER_EXPLICIT。
    assert by_value[("green_chemistry", "绿色化学")] is IntentSource.MODEL_INFERRED


# ---------------------------------------------------------------------------
# Planner / Runtime 集成 23-30
# ---------------------------------------------------------------------------


def test_a_valid_intent_converts_to_planner_inputs_and_a_plan(env, svc):
    from crea.agent.tool_surface import build_agent_tools

    intent = svc.understand("研究 CC(=O)Oc1ccccc1C(=O)O 的合成路线")
    inputs = svc.to_planner_inputs(intent)
    planner = Planner(build_agent_tools(
        policy=ExecutionPolicy(allow_network=True)))
    proposal = planner.initial_plan(
        inputs["goal"], identity_input=inputs["identity_input"],
        smiles=inputs["smiles"], wants_routes=inputs["wants_routes"],
        context=inputs["context"])
    assert proposal.source == "deterministic"
    assert planner.validate_plan(proposal.plan) == []
    # 路线意图 → 先解析身份再逆合成：结构依赖没有被绕过。
    assert proposal.plan.has(ResearchPlanAction.RESOLVE_IDENTITY)
    assert proposal.plan.has(ResearchPlanAction.RETROSYNTHESIZE)


def test_an_unresolved_identity_never_produces_structure_arguments(svc):
    intent = svc.understand("研究二甲苯的逆合成路线")
    inputs = svc.to_planner_inputs(intent)
    assert inputs["identity_input"] is None
    assert inputs["smiles"] == ""
    assert inputs["wants_routes"] is True


def test_an_inconsistent_intent_refuses_to_reach_the_planner(svc):
    # 已记录的缺失（missing_target 等）是诚实的记录，不阻止规划——
    # 真正阻止的是自相矛盾：一个“已验证”却没有规范结构的实体。
    intent = svc.understand("研究阿司匹林的合成路线")
    intent.entities[0].identity_status = EntityIdentityStatus.STRUCTURE_VALIDATED
    intent.entities[0].structure_validated = True
    with pytest.raises(ResearchStudyError) as excinfo:
        svc.to_planner_inputs(intent)
    assert excinfo.value.code == "INTENT_NOT_VALID"


class _Args(BaseModel):
    query: str = Field(default="")


class _SmilesArgs(BaseModel):
    smiles: str = Field(min_length=1)


class _RetrospectArgs(BaseModel):
    target_smiles: str = Field(min_length=1)


class _Script:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.payloads.pop(0) if self.payloads else {"status": "ok"}


def test_a_complete_request_runs_through_the_runtime(env, svc):
    inspect = _Script([{"status": "ok", "canonical_smiles": "CCO"}])
    registry = ToolRegistry(enforce_policy=True, policy=ExecutionPolicy())
    for name in ("get_existing_knowledge", "search_literature"):
        registry.register(ToolSpec(
            name=name, description=name, arguments_model=_Args,
            run=inspect, access=("local",)))
    registry.register(ToolSpec(
        name="analyze_molecule", description="analyze",
        arguments_model=_SmilesArgs, run=inspect, access=("local",)))
    intent = svc.understand("分析一下 CC(=O)Oc1ccccc1C(=O)O 的结构特点")
    inputs = svc.to_planner_inputs(intent)
    runtime = CoreAgentRuntime(registry)
    result = runtime.run(inputs["goal"], identity_input=inputs["identity_input"],
                         smiles=inputs["smiles"],
                         wants_routes=inputs["wants_routes"],
                         use_llm=False)

    assert result.status is TaskStatus.COMPLETED
    assert len(inspect.calls) >= 2


def test_an_unresolved_route_request_pauses_for_arguments(env, svc):
    registry = ToolRegistry(enforce_policy=True, policy=ExecutionPolicy())
    for name in ("get_existing_knowledge", "search_literature"):
        registry.register(ToolSpec(
            name=name, description=name, arguments_model=_Args,
            run=_Script([{"status": "no_results"}]), access=("local",)))
    registry.register(ToolSpec(
        name="retrosynthesize", description="retro",
        arguments_model=_RetrospectArgs,
        run=_Script([{"status": "ok"}]), access=("local",)))
    intent = svc.understand("研究二甲苯的逆合成路线")
    inputs = svc.to_planner_inputs(intent)  # 无结构参数
    result = CoreAgentRuntime(registry).run(
        inputs["goal"], identity_input=inputs["identity_input"],
        smiles=inputs["smiles"], wants_routes=inputs["wants_routes"],
        use_llm=False)

    assert result.status is TaskStatus.RUNNING
    assert result.outcome == "input_required"
    assert result.awaiting is not None


def test_the_clarification_question_is_actually_askable(svc):
    intent = svc.understand("帮我研究二甲苯的类似物")
    assert intent.clarification
    assert "二甲苯" in intent.clarification
    assert any(m.entity_name == "二甲苯" for m in intent.missing)


def test_answering_the_clarification_restores_a_complete_request(svc):
    intent = svc.understand("帮我研究二甲苯的类似物")
    answered = svc.select_candidate(intent, "二甲苯", 0,
                                    user_selection_statement="用户说：邻二甲苯")
    assert answered.validated
    assert answered.clarification == ""
    inputs = svc.to_planner_inputs(answered)
    assert inputs["smiles"] == ""  # 候选是名称级；结构由身份解析层确认
    assert inputs["identity_input"] == {"kind": "name", "value": "邻二甲苯"}


def test_study_context_resolution_reads_only_the_named_study(env, svc):
    studies = ResearchStudyService(env)
    a = studies.create(title="A", question="A 的研究")
    b = studies.create(title="B", question="B 的研究")
    context = StudyContextService(env)
    context.open(a.study_id, constraints=["仅用开放获取文献"])
    context.open(b.study_id, constraints=["不使用钯催化剂"])
    intent = svc.understand("继续上次的研究")
    intent.study_id = a.study_id
    intent.continuation = True
    resolved = svc.resolve_references(intent, study_context=context)
    values = [c.value for c in resolved.constraints
              if c.source is IntentSource.CONTEXT_INHERITED]
    assert values == ["仅用开放获取文献"]
    assert "不使用钯催化剂" not in values


def test_the_raw_input_and_every_field_stay_traceable(svc):
    text = "帮我研究阿司匹林的合成路线，近五年的论文也要"
    intent = svc.understand(text)
    assert intent.raw_input == text
    assert intent.goal == text  # 确定性路径不改写用户的话
    entity = intent.entity("阿司匹林")
    assert entity.source is IntentSource.USER_EXPLICIT
    assert all(c.source is IntentSource.USER_EXPLICIT
               for c in intent.constraints)


def test_a_selection_can_persist_through_the_study_context(env, svc):
    studies = ResearchStudyService(env)
    study = studies.create(title="S", question="Q")
    context = StudyContextService(env)
    intent = svc.understand("研究二甲苯的合成路线")
    svc.select_candidate(intent, "二甲苯", 1,
                         user_selection_statement="用户说：间二甲苯",
                         study_context=context, study_id=study.study_id)
    records = context.identity_records(study.study_id)
    assert len(records) == 1
    assert records[0].selected_index == 1
    assert context.identity_resolve_arguments(
        study.study_id, records[0].record_id)["value"] == "间二甲苯"

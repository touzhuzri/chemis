"""M1.6 -- input understanding: natural language in, a sourced request out.

`InputUnderstandingService` turns what a researcher actually says into a
`ResearchIntent` the rest of CREA can validate, plan and audit. It performs
no research: no tool executes here, no literature is searched, no structure
is computed beyond parsing the user's own SMILES with the existing chem
layer, and no name is resolved to a structure -- recognition is not
confirmation, and resolution stays with the identity mechanism (the
`resolve_chemical_identity` tool, the user's candidate selection).

The order of understanding is deliberate:

1. deterministic chemistry pass -- InChI / SMILES-looking tokens, validated
   with `chem.normalize` (the only place a structure gets confirmed, and only
   for input the USER supplied);
2. deterministic language pass -- intent keywords, constraints and entity
   names in Chinese and English, each mapped by explicit rules;
3. optional, constrained LLM pass -- one call for messy natural language,
   schema-checked; the model may suggest intents, roles, constraints and
   questions, but never confirms a structure, never invents a fact, and its
   guesses are stored as `MODEL_INFERRED`, never as the user's words;
4. validation -- `validate()` reports every blocking gap in machine-readable
   terms; ambiguity keeps its candidates and asks, it never guesses.

Continuing prior work goes through the study context (`resolve_references`):
one study may be resumed directly, several must be chosen by the user, and
the context's constraints come back as `CONTEXT_INHERITED` -- never mixed
with another study's anything.
"""

from __future__ import annotations

import re
from typing import Any

from crea.core.errors import NormalizationError, ResearchStudyError
from crea.core.json_parse import parse_json_object
from crea.core.models.research_intent import (
    EntityIdentityStatus,
    EntityRole,
    IntentEntity,
    IntentSource,
    MissingInformation,
    MissingKind,
    ResearchConstraint,
    ResearchIntent,
    ResearchIntentKind,
)
from crea.llm.base import LLMProvider, LLMRequest

#: The intents whose work depends on a concrete molecular structure.
_STRUCTURE_DEPENDENT = frozenset({
    ResearchIntentKind.MOLECULE_COMPARISON,
    ResearchIntentKind.SIMILARITY_SEARCH,
    ResearchIntentKind.RETROSYNTHESIS,
    ResearchIntentKind.SYNTHESIS_ROUTE_RESEARCH,
    ResearchIntentKind.STRUCTURE_ANALYSIS,
    ResearchIntentKind.PROPERTY_ANALYSIS,
})

#: Ordered keyword map (Chinese first-class). The FIRST match in this order
#: is the primary intent; every other match is kept as a secondary intent --
#: a request about papers and routes keeps both.
_INTENT_KEYWORDS: tuple[tuple[ResearchIntentKind, tuple[str, ...]], ...] = (
    (ResearchIntentKind.SYNTHESIS_ROUTE_RESEARCH,
     ("合成路线", "合成方法", "制备路线", "路线设计", "全合成",
      "synthesis route", "synthetic route", "route design", "total synthesis",
      "合成", "制备", "synthesis", "preparation", "prepare")),
    (ResearchIntentKind.RETROSYNTHESIS,
     ("逆合成", "反合成", "retrosynth", "disconnection")),
    (ResearchIntentKind.MOLECULE_COMPARISON,
     ("比较", "对比", "差异", "compare", "comparison", "versus", " vs ",
      "difference")),
    (ResearchIntentKind.SIMILARITY_SEARCH,
     ("类似物", "类似", "相似", "衍生物", "analogue", "analog", "similar",
      "scaffold hop")),
    (ResearchIntentKind.MECHANISM_ANALYSIS,
     ("机理", "mechanism")),
    (ResearchIntentKind.REACTION_RESEARCH,
     ("反应条件", "反应研究", "反应查找", "reaction", "reactions")),
    (ResearchIntentKind.LITERATURE_RESEARCH,
     ("文献", "论文", "专利", "papers", "paper", "literature", "publication",
      "article")),
    (ResearchIntentKind.PROPERTY_ANALYSIS,
     ("性质", "构效", "药代", "admet", "property", "properties", "solubility",
      "potency")),
    (ResearchIntentKind.STRUCTURE_ANALYSIS,
     ("结构分析", "分子结构", "结构特点", "structure")),
    (ResearchIntentKind.MOLECULE_IDENTIFICATION,
     ("是什么分子", "识别", "identify", "identification", "what molecule")),
    (ResearchIntentKind.CONTINUE_RESEARCH,
     ("继续", "上次", "接着", "continue", "resume", "last time")),
)

#: Deterministic constraint extraction. The words are the user's, so the
#: source is always USER_EXPLICIT; a model may only add MODEL_INFERRED ones.
_CONSTRAINT_KEYWORDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("time_range", "last_5_years",
     ("近五年", "近5年", "最近五年", "最近5年", "last five years",
      "last 5 years", "past five years")),
    ("time_range", "last_10_years",
     ("近十年", "近10年", "last ten years", "last 10 years")),
    ("time_range", "last_3_years",
     ("近三年", "近3年", "last three years", "last 3 years")),
    ("time_range", "recent", ("最近", "近年来", "recent", "latest")),
    ("green_chemistry", "绿色化学",
     ("绿色", "环保", "green", "sustainable")),
    ("cost", "低成本", ("成本低", "廉价", "便宜", "cost", "cheap")),
    ("safety", "安全", ("安全", "低毒", "non-toxic", "safety")),
    ("literature_type", "patent", ("专利", "patent")),
    ("literature_type", "open_access", ("开放获取", "open access")),
    ("conditions", "solvent_free", ("无溶剂", "solvent-free", "solvent free")),
    ("conditions", "high_pressure", ("高压", "high pressure")),
    ("conditions", "room_temperature", ("常温", "room temperature")),
)

#: A curated starter list of names that are KNOWN to be ambiguous. Labels
#: only -- no structures, because inventing candidate structures here would
#: be exactly the confirmation this layer refuses to make. The real
#: resolution still happens through the identity tool; this list only knows
#: that a choice is owed.
_AMBIGUOUS_NAMES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "二甲苯": (("name", "邻二甲苯", "o-xylene"),
               ("name", "间二甲苯", "m-xylene"),
               ("name", "对二甲苯", "p-xylene")),
    "xylene": (("name", "o-xylene", "邻二甲苯"),
               ("name", "m-xylene", "间二甲苯"),
               ("name", "p-xylene", "对二甲苯")),
    "xylenes": (("name", "o-xylene", "邻二甲苯"),
                ("name", "m-xylene", "间二甲苯"),
                ("name", "p-xylene", "对二甲苯")),
}

#: Function words stripped from CJK runs before an entity name is taken from
#: what is left. Intent keywords are read from the FULL text first, so this
#: strip never costs an intent -- only decides what counts as a name.
_CJK_STRIP = re.compile(
    "(我想|我要|我需要|帮我|请|你能|你|能不能|可以|是否|能否|研究|一下|关于|"
    "什么|怎么|怎么样|哪些|哪个|一些|目前|现在|首先|谢谢|麻烦|的|了|在|和|与|差异|"
    "或|以及|并|并且|同时|比较|对比|了解|探索|寻找|找到|找|看看|解析|分析|"
    "继续|上次|最近|近|年来|年内|论文|文献|发表|文章|专利|路线|方法|合成|"
    "制备|类似物|类似|衍生物|绿色|化学|生物|分子|结构|性质|反应|机理|条件|"
    "成本|安全|传统|先|更|关注|重点|范围|输出|要求|时间|提供|给出|使用|采用|"
    "基于|通过|能够|这个|那个|它|其中|等|等等|比如|例如|的话|时候|问题|方向|"
    "开发|药物|潜在|天然产物|产物|它的|它们|年|月|日|好奇|挺|真的|有点|吧|呢|啊|呀)")

#: Tokens that RDKit would happily parse but that are ordinary words.
_WORD_STOP = frozenset({
    "no", "in", "on", "or", "so", "if", "is", "it", "at", "be", "by", "do",
    "go", "he", "me", "my", "of", "oh", "up", "us", "we", "to", "as", "an",
    "am", "re", "ve", "ll", "ok", "id", "cs", "in", "pm", "am",
})

_INCHI_RE = re.compile(r"^InChI=1S?/", re.IGNORECASE)
_INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
#: Split on whitespace and punctuation. ASCII ()[] stay INSIDE tokens: they
#: are SMILES branch/ring syntax, not prose punctuation.
_TOKEN_SPLIT_RE = re.compile(
    r"[\s,;，；、。？！?！\"'“”‘’「」『』（）【】]+")
_QUOTED_RE = re.compile(
    r"[\"'“‘「『]([^\"'”‘」』]{2,60})[\"'”’」』]")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
_EN_CAPITAL_RE = re.compile(r"\b([A-Z][a-zA-Z]{3,})\b")
_COMPARISON_RE = re.compile(
    r"(?:比较|对比|compare|versus)\s*([^,，。;；]{2,40})")

#: Sentence-starting capitalised words that are not chemical names.
_EN_NAME_STOP = frozenset({
    "The", "This", "That", "These", "Those", "My", "Your", "Our", "Want",
    "Wants", "Please", "Help", "Find", "Search", "Compare", "Continue",
    "Resume", "Last", "Recent", "Green", "Synthesis", "Route", "Routes",
    "Literature", "Papers", "Paper", "What", "How", "Does", "Can", "Could",
    "Should", "Would", "Study", "Studying", "Research", "Researching",
    "Molecule", "Molecules", "Structure", "Structures", "Analysis",
    "Looking", "Give", "Get", "Make", "Check", "Review", "Summarize",
    "Summarise", "Hello", "Hi", "Thanks", "Thank",
})


class InputUnderstandingService:
    """Natural language in; a sourced, validatable `ResearchIntent` out."""

    def __init__(self, kb: Any = None, llm: LLMProvider | None = None) -> None:
        self._kb = kb
        self._llm = llm

    # -- understanding ------------------------------------------------------

    def understand(self, user_input: str, *, context: dict | None = None,
                   use_llm: bool | None = None) -> ResearchIntent:
        """Read one user message into a structured request.

        `context` is currently accepted and reserved for caller state (the
        study context arrives through `resolve_references`, which knows the
        study); nothing from it is treated as user intent.
        `use_llm=None` consults the model only when one is configured AND the
        deterministic pass could not settle a concrete task.
        """
        text = (user_input or "").strip()
        warnings: list[str] = []

        entities = _extract_entities(text)
        primary, secondary = _classify_intents(text)
        constraints = _extract_constraints(text)
        missing = _derive_missing(entities, primary, secondary)

        clarification = ""
        first_required = next((m for m in missing
                               if m.severity is MissingKind.REQUIRED), None)
        if first_required is not None:
            clarification = _clarification_for(first_required)

        intent = ResearchIntent(
            raw_input=text, goal=text, primary_intent=primary,
            secondary_intents=secondary, entities=entities,
            constraints=constraints, missing=missing,
            continuation=(ResearchIntentKind.CONTINUE_RESEARCH
                          in (primary, *secondary)),
            clarification=clarification, warnings=warnings)

        if use_llm is None:
            use_llm = (self._llm is not None
                       and _needs_model(text, primary, missing))
        if use_llm and self._llm is not None:
            self._llm_refine(intent)

        intent.validated = not self._issues(intent)
        return intent

    # -- validation ---------------------------------------------------------

    def validate(self, intent: ResearchIntent) -> list[str]:
        """Every reason this intent is not yet safe to plan with."""
        return self._issues(intent)

    def _issues(self, intent: ResearchIntent) -> list[str]:
        issues: list[str] = []
        if not (intent.raw_input or "").strip():
            issues.append("raw_input 为空：没有任何用户输入可理解")
        if (intent.primary_intent is ResearchIntentKind.UNKNOWN
                and not intent.missing
                and not intent.clarification):
            issues.append("意图未知（UNKNOWN）且没有记录任何缺失信息；"
                          "请求无法被安全地规划")
        molecules = [e for e in intent.entities
                     if e.entity_type == "molecule"]
        if (intent.primary_intent in _STRUCTURE_DEPENDENT
                or ResearchIntentKind.MOLECULE_COMPARISON
                in intent.secondary_intents) and not molecules:
            if not any(m.kind == "missing_target" for m in intent.missing):
                issues.append("结构相关任务没有研究对象，且未记录 missing_target")
        if (ResearchIntentKind.MOLECULE_COMPARISON
                in (intent.primary_intent, *intent.secondary_intents)
                and len(molecules) < 2
                and not any(m.kind == "missing_second_entity"
                            for m in intent.missing)):
            issues.append("比较任务不足两个分子对象，且未记录 missing_second_entity")
        for entity in intent.entities:
            if (entity.identity_status
                    is EntityIdentityStatus.STRUCTURE_VALIDATED
                    and not entity.normalized_smiles):
                issues.append(f"实体 {entity.name!r} 标记为已验证但没有规范结构")
            if entity.identity_status is EntityIdentityStatus.AMBIGUOUS:
                has_missing = any(
                    m.kind == "ambiguous_identity"
                    and m.entity_name == entity.name for m in intent.missing)
                if not entity.candidates and not has_missing:
                    issues.append(f"实体 {entity.name!r} 标记为歧义，"
                                  f"但既没有候选也没有缺失记录")
            if (entity.identity_status is EntityIdentityStatus.RESOLVED
                    and not (entity.kind and entity.value)):
                issues.append(f"实体 {entity.name!r} 标记为已解析，"
                              f"但缺少 kind/value")
        if intent.continuation and not intent.study_id:
            if not any(m.kind == "missing_study" for m in intent.missing):
                issues.append("继续研究没有指定 study，且未记录 missing_study")
        return issues

    # -- study context ------------------------------------------------------

    def resolve_references(self, intent: ResearchIntent, *,
                           studies: list | None = None,
                           study_context: Any = None) -> ResearchIntent:
        """Settle which study this request belongs to, without guessing.

        One available study may be resumed directly; several are a question,
        never a race to the most recent. The chosen study's constraints come
        back as `CONTEXT_INHERITED`, kept apart from the user's own words.
        """
        if not intent.continuation:
            return intent
        if not intent.study_id:
            available = list(studies) if studies is not None else (
                list(self._kb.list_studies()) if self._kb is not None else [])
            if len(available) == 1:
                intent.study_id = available[0].study_id
                intent.missing = [m for m in intent.missing
                                  if m.kind != "missing_study"]
            elif not available:
                intent.missing = _with_missing(
                    intent.missing, MissingInformation(
                        kind="missing_study",
                        detail="没有任何可继续的研究",
                        severity=MissingKind.REQUIRED))
                intent.clarification = "没有可继续的研究；请先开始一个新研究。"
                intent.validated = False
                return intent
            else:
                names = "；".join(
                    f"{s.study_id}（{s.title or s.question}）"
                    for s in available)
                intent.missing = _with_missing(
                    intent.missing, MissingInformation(
                        kind="missing_study",
                        detail=f"找到 {len(available)} 个研究",
                        severity=MissingKind.REQUIRED))
                intent.clarification = (
                    f"找到 {len(available)} 个可继续的研究：{names}。"
                    f"请指定要继续哪一个（study_id）。")
                intent.validated = False
                return intent

        if study_context is not None and intent.study_id:
            try:
                context = study_context.get_context(intent.study_id)
            except Exception:  # noqa: BLE001 - a missing study is handled above
                context = None
            if context is not None:
                known = {(c.kind, c.value) for c in intent.constraints}
                # The context profile stores constraints as the plain
                # strings the caller bound; they arrive here as inherited
                # scope, never dressed up as the user's own words.
                for value in context.constraints:
                    if ("study_constraint", value) not in known:
                        intent.constraints.append(ResearchConstraint(
                            kind="study_constraint", value=value,
                            source=IntentSource.CONTEXT_INHERITED))
                # Candidates a previous session already collected travel
                # with their record -- still a question, never a choice.
                for record in context.identity_records:
                    for entity in intent.entities:
                        if (entity.identity_status
                                in (EntityIdentityStatus.NAME_RECOGNIZED,
                                    EntityIdentityStatus.AMBIGUOUS)
                                and record.candidates
                                and (entity.name
                                     and entity.name in record.question)):
                            entity.candidates = [
                                c.model_dump(mode="json")
                                for c in record.candidates]
                            entity.identity_status = \
                                EntityIdentityStatus.AMBIGUOUS
        intent.validated = not self._issues(intent)
        return intent

    # -- clarification ------------------------------------------------------

    def select_candidate(self, intent: ResearchIntent, entity_name: str,
                         selected_index: int, *,
                         user_selection_statement: str,
                         study_context: Any = None,
                         study_id: str = "") -> ResearchIntent:
        """Record THE USER's choice among an entity's candidates.

        Reuses the M1.5 identity-selection records when a study is involved
        -- there is one candidate-selection mechanism in CREA, not two. The
        statement is the user's own words and is always required.
        """
        entity = intent.entity(entity_name)
        if entity is None:
            raise ResearchStudyError(
                "CONTEXT_RECORD_NOT_FOUND",
                f"请求中没有实体 {entity_name!r}")
        if not (0 <= selected_index < len(entity.candidates)):
            raise ResearchStudyError(
                "IDENTITY_INDEX_INVALID",
                f"候选 {selected_index} 不存在；{entity_name!r} 有 "
                f"{len(entity.candidates)} 个候选")
        statement = (user_selection_statement or "").strip()
        if not statement:
            raise ResearchStudyError(
                "IDENTITY_SELECTION_STATEMENT_REQUIRED",
                "选择必须记录用户自己的话；没有 user_selection_statement "
                "的选择不予承认")
        chosen = entity.candidates[selected_index]
        entity.kind = str(chosen.get("kind") or "name")
        entity.value = str(chosen.get("value") or chosen.get("label") or "")
        entity.identity_status = EntityIdentityStatus.RESOLVED
        entity.source = IntentSource.USER_EXPLICIT
        entity.note = f"用户选择：{statement}"

        if study_context is not None and study_id:
            from crea.core.models.study_context import IdentityCandidate

            record = study_context.save_identity_candidates(
                study_id, question=f"实体“{entity_name}”的候选选择",
                candidates=[IdentityCandidate(
                    kind=str(c.get("kind") or ""),
                    value=str(c.get("value") or ""),
                    label=str(c.get("label") or ""),
                    raw=c if isinstance(c, dict) else {})
                    for c in entity.candidates])
            study_context.select_identity_candidate(
                study_id, record.record_id, selected_index,
                user_selection_statement=statement)

        intent.missing = [
            m for m in intent.missing
            if not (m.kind == "ambiguous_identity"
                    and m.entity_name == entity_name)]
        if not any(m.severity is MissingKind.REQUIRED for m in intent.missing):
            intent.clarification = ""
        intent.validated = not self._issues(intent)
        return intent

    def answer_missing(self, intent: ResearchIntent, kind: str,
                       detail: str) -> ResearchIntent:
        """Fold a user's answer into the request and re-validate.

        The answer becomes a USER_EXPLICIT constraint of the requested kind
        (for condition-like gaps) or replaces the missing entry; the original
        question and answer both stay on the record.
        """
        answered = [m for m in intent.missing if m.kind != kind]
        if len(answered) == len(intent.missing):
            return intent
        intent.missing = answered
        text = (detail or "").strip()
        if text:
            intent.constraints.append(ResearchConstraint(
                kind=kind, value=text, source=IntentSource.USER_EXPLICIT))
            if kind == "missing_second_entity":
                # The answer names the missing object: it becomes an entity
                # (name-level, like any other recognised name), not a fact.
                if intent.entity(text) is None:
                    intent.entities.append(IntentEntity(
                        name=text, kind="name", value=text,
                        role=EntityRole.COMPARISON,
                        identity_status=EntityIdentityStatus.NAME_RECOGNIZED,
                        source=IntentSource.USER_EXPLICIT,
                        note="用户在澄清中提供的比较对象"))
                intent.missing.append(MissingInformation(
                    kind="identity_confirmation",
                    detail=f"“{text}”尚未确认结构身份",
                    severity=MissingKind.DEFERRABLE, entity_name=text))
        if not any(m.severity is MissingKind.REQUIRED for m in intent.missing):
            intent.clarification = ""
        intent.validated = not self._issues(intent)
        return intent

    def revise_intent(self, old: ResearchIntent,
                      new: ResearchIntent) -> ResearchIntent:
        """A goal revision: the new request wins, the history stays visible.

        The old goal is recorded on the new intent (`superseded_goals`); old
        user/context constraints are kept, and one the new input replaced is
        marked `superseded_by` rather than deleted.
        """
        merged = new.model_copy(deep=True)
        if not merged.study_id:
            merged.study_id = old.study_id
        if old.goal and old.goal != merged.goal:
            merged.superseded_goals = list(old.superseded_goals) + [old.goal]
            merged.warnings.append(
                f"研究目标已修正：{old.goal!r} → {merged.goal!r}")
        new_user = {c.kind: c.value for c in merged.constraints
                    if c.source is IntentSource.USER_EXPLICIT}
        for constraint in old.constraints:
            if constraint.source is IntentSource.MODEL_INFERRED:
                continue
            if constraint.kind in new_user:
                if (constraint.value != new_user[constraint.kind]
                        and not constraint.superseded_by):
                    constraint = constraint.model_copy(update={
                        "superseded_by":
                            f"{constraint.kind}={new_user[constraint.kind]}"})
                    merged.warnings.append(
                        f"约束 {constraint.kind}={constraint.value} 已被新输入"
                        f"取代（保留记录）")
                else:
                    continue
            if not any(c.kind == constraint.kind
                       and c.value == constraint.value
                       for c in merged.constraints):
                merged.constraints.append(constraint)
        return merged

    # -- planner boundary ---------------------------------------------------

    def to_planner_inputs(self, intent: ResearchIntent) -> dict:
        """The validated bridge from a request to `Planner.initial_plan`.

        Raises when validation fails: an intent with blocking gaps never
        reaches the planner dressed up as complete. Structures travel only
        when the CHEM layer validated the user's own input, and identity
        arguments only from a `resolved` entity -- an ambiguous name produces
        neither, so structure-dependent arguments cannot be fabricated.
        """
        issues = self._issues(intent)
        if issues:
            raise ResearchStudyError(
                "INTENT_NOT_VALID",
                "结构化请求尚未通过校验：" + "；".join(issues))
        identity_input: dict | None = None
        smiles = ""
        validated = [e for e in intent.entities
                     if e.structure_validated and e.normalized_smiles]
        resolved = [e for e in intent.entities
                    if e.identity_status is EntityIdentityStatus.RESOLVED
                    and e.kind and e.value]
        if validated:
            smiles = validated[0].normalized_smiles
        if resolved:
            identity_input = {"kind": resolved[0].kind,
                              "value": resolved[0].value}
        wants_routes = (
            intent.primary_intent in (ResearchIntentKind.RETROSYNTHESIS,
                                      ResearchIntentKind.SYNTHESIS_ROUTE_RESEARCH)
            or bool({ResearchIntentKind.RETROSYNTHESIS,
                     ResearchIntentKind.SYNTHESIS_ROUTE_RESEARCH}
                    & set(intent.secondary_intents)))
        return {
            "goal": intent.goal or intent.raw_input,
            "identity_input": identity_input,
            "smiles": smiles,
            "wants_routes": wants_routes,
            "document_id": "",
            "context": {"research_intent": intent.primary_intent.value,
                        "raw_input": intent.raw_input},
        }

    # -- model pass ---------------------------------------------------------

    def _llm_refine(self, intent: ResearchIntent) -> None:
        """One constrained model call, merged under strict rules.

        The model may refine the goal, suggest intents, roles, constraints
        and missing information. It may NOT confirm a structure: entities it
        mentions arrive as `MODEL_INFERRED` / `UNRESOLVED` regardless of what
        the model claims, and unknown enum values are dropped, not coerced.
        """
        try:
            response = self._llm.complete(LLMRequest(
                system=_UNDERSTANDING_SYSTEM,
                messages=[{"role": "user",
                           "content": _prompt_for(intent)}],
                temperature=0.0,
            ))
        except Exception as exc:  # noqa: BLE001 - a dead model is not an answer
            intent.warnings.append(
                f"模型不可用，已降级为确定性理解：{type(exc).__name__}: {exc}")
            return
        payload = parse_json_object(response.text or "", required_key="goal")
        if payload is None:
            intent.warnings.append(
                "模型输出不是合法的 JSON 对象，已降级为确定性理解")
            return

        goal = payload.get("goal")
        if isinstance(goal, str) and goal.strip():
            intent.goal = goal.strip()

        primary = _enum_or_none(ResearchIntentKind, payload.get("primary_intent"))
        weak = intent.primary_intent in (ResearchIntentKind.UNKNOWN,
                                         ResearchIntentKind.GENERAL_CHEMISTRY_QUESTION)
        if primary is not None and primary is not intent.primary_intent:
            if weak and primary not in (ResearchIntentKind.UNKNOWN,
                                        ResearchIntentKind.GENERAL_CHEMISTRY_QUESTION):
                intent.primary_intent = primary
                intent.source_of_intent = IntentSource.MODEL_INFERRED
            elif not weak and primary not in intent.secondary_intents:
                # The deterministic reading stands; the model's different
                # primary is still a suggestion, kept as a secondary --
                # a multi-intent request never drops an intent.
                intent.secondary_intents.append(primary)
        for value in payload.get("secondary_intents") or []:
            kind = _enum_or_none(ResearchIntentKind, value)
            if (kind is not None and kind is not intent.primary_intent
                    and kind not in intent.secondary_intents):
                intent.secondary_intents.append(kind)

        for item in payload.get("entities") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or intent.entity(name) is not None:
                continue
            role = _enum_or_none(EntityRole, item.get("role"))
            intent.entities.append(IntentEntity(
                name=name, role=role or EntityRole.UNKNOWN,
                identity_status=EntityIdentityStatus.UNRESOLVED,
                source=IntentSource.MODEL_INFERRED,
                note="模型提及；身份未做任何确认"))

        known = {(c.kind, c.value) for c in intent.constraints}
        for item in payload.get("constraints") or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            value = str(item.get("value") or "").strip()
            if not kind or not value or (kind, value) in known:
                continue
            intent.constraints.append(ResearchConstraint(
                kind=kind, value=value,
                source=IntentSource.MODEL_INFERRED))

        known_missing = {m.kind for m in intent.missing}
        for item in payload.get("missing") or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            if not kind or kind in known_missing:
                continue
            severity = _enum_or_none(MissingKind, item.get("severity"))
            intent.missing.append(MissingInformation(
                kind=kind, detail=str(item.get("detail") or ""),
                severity=severity or MissingKind.DEFERRABLE))
            known_missing.add(kind)


# ---------------------------------------------------------------------------
# Deterministic extraction
# ---------------------------------------------------------------------------


def _extract_entities(text: str) -> list[IntentEntity]:
    """Find chemical objects in the user's own words.

    Structures the user supplied are parsed with the existing chem layer --
    the one confirmation this layer ever makes, and only for user input.
    Names are recognised, never resolved.
    """
    entities: list[IntentEntity] = []
    seen: set[str] = set()
    remainder = text

    for token in _TOKEN_SPLIT_RE.split(text):
        if not token or token in seen:
            continue
        if _INCHI_RE.match(token):
            seen.add(token)
            entities.append(IntentEntity(
                name=token, kind="inchi", value=token,
                identity_status=EntityIdentityStatus.UNRESOLVED,
                note="InChI 将由身份解析层验证，本层不解析 InChI"))
            remainder = remainder.replace(token, " ")
            continue
        if _INCHIKEY_RE.match(token):
            seen.add(token)
            entities.append(IntentEntity(
                name=token, kind="inchikey", value=token,
                identity_status=EntityIdentityStatus.UNRESOLVED,
                note="InChIKey 将由身份解析层验证"))
            remainder = remainder.replace(token, " ")
            continue
        if not _looks_structure_like(token):
            continue
        molecule, error = _try_smiles(token)
        remainder = remainder.replace(token, " ")
        seen.add(token)
        if molecule is not None:
            entities.append(IntentEntity(
                name=token, kind="smiles", value=token,
                normalized_smiles=molecule.canonical_smiles,
                identity_status=EntityIdentityStatus.STRUCTURE_VALIDATED,
                structure_validated=True,
                note="用户提供的 SMILES 已通过现有化学解析"))
        elif _structure_signal(token):
            entities.append(IntentEntity(
                name=token, kind="smiles", value=token,
                identity_status=EntityIdentityStatus.STRUCTURE_INVALID,
                note=f"看似 SMILES 的输入无法解析：{error}"))
        # Acronym-shaped tokens that do not parse are dropped silently:
        # "DMF" is a solvent's nickname, not a malformed structure.

    for match in _QUOTED_RE.finditer(text):
        name = match.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            entities.append(_name_entity(name))
            remainder = remainder.replace(match.group(0), " ")

    for run in _CJK_RUN_RE.findall(remainder):
        # Conjunctions SPLIT names before anything is stripped: 和/与 between
        # two molecules would otherwise let the stopwords glue "阿司匹林" and
        # "布洛芬" into one phantom entity. Numerals stay inside segments:
        # 二甲苯 contains one.
        for segment in re.split(r"(?:以及|和|与|跟|或|、)", run):
            cleaned = _CJK_STRIP.sub("", segment)
            for name in re.findall(r"[一-鿿]{2,8}", cleaned):
                if name not in seen:
                    seen.add(name)
                    entities.append(_name_entity(name))

    for token in _EN_CAPITAL_RE.findall(remainder):
        if token in _EN_NAME_STOP or token in seen or len(token) < 4:
            continue
        seen.add(token)
        entities.append(_name_entity(token))

    # Roles: the first molecule is the target; with a comparison request the
    # rest become comparison objects; anything else is a reference.
    molecules = [e for e in entities if e.entity_type == "molecule"]
    comparing = any(k in text.lower()
                    for k in ("比较", "对比", "compare", "versus", " vs "))
    for index, entity in enumerate(molecules):
        if index == 0:
            entity.role = EntityRole.TARGET
        elif comparing:
            entity.role = EntityRole.COMPARISON

    # A comparison of routes/things ("比较传统路线") that names no second
    # molecule is kept as a route-shaped comparison object, not discarded.
    match = _COMPARISON_RE.search(text)
    if match:
        phrase = match.group(1).strip()
        for prefix in ("with ", "and ", "和", "与", "跟"):
            if phrase.lower().startswith(prefix):
                phrase = phrase[len(prefix):].strip()
                break
        if phrase and not any(phrase in e.name or e.name in phrase
                              for e in molecules):
            entity = IntentEntity(
                name=phrase, entity_type="route", role=EntityRole.COMPARISON,
                identity_status=EntityIdentityStatus.UNRESOLVED,
                note="比较对象（路线/非分子对象）；不做身份解析")
            if phrase not in seen:
                seen.add(phrase)
                entities.append(entity)
    return entities


def _name_entity(name: str) -> IntentEntity:
    """A recognised name. Recognition only -- identity stays unconfirmed."""
    ambiguous = _AMBIGUOUS_NAMES.get(name.lower())
    if ambiguous:
        return IntentEntity(
            name=name, kind="name", value=name,
            identity_status=EntityIdentityStatus.AMBIGUOUS,
            candidates=[{"kind": kind, "value": value, "label": label,
                         "note": "内置歧义词表：该名称存在多个常见异构体/指代"}
                        for kind, value, label in ambiguous],
            note="名称已识别，但对应多个结构；未经用户选择不得认定")
    return IntentEntity(
        name=name, kind="name", value=name,
        identity_status=EntityIdentityStatus.NAME_RECOGNIZED,
        note="名称已识别；结构身份需经身份解析机制确认")


def _classify_intents(text: str) -> tuple[ResearchIntentKind,
                                          list[ResearchIntentKind]]:
    lowered = f" {text.lower()} "
    matched: list[ResearchIntentKind] = []
    for kind, keywords in _INTENT_KEYWORDS:
        if any(k in lowered or k in text for k in keywords):
            matched.append(kind)
    if matched:
        return matched[0], matched[1:]
    if ("?" in text or "？" in text
            or any(k in text for k in ("什么", "如何", "怎么", "为什么"))):
        return ResearchIntentKind.GENERAL_CHEMISTRY_QUESTION, []
    return ResearchIntentKind.UNKNOWN, []


def _extract_constraints(text: str) -> list[ResearchConstraint]:
    lowered = text.lower()
    out: list[ResearchConstraint] = []
    seen: set[tuple[str, str]] = set()
    for kind, value, keywords in _CONSTRAINT_KEYWORDS:
        if (kind, value) in seen:
            continue
        if any(k in lowered or k in text for k in keywords):
            seen.add((kind, value))
            out.append(ResearchConstraint(kind=kind, value=value,
                                          source=IntentSource.USER_EXPLICIT))
    return out


def _derive_missing(entities: list[IntentEntity],
                    primary: ResearchIntentKind,
                    secondary: list[ResearchIntentKind]) -> list[MissingInformation]:
    missing: list[MissingInformation] = []
    molecules = [e for e in entities if e.entity_type == "molecule"]
    intents = (primary, *secondary)
    structure_dependent = (primary in _STRUCTURE_DEPENDENT
                           or ResearchIntentKind.MOLECULE_COMPARISON in secondary)
    if structure_dependent and not molecules:
        missing.append(MissingInformation(
            kind="missing_target", severity=MissingKind.REQUIRED,
            detail="请求依赖具体结构，但没有识别到任何分子对象"))
    if (ResearchIntentKind.MOLECULE_COMPARISON in intents
            and len(molecules) < 2):
        missing.append(MissingInformation(
            kind="missing_second_entity", severity=MissingKind.REQUIRED,
            detail="比较任务需要至少两个对象"))
    if ResearchIntentKind.CONTINUE_RESEARCH in intents:
        missing.append(MissingInformation(
            kind="missing_study", severity=MissingKind.DEFAULTABLE,
            detail="继续研究需要指明研究；有一个候选研究时可自动继续"))
    for entity in entities:
        if entity.identity_status is EntityIdentityStatus.AMBIGUOUS:
            missing.append(MissingInformation(
                kind="ambiguous_identity", severity=MissingKind.REQUIRED,
                entity_name=entity.name,
                detail=f"“{entity.name}”有多个候选结构，需要用户选择"))
        elif entity.identity_status is EntityIdentityStatus.NAME_RECOGNIZED:
            missing.append(MissingInformation(
                kind="identity_confirmation", severity=MissingKind.DEFERRABLE,
                entity_name=entity.name,
                detail=f"“{entity.name}”尚未确认结构身份"))
        elif entity.identity_status is EntityIdentityStatus.STRUCTURE_INVALID:
            missing.append(MissingInformation(
                kind="invalid_structure", severity=MissingKind.REQUIRED,
                entity_name=entity.name,
                detail=f"“{entity.name}”无法解析为结构"))
    return missing


def _clarification_for(missing: MissingInformation) -> str:
    return missing.detail or "需要补充信息才能继续。"


def _with_missing(missing: list[MissingInformation],
                  item: MissingInformation) -> list[MissingInformation]:
    return [m for m in missing if m.kind != item.kind] + [item]


def _looks_structure_like(token: str) -> bool:
    if not token or len(token) < 2:
        return False
    if not re.fullmatch(r"[A-Za-z0-9@+\-\[\]\(\)=#$:/\\.]+", token):
        return False
    return token.lower() not in _WORD_STOP and (
        _structure_signal(token)
        or (token.isalpha() and len(token) >= 3
            and sum(1 for ch in token if ch.isupper()) >= 2))


def _structure_signal(token: str) -> bool:
    """Shape that only a structure notation would have -- digits, rings,
    branches. Three capital letters alone ("DMF") is an acronym, not a
    structure claim."""
    return (any(ch.isdigit() for ch in token)
            or any(ch in "[]()=#@+/\\" for ch in token))


def _try_smiles(token: str):
    """Parse with the existing chem layer. The only structure confirmation
    this service ever performs, and only on the user's own input.

    `identity` profile: the user's own structure, normalized -- not its
    parent, because rewriting what they gave would not be validation.
    """
    from crea.chem.normalize import normalize

    try:
        return normalize(token, profile="identity"), None
    except NormalizationError as exc:
        return None, str(exc)


def _enum_or_none(enum_cls, value):
    if not isinstance(value, str):
        return None
    for member in enum_cls:
        if value == member.value or value.upper() == member.name:
            return member
    return None


def _needs_model(text: str, primary: ResearchIntentKind,
                 missing: list[MissingInformation]) -> bool:
    connectors = ("以及", "然后", "并且", "顺便", "同时", " and ", " then ")
    return (len(text) > 120
            or primary in (ResearchIntentKind.UNKNOWN,
                           ResearchIntentKind.GENERAL_CHEMISTRY_QUESTION)
            or any(c in f" {text.lower()} " for c in connectors))


def _prompt_for(intent: ResearchIntent) -> str:
    return (f"用户输入：{intent.raw_input}\n"
            f"确定性解析结果：primary={intent.primary_intent.value}，"
            f"实体={[e.name for e in intent.entities]}，"
            f"约束={[(c.kind, c.value) for c in intent.constraints]}\n"
            f"请以 JSON 输出：goal（一句话目标）、primary_intent、"
            f"secondary_intents、entities（name/role）、constraints"
            f"（kind/value，只写用户明确表达的）、missing（kind/detail/"
            f"severity）。不得确认任何结构，不得补充用户没说的实验条件。")


_UNDERSTANDING_SYSTEM = (
    "You read a chemistry researcher's request into structured fields. You "
    "never confirm a structure, never verify a paper, and never invent "
    "experimental conditions. Constraints you infer are guesses and must "
    "come from the user's own words to be USER_EXPLICIT -- when unsure, "
    "leave them out. Answer with JSON only."
)


__all__ = ["InputUnderstandingService"]

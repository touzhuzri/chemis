"""The prompt contract of the AI Research Intelligence (M2.0, revised M2.0.1).

Three jobs, and nothing else:

* state what the model *is* and the epistemic rules it works under;
* render the current research state as one **budgeted** JSON document;
* turn the model's reply into a `ResearchDecision`, or say why it cannot.

Parsing is strict on purpose. `parse_decision` accepts a bare JSON object
or a fenced ```json block and nothing else -- no prose extraction, no
"find the decision inside the paragraph". A reply that is not a decision
is not a decision; the caller reports `UNPARSEABLE` and may send at most a
bounded number of repair requests. That is the difference between a gate
and a guess.

M2.0.1 -- the budget itself. The first real-model acceptance run (44
tools) proved that a render which lets the full tool catalog consume the
whole budget drains the observations to zero: the model never sees what
the tools returned. The rule now is explicit and enforced by
`render_state_document`:

    **A tool result the model must reason about outranks the catalog of
    tools it might call next, which outranks old decision history.**

The budget (`STATE_BUDGET_CHARS`) is an *approximate character* bound on
the rendered state document -- not a token count, and never claimed to be
one (`context_budget.approximate` says so inside the document). Whatever
is dropped is declared in `context_budget` and `state_truncated`; a
trimmed state never pretends to be a whole one. The original
`Observation` objects are never modified: every compression happens on
the serialization built here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from crea.core.json_parse import parse_json_object
from crea.core.models.research_decision import (
    INTELLIGENCE_PROMPT_VERSION,
    ResearchDecision,
)

#: Hard bound on one rendered state document, in *characters*. An
#: approximate budget: characters are a stable, tokenizer-independent
#: proxy, and the rendered document says so rather than pretending to be
#: a token budget.
STATE_BUDGET_CHARS = 24_000

#: Room reserved inside the budget for the `context_budget` diagnostics
#: block itself, so the accounting never blows the budget it accounts for.
CONTEXT_DIAGNOSTICS_RESERVE = 520

#: Progressively smaller previews of one observation's `data`, tried in
#: order when the observation share is tight. The last rung is the floor:
#: even a pathological payload keeps a recognizable head of itself.
_OBSERVATION_DATA_LIMITS = (None, 900, 450, 180)

#: Section shares of the non-core remainder, in characters' worth of
#: proportion. Observations get the largest slice -- that is the fix. The
#: catalog's share is enough for the compact tier at the production tool
#: count (36 canonical tools ≈ 6.2k chars lite), and the ladder inside
#: `_fit_catalog` drops further only if a registry is fatter than that.
_OBSERVATION_SHARE = 0.43
_CATALOG_SHARE = 0.33
# The rest (0.24) is decision/fact/hypothesis/issue/signature history.

#: Longest goal / direction strings shown when the budget is tight.
_GOAL_CLIP = 1500
_DIRECTION_CLIP = 800
_CONSTRAINT_CLIP = 200
_EVIDENCE_ID_CLIP_COUNT = 80

SYSTEM = f"""你是 CREA 的 AI Research Intelligence——化学科研决策主体。
版本：prompt {INTELLIGENCE_PROMPT_VERSION}。

你的职责是围绕用户的研究目标，自主决定下一步研究行动：选择工具、提出并维护可检验的
假设、根据实际工具结果调整研究方向、在证据不足时如实说明、在需要人回答时提问。

## 必须遵守的科研真实性规则

1. 只能调用「可用工具目录」里注册的工具，参数必须符合该工具的参数 Schema。
   不能执行任意 Python、Shell、SQL，不能调用未注册函数。
2. 工具执行结果（observations）是唯一可以当作事实的内容。你的假设、推断和解读
   永远不是事实。**observations 里的 data 字段是工具返回的真实数据本身，必须
   基于它推理，而不是基于你对工具行为的猜测。**
3. 不得编造 DOI、反应条件、产率、文献结论、实验结果。没有来源的数值不能写进
   假设的支持证据。
4. 只能引用「已有证据」列表里真实存在的 evidence_id。没有列表里的 id 就不要引用。
5. 文献发现不等于实验证据；SMARTS 匹配不等于实验成功；检索不到不等于化学上
   不可能；Unknown 不等于 false；工具执行成功不等于科学结论正确。
6. 结构身份未确认（identity_status 不是 resolved / structure_validated）时，
   不得调用依赖该结构的计算或路线工具；可以做名称级文献发现，但必须说明这是
   名称级发现而不是已验证的化学证据。
7. 不得把候选结果（candidates）升级为事实。候选永远是候选。
8. 不得声称你拥有未出现在上下文里的权限；高风险工具会由系统要求人工审批，
   你无法自行授权。

## 关于状态文档的预算（M2.0.1）

状态文档受近似字符预算约束，`context_budget` 字段如实报告各部分占用与省略：

- observations（真实工具结果）优先级最高，最近的观察一定保留（必要时数据被
  截短并标记 `data_truncated_for_prompt`；原始完整结果不在 prompt 里，但事实
  本身没有丢失）。
- 工具目录按需压缩：`catalog_mode` 为 `full` 时每个工具带完整参数 Schema；
  为 `focused` 时与研究目标最相关的工具带完整 Schema、其余为简要条目
  （参数名与类型）；为 `compact` 时全部为简要条目；为 `names` 时只有名称与
  用途。简要条目缺少的参数细节，会在你提出调用后由校验器在 issues 中按字段
  指出——按 issues 补全参数即可，不要因此放弃有价值的工具。
- 较早的决策历史与目录描述可能被压缩或省略；`context_budget.omitted` 列出
  省略数量。需要更早的信息时，可通过研究上下文类工具查询，不要凭空补写。

## 决策协议

每次必须且只能返回一个 JSON 对象，形如：

{{"kind": "<六种之一>", ...该 kind 的字段..., "observation_interpretation": "..."}}

六种 kind 与各自必需字段：

- `continue_with_tool`：
  `tool_call`: {{"tool", "arguments", "decision_summary", "purpose",
  "expected_information", "related_hypothesis_ids", "relevant_evidence_ids"}}
- `update_hypothesis`：
  `hypothesis_update`: {{"hypothesis_id"(新假设留空), "hypothesis",
  "rationale_summary", "supporting_evidence_ids", "contradicting_evidence_ids",
  "uncertainty", "next_verification_action", "proposed_status"}}
- `update_direction`：
  `direction_update`: {{"previous_direction", "new_direction", "reason",
  "triggering_step_ids", "relevant_evidence_ids", "abandoned"}}
- `ask_user`：
  `ask_user`: {{"question", "why", "expected_information", "options"}}
- `complete`：
  `completion`: {{"completion_reason", "achieved_objectives",
  "unresolved_questions", "evidence_sufficiency", "limitations"}}
- `blocked`：
  `blocked`: {{"reason", "blocker", "details", "relevant_evidence_ids"}}

`observation_interpretation` 是你对最近一次工具结果的科学解读（意味着什么、
支持或反驳了哪个假设、下一步该验证什么）。它不会覆盖原始工具结果，也不会被
当成事实保存。解读必须引用 observations 里真实出现的数据（数值、分子式、
文献标题等）；如果数据不可见，如实说明，不要假装看见。

只能返回 JSON，不要返回解释性文字、不要返回 Markdown 标题、不要返回多个对象。
`evidence_sufficiency` 只能是 `sufficient` / `partial` / `insufficient`。
`proposed_status` 只能是 `proposed` / `under_review` / `supported` /
`refuted` / `unresolved`；标为 `supported` 必须给出真实的 supporting_evidence_ids。
"""


# ---------------------------------------------------------------------------
# Rendered state + diagnostics
# ---------------------------------------------------------------------------


@dataclass
class RenderedState:
    """One rendered state document and the honest accounting of its budget.

    Every number here was measured on the serialized document that was
    actually produced -- not estimated -- so a reader can see which part
    of the context is expensive and what the trim cost.
    """

    text: str
    budget_chars: int = STATE_BUDGET_CHARS
    total_chars: int = 0
    core_chars: int = 0
    tool_catalog_chars: int = 0
    observations_chars: int = 0
    decisions_chars: int = 0
    facts_chars: int = 0
    hypotheses_chars: int = 0
    issues_chars: int = 0
    signatures_chars: int = 0
    #: "full" | "focused" | "compact" | "names".
    catalog_mode: str = "full"
    tool_count: int = 0
    omitted_tool_count: int = 0
    observation_count: int = 0
    omitted_observation_count: int = 0
    omitted_decision_count: int = 0
    omitted_fact_count: int = 0
    truncated: bool = False
    truncation_reasons: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        """The diagnostics, as the small dict stored on a `DecisionRecord`."""
        return {
            "budget_chars": self.budget_chars,
            "total_chars": self.total_chars,
            "approximate": "字符预算，非精确 token 计数",
            "catalog_mode": self.catalog_mode,
            "sections": {
                "core": self.core_chars,
                "tool_catalog": self.tool_catalog_chars,
                "observations": self.observations_chars,
                "decisions": self.decisions_chars,
                "facts": self.facts_chars,
                "hypotheses": self.hypotheses_chars,
                "issues": self.issues_chars,
                "signatures": self.signatures_chars,
            },
            "counts": {
                "tools": self.tool_count,
                "observations": self.observation_count,
            },
            "omitted": {
                "tools": self.omitted_tool_count,
                "observations": self.omitted_observation_count,
                "decisions": self.omitted_decision_count,
                "facts": self.omitted_fact_count,
            },
            "reasons": list(self.truncation_reasons[:6]),
        }


def render_state(state: Any) -> str:
    """The research state as one bounded JSON document (text only).

    Kept for callers that only want the prompt text; callers that need
    the budget accounting ask `render_state_document` instead.
    """
    return render_state_document(state).text


def render_state_document(state: Any) -> RenderedState:
    """The research state as one budgeted JSON document, with accounting.

    Fast path: when the whole state (tool catalog included, in full) plus
    the diagnostics block fits the budget, nothing is trimmed -- a small
    registry sees exactly what it always saw, plus its accounting. Slow
    path: the priority ladder in the module docstring, applied to fresh
    copies; `state.as_prompt_dict()` and the state's own lists are never
    mutated, so the next turn renders from the same originals.
    """
    payload = state.as_prompt_dict()
    _, rendered = _assemble(payload)
    if (rendered.total_chars + CONTEXT_DIAGNOSTICS_RESERVE
            <= STATE_BUDGET_CHARS):
        _, rendered = _finalize(dict(payload), rendered)
        return rendered

    # -- slow path: priority-budgeted assembly -----------------------------
    reasons: list[str] = []
    core, core_chars = _core_section(payload, reasons)
    remaining = max(0, STATE_BUDGET_CHARS - CONTEXT_DIAGNOSTICS_RESERVE
                    - core_chars)
    obs_share = int(remaining * _OBSERVATION_SHARE)
    cat_share = int(remaining * _CATALOG_SHARE)
    hist_share = remaining - obs_share - cat_share

    observations, obs_chars, kept_obs, omitted_obs = _fit_observations(
        payload.get("observations") or [], obs_share, reasons)
    catalog, cat_chars, mode, omitted_tools = _fit_catalog(
        payload.get("available_tools") or [], cat_share,
        goal_text=_goal_text(payload), reasons=reasons)
    history, hist_chars, hist_counts = _fit_history(payload, hist_share,
                                                    reasons)

    document = dict(core)
    document["established_facts"] = history["facts"]
    document["hypotheses"] = history["hypotheses"]
    document["evidence_ids_held"] = core["evidence_ids_held"]
    document["decisions_already_taken"] = history["decisions"]
    document["open_issues"] = history["issues"]
    document["observations"] = observations
    document["tool_calls_already_made"] = history["signatures"]
    document["available_tools"] = catalog

    rendered = RenderedState(
        text="", core_chars=core_chars,
        tool_catalog_chars=cat_chars, observations_chars=obs_chars,
        decisions_chars=hist_counts["decisions_chars"],
        facts_chars=hist_counts["facts_chars"],
        hypotheses_chars=hist_counts["hypotheses_chars"],
        issues_chars=hist_counts["issues_chars"],
        signatures_chars=hist_counts["signatures_chars"],
        catalog_mode=mode,
        tool_count=len(payload.get("available_tools") or []),
        omitted_tool_count=omitted_tools,
        observation_count=kept_obs,
        omitted_observation_count=omitted_obs,
        omitted_decision_count=hist_counts["omitted_decisions"],
        omitted_fact_count=hist_counts["omitted_facts"],
        truncated=True, truncation_reasons=reasons)
    document, rendered = _finalize(document, rendered)
    return rendered


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


#: The parts that must survive any trim: who is researching what, on which
#: entities, under which constraints, and which evidence ids may be cited.
_CORE_KEYS = (
    "research_goal", "session_id", "study_id", "research_direction",
    "constraints", "chemical_entities",
    "entities_whose_structure_is_not_settled", "evidence_ids_held",
)


def _core_section(payload: dict, reasons: list[str]) -> tuple[dict, int]:
    """The non-negotiable core, clipped only when the goal itself is huge."""
    core = {}
    for key in _CORE_KEYS:
        core[key] = payload.get(key)
    goal = core.get("research_goal") or ""
    if isinstance(goal, str) and len(goal) > _GOAL_CLIP:
        core["research_goal"] = goal[:_GOAL_CLIP] + "…"
        reasons.append("研究目标超长，已截断展示（原始目标保留在研究上下文中）")
    direction = core.get("research_direction") or ""
    if isinstance(direction, str) and len(direction) > _DIRECTION_CLIP:
        core["research_direction"] = direction[:_DIRECTION_CLIP] + "…"
    constraints = core.get("constraints")
    if isinstance(constraints, list):
        clipped = [c[:_CONSTRAINT_CLIP] for c in constraints[:20]
                   if isinstance(c, str)]
        if len(constraints) > 20:
            reasons.append(
                f"约束仅展示前 20 条（共 {len(constraints)} 条）")
        core["constraints"] = clipped
    held = core.get("evidence_ids_held")
    if isinstance(held, list) and len(held) > _EVIDENCE_ID_CLIP_COUNT:
        core["evidence_ids_held"] = held[-_EVIDENCE_ID_CLIP_COUNT:]
        reasons.append(
            f"evidence_ids_held 仅展示最近 {_EVIDENCE_ID_CLIP_COUNT} 条"
            f"（共 {len(held)} 条）")
    return core, _cost(core)


def _fit_observations(items: list, share: int, reasons: list[str]
                      ) -> tuple[list, int, int, int]:
    """Newest observations first, up to the share. Never silently empty.

    The newest observation is always kept -- its `data` shrinks through
    the preview ladder if it must, and the shrink is flagged on the entry
    itself (`data_truncated_for_prompt`) and in `reasons`. Older
    observations fill what is left, newest first; the first one that
    cannot fit (even at the floor) ends the window and the rest are
    counted as omitted.
    """
    kept: list[dict] = []
    used = 0
    total = len(items)
    for index in range(total - 1, -1, -1):  # newest first
        item = items[index]
        entry, cost, flagged = _observation_entry(
            item, share - used, force_floor=not kept)
        if entry is None:
            omitted = index + 1  # this one, and everything older
            if not kept:
                reasons.append(
                    "预算在核心状态后已耗尽，最近一次观察也无法放入；"
                    "这是显式报告，不是静默丢弃")
            return kept, used, total - omitted, omitted
        kept.insert(0, entry)
        used += cost
        if flagged:
            reasons.append(
                f"观察 {entry.get('step_id') or entry.get('tool')}"
                f" 的数据为放入预算被截短（原始结果保留在运行记录中）")
    return kept, used, len(kept), 0


def _observation_entry(item: Any, room: int, *, force_floor: bool
                       ) -> tuple[dict | None, int, bool]:
    """One observation rendered to fit `room`, or None when it cannot.

    Returns `(entry, cost, data_was_shrunk)`. The data preview ladder
    runs from the stored preview down to the floor; `force_floor` (the
    newest observation) may exceed `room` only down to the floor rung --
    it is still reported, never silently dropped.
    """
    if not isinstance(item, dict):
        item = {"value": _stringify(item)}
    base = dict(item)
    summary = base.get("summary")
    if isinstance(summary, str) and len(summary) > 240:
        base["summary"] = summary[:240] + "…"
    for limit in _OBSERVATION_DATA_LIMITS:
        entry = dict(base)
        flagged = False
        if limit is not None and "data" in entry:
            preview = _preview(entry.get("data"), limit)
            if preview is not _UNCHANGED:
                entry["data"] = preview
                entry["data_truncated_for_prompt"] = True
                flagged = True
        cost = _cost(entry)
        if cost <= room or (force_floor and limit == _OBSERVATION_DATA_LIMITS[-1]):
            return entry, cost, flagged
        if limit == _OBSERVATION_DATA_LIMITS[-1]:
            break
    return None, 0, False


def _fit_catalog(tools: list, share: int, *, goal_text: str,
                 reasons: list[str]) -> tuple[list, int, str, int]:
    """The tool catalog, compressed by a fixed ladder. Names never hide.

    full    -- every canonical tool with its complete parameter schema.
    focused -- full schema for the tools most relevant to the goal,
               compact entries for the rest.
    compact -- every tool as name + purpose + parameter names/types.
    names   -- name + purpose only (pathological budgets).

    Aliases never appear: `_catalog()` already excludes them, and this
    function only ever receives canonical entries. A tool may lose its
    schema; it may not lose its name -- the model must always be able to
    see what exists and try it, with the validator spelling out any
    missing schema detail as actionable issues.
    """
    if not tools:
        return [], 0, "full", 0
    if _cost(tools) <= share:
        return tools, _cost(tools), "full", 0

    ranked = _rank_tools(tools, goal_text)
    for keep in (6, 4, 3, 2, 1):
        top = {e.get("name") for e in ranked[:keep]}
        entries = [dict(item) if item.get("name") in top
                   else _compact_tool(item) for item in tools]
        if _cost(entries) <= share:
            reasons.append(
                f"工具目录：与目标最相关的 {keep} 个工具带完整 Schema，"
                f"其余为简要条目（参数不全时校验器会按字段指出）")
            return entries, _cost(entries), "focused", 0

    compact = [_compact_tool(item) for item in tools]
    if _cost(compact) <= share:
        reasons.append(
            "工具目录：完整参数 Schema 超出预算，已压缩为简要条目"
            "（参数不全时校验器会按字段指出）")
        return compact, _cost(compact), "compact", 0

    lite = [_compact_tool(item, lite=True) for item in tools]
    if _cost(lite) <= share:
        reasons.append(
            "工具目录：仅必需参数带类型，其余参数只列名字；"
            "提出调用后校验器会按字段给出参数要求")
        return lite, _cost(lite), "compact", 0

    named = [{"name": item.get("name"),
              "purpose": _clip(item.get("description"), 80)}
             for item in tools]
    if _cost(named) <= share:
        reasons.append(
            "工具目录：仅保留工具名与用途；提出调用后校验器会给出参数要求")
        return named, _cost(named), "names", 0

    # Pathological: keep as many names as fit, declare the rest.
    kept: list = []
    used = 0
    omitted_names: list[str] = []
    for entry in named:
        cost = _cost(entry)
        if used + cost <= share:
            kept.append(entry)
            used += cost
        else:
            omitted_names.append(str(entry.get("name")))
    if kept:
        reasons.append(
            "工具目录：预算不足以列出全部工具，已省略 "
            f"{len(omitted_names)} 个（如需可先用研究上下文类工具查询）")
    return kept, used, "names", len(omitted_names)


def _rank_tools(tools: list, goal_text: str) -> list[dict]:
    """Tools by relevance to the goal; ties keep the catalog's own order.

    Relevance is spelling, not interpretation: English/alphanumeric
    tokens of the goal that appear in the tool's name or description. A
    goal with no such tokens yields a stable order -- no tool is
    penalized into invisibility, because every name stays listed.
    """
    tokens = {t for t in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}",
                                    (goal_text or "").lower())}
    scored: list[tuple[int, int, dict]] = []
    for index, item in enumerate(tools):
        hay = f"{item.get('name', '')} {item.get('description', '')}".lower()
        score = sum(1 for token in tokens if token in hay)
        scored.append((-score, index, item))
    scored.sort(key=lambda row: (row[0], row[1]))
    return [item for _, _, item in scored]


#: JSON-schema type -> the short token shown in a compact catalog entry.
_SHORT_TYPES = {
    "string": "str", "integer": "int", "number": "float", "boolean": "bool",
    "array": "list", "object": "dict",
}


def _compact_tool(item: dict, *, lite: bool = False) -> dict:
    """One catalog entry, decision-relevant fields only.

    Keeps the name, a clipped purpose, the parameter names and types (only
    the *required* ones in `lite` mode, with the optional names listed),
    the facts the tool needs and whether a human must approve it --
    everything needed to *propose* a call. The complete schema is one
    validation round-trip away: the validator's issues name missing or
    invalid fields precisely, which is the second tier of the layered
    catalog (M2.0.1). Nothing here may drop a tool's name.
    """
    schema = item.get("input_schema") or item.get("arguments") or {}
    if not isinstance(schema, dict):
        schema = {}
    properties = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    kinds: dict[str, str] = {}
    for name, spec in properties.items():
        if isinstance(spec, dict):
            kind = spec.get("type")
            if not kind:
                kind = ("one_of" if ("anyOf" in spec or "oneOf" in spec)
                        else "any")
        else:
            kind = "any"
        kinds[name] = _SHORT_TYPES.get(kind, kind)
    out: dict = {
        "name": item.get("name"),
        "purpose": _clip(item.get("description"), 56 if lite else 80),
    }
    if lite:
        required_kinds = {n: kinds[n] for n in required if n in kinds}
        optional = [n for n in properties if n not in required]
        if required_kinds:
            out["params"] = required_kinds
        if optional:
            out["optional_params"] = optional
    else:
        if kinds:
            out["params"] = kinds
        if required:
            out["required"] = required
    facts = item.get("requires_facts") or item.get("requires") or []
    if facts:
        out["requires_facts"] = list(facts)
    if item.get("needs_human_approval") or item.get("approval") == "required":
        out["needs_human_approval"] = True
    return out


def _fit_history(payload: dict, share: int, reasons: list[str]) -> tuple[
        dict, int, dict]:
    """Study memory inside the history share, newest first.

    Order of access: established facts, hypotheses, open issues, recent
    decision summaries, executed-call signatures. Each section keeps the
    newest entries that fit; a section that overflows says how many it
    dropped. Entries are already compact rows -- the clip bounds only
    stop one runaway summary from eating the share.
    """
    used = 0
    counts = {"decisions_chars": 0, "facts_chars": 0, "hypotheses_chars": 0,
              "issues_chars": 0, "signatures_chars": 0,
              "omitted_decisions": 0, "omitted_facts": 0}

    facts, cost, omitted = _fill_newest(
        payload.get("established_facts") or [], share - used,
        clip_fields=(("summary", 200),))
    used += cost
    counts["facts_chars"] = cost
    counts["omitted_facts"] = omitted
    if omitted:
        reasons.append(f"较早的 {omitted} 条已建立事实未放入 prompt")

    hypotheses, cost, omitted = _fill_newest(
        payload.get("hypotheses") or [], share - used,
        clip_fields=(("content", 260),))
    used += cost
    counts["hypotheses_chars"] = cost
    if omitted:
        reasons.append(f"较早的 {omitted} 条假设未放入 prompt")

    issues, cost, _ = _fill_newest(
        payload.get("open_issues") or [], share - used,
        clip_fields=(("content", 200),))
    used += cost
    counts["issues_chars"] = cost

    decisions, cost, omitted = _fill_newest(
        payload.get("decisions_already_taken") or [], share - used,
        clip_fields=(("summary", 160), ("decision", 200), ("reason", 160)))
    used += cost
    counts["decisions_chars"] = cost
    counts["omitted_decisions"] = omitted
    if omitted:
        reasons.append(f"较早的 {omitted} 条决策历史未放入 prompt（保留了最近的）")

    signatures, cost, _ = _fill_newest(
        payload.get("tool_calls_already_made") or [], share - used,
        as_strings=True)
    used += cost
    counts["signatures_chars"] = cost

    return {
        "facts": facts, "hypotheses": hypotheses, "issues": issues,
        "decisions": decisions, "signatures": signatures,
    }, used, counts


def _fill_newest(items: list, share: int, *, clip_fields=(),
                 as_strings: bool = False) -> tuple[list, int, int]:
    """The newest entries that fit `share`, in their original order."""
    kept: list = []
    used = 0
    omitted = 0
    for item in reversed(list(items)):
        if as_strings:
            entry = _clip(item, 160) if isinstance(item, str) else str(item)
        elif isinstance(item, dict):
            entry = dict(item)
            for name, limit in clip_fields:
                value = entry.get(name)
                if isinstance(value, str) and len(value) > limit:
                    entry[name] = value[:limit] + "…"
        else:
            entry = {"value": _stringify(item)}
        cost = _cost(entry)
        if used + cost > share:
            omitted += 1
            continue
        kept.insert(0, entry)
        used += cost
    return kept, used, omitted


# ---------------------------------------------------------------------------
# Assembly and helpers
# ---------------------------------------------------------------------------


def _assemble(payload: dict) -> tuple[dict, RenderedState]:
    """The untouched document plus its measured accounting."""
    text = _dumps(payload)
    sections = {}
    for key, name in (
        ("established_facts", "facts"), ("hypotheses", "hypotheses"),
        ("open_issues", "issues"), ("decisions_already_taken", "decisions"),
        ("observations", "observations"), ("available_tools", "tool_catalog"),
        ("tool_calls_already_made", "signatures"),
    ):
        sections[name] = _cost(payload.get(key))
    core_chars = _cost({k: payload.get(k) for k in _CORE_KEYS})
    rendered = RenderedState(
        text=text, total_chars=len(text), core_chars=core_chars,
        tool_catalog_chars=sections["tool_catalog"],
        observations_chars=sections["observations"],
        decisions_chars=sections["decisions"],
        facts_chars=sections["facts"],
        hypotheses_chars=sections["hypotheses"],
        issues_chars=sections["issues"],
        signatures_chars=sections["signatures"],
        tool_count=len(payload.get("available_tools") or []),
        observation_count=len(payload.get("observations") or []),
        truncated=False)
    return payload, rendered


def _finalize(document: dict, rendered: RenderedState
              ) -> tuple[dict, RenderedState]:
    """Attach the diagnostics, mark truncation honestly, measure the text."""
    if rendered.truncated:
        document["state_truncated"] = (
            "较早的研究历史或部分数据因上下文预算被省略（清单见 context_budget）；"
            "如需请重新查询研究上下文。")
    document["context_budget"] = rendered.summary()
    text = _dumps(document)
    rendered.total_chars = len(text)
    rendered.text = text
    if rendered.total_chars > STATE_BUDGET_CHARS:
        # The arithmetic reserved room for the diagnostics block; if a
        # pathological core still overflows, the document says so rather
        # than the caller pretending the budget held.
        rendered.truncation_reasons.append(
            f"渲染后总长 {rendered.total_chars} 仍超过预算 "
            f"{STATE_BUDGET_CHARS}（核心状态过大）")
    return document, rendered


def _goal_text(payload: dict) -> str:
    parts = [payload.get("research_goal"), payload.get("research_direction")]
    return " ".join(str(p) for p in parts if p)


def _cost(value: Any) -> int:
    return len(_dumps(value))


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _stringify(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


_UNCHANGED = object()


def _preview(data: Any, limit: int) -> Any:
    """A bounded preview of `data`, or `_UNCHANGED` when it already fits.

    A dict preview keeps the head of the serialized form and says so;
    the original value is never modified -- the caller stores the preview
    in a copy.
    """
    text = _stringify(data)
    if len(text) <= limit:
        return _UNCHANGED
    return {"truncated_preview": text[:limit], "original_chars": len(text)}


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else _stringify(value)
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------
# Message assembly, repair, parsing
# ---------------------------------------------------------------------------


def build_user_message(state: Any, *, budget_note: str = "") -> tuple[
        str, RenderedState]:
    """One user message: the budgeted state, the remaining budget, what to do.

    Returns `(message, rendered)` so the loop can record the budget
    accounting on the decision it is about to request.
    """
    rendered = render_state_document(state)
    parts = [
        "当前研究状态如下：",
        rendered.text,
    ]
    if budget_note:
        parts.append(f"预算提示：{budget_note}")
    parts.append("请给出下一步的唯一一个研究决策（JSON）。")
    return "\n\n".join(parts), rendered


def repair_message(issues: list[str]) -> str:
    """What the model is told when its reply could not be used."""
    listed = "\n".join(f"- {issue}" for issue in issues) or "- 未能解析为决策"
    return (
        "你上一次的回复不能作为研究决策使用，问题如下：\n"
        f"{listed}\n\n"
        "请重新只输出一个符合协议的 JSON 决策对象。不要解释，不要输出其他内容。"
    )


def parse_decision(text: str) -> tuple[ResearchDecision | None, list[str]]:
    """A decision from the reply, or the reasons there is none.

    Returns `(decision, [])` on success and `(None, issues)` on failure.
    The issues are what a repair request repeats back to the model -- they
    are read from the reply, never invented.
    """
    payload = parse_json_object(text or "", required_key="kind")
    if payload is None:
        return None, ["回复不是单个 JSON 对象，或缺少 'kind' 字段"]
    try:
        return ResearchDecision.model_validate(payload), []
    except Exception as exc:  # noqa: BLE001 - a schema refusal is the report
        return None, _readable_issues(exc)


def _readable_issues(exc: Exception) -> list[str]:
    """The pydantic refusal, as sentences a model can act on."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return [f"决策未通过 Schema 校验：{exc}"]
    out: list[str] = []
    for error in errors():
        location = ".".join(str(x) for x in error.get("loc", ())) or "决策"
        out.append(f"{location}: {error.get('msg')}")
    return out or [f"决策未通过 Schema 校验：{exc}"]


__all__ = [
    "CONTEXT_DIAGNOSTICS_RESERVE",
    "RenderedState",
    "STATE_BUDGET_CHARS",
    "SYSTEM",
    "build_user_message",
    "parse_decision",
    "repair_message",
    "render_state",
    "render_state_document",
]

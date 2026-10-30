"""The prompt contract of the AI Research Intelligence (M2.0).

Three jobs, and nothing else:

* state what the model *is* and the epistemic rules it works under;
* render the current research state as one bounded JSON document;
* turn the model's reply into a `ResearchDecision`, or say why it cannot.

Parsing is strict on purpose. `parse_decision` accepts a bare JSON object
or a fenced ```json block and nothing else -- no prose extraction, no
"find the decision inside the paragraph". A reply that is not a decision
is not a decision; the caller reports `UNPARSEABLE` and may send at most a
bounded number of repair requests. That is the difference between a gate
and a guess.
"""

from __future__ import annotations

import json
from typing import Any

from crea.core.json_parse import parse_json_object
from crea.core.models.research_decision import (
    INTELLIGENCE_PROMPT_VERSION,
    ResearchDecision,
)

#: Hard bound on one rendered state document. A study that has run for a
#: long time must still produce a prompt a model can read; when the budget
#: bites, the OLDEST entries are dropped and the drop is declared in the
#: document itself rather than silent.
STATE_BUDGET_CHARS = 24_000

SYSTEM = f"""你是 CREA 的 AI Research Intelligence——化学科研决策主体。
版本：prompt {INTELLIGENCE_PROMPT_VERSION}。

你的职责是围绕用户的研究目标，自主决定下一步研究行动：选择工具、提出并维护可检验的
假设、根据实际工具结果调整研究方向、在证据不足时如实说明、在需要人回答时提问。

## 必须遵守的科研真实性规则

1. 只能调用「可用工具目录」里注册的工具，参数必须符合该工具的参数 Schema。
   不能执行任意 Python、Shell、SQL，不能调用未注册函数。
2. 工具执行结果（observations）是唯一可以当作事实的内容。你的假设、推断和解读
   永远不是事实。
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
当成事实保存。

只能返回 JSON，不要返回解释性文字、不要返回 Markdown 标题、不要返回多个对象。
`evidence_sufficiency` 只能是 `sufficient` / `partial` / `insufficient`。
`proposed_status` 只能是 `proposed` / `under_review` / `supported` /
`refuted` / `unresolved`；标为 `supported` 必须给出真实的 supporting_evidence_ids。
"""


def render_state(state: Any) -> str:
    """The research state as one bounded JSON document.

    The state object exposes `.as_prompt_dict()`; the budget is applied
    here so a long study degrades by dropping its oldest history and
    saying so, rather than by overflowing the model's context.
    """
    payload = state.as_prompt_dict()
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= STATE_BUDGET_CHARS:
        return text
    trimmed = dict(payload)
    for key in ("decisions", "observations", "session_summaries", "facts"):
        items = trimmed.get(key)
        if not isinstance(items, list) or not items:
            continue
        while items and len(json.dumps(trimmed, ensure_ascii=False,
                                       default=str)) > STATE_BUDGET_CHARS:
            items.pop(0)
        trimmed[key] = items
        if len(json.dumps(trimmed, ensure_ascii=False,
                          default=str)) <= STATE_BUDGET_CHARS:
            break
    trimmed["state_truncated"] = (
        "较早的研究历史因上下文预算被省略；如需请重新查询研究上下文。")
    return json.dumps(trimmed, ensure_ascii=False, default=str)


def build_user_message(state: Any, *, budget_note: str = "") -> str:
    """One user message: the state, the remaining budget, and what to do."""
    parts = [
        "当前研究状态如下：",
        render_state(state),
    ]
    if budget_note:
        parts.append(f"预算提示：{budget_note}")
    parts.append("请给出下一步的唯一一个研究决策（JSON）。")
    return "\n\n".join(parts)


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
    "STATE_BUDGET_CHARS",
    "SYSTEM",
    "build_user_message",
    "parse_decision",
    "repair_message",
    "render_state",
]

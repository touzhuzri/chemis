"""Rendering one AI Research Intelligence run as a readable report (M2.0).

The report's job is to keep the epistemic categories apart on the page,
exactly as the run kept them apart in memory:

* what a tool actually returned (the observations, with their M1.2 kinds);
* what the model *thought* those results meant (the interpretations);
* which hypotheses it proposed, and which of those the study context let
  it mark supported;
* what it could not settle;
* what it refused to claim.

A line that would blur those categories is not written here. An
interpretation is never rendered under a "findings" heading, and an
evidence id appears only where the run actually held it.
"""

from __future__ import annotations

import json
from typing import Any

from crea.core.models.research import TaskStatus

_VERDICT_LABELS = {
    "completed": "研究完成",
    "partial": "部分完成",
    "no_results": "检索完成但无命中（这是结果，不是失败）",
    "input_required": "等待用户回答",
    "failed": "未能产出可用结果",
}

_SUFFICIENCY = {
    "sufficient": "证据充分",
    "partial": "证据部分充分",
    "insufficient": "证据不足",
}


def render_intelligence_report(result: Any) -> str:
    """The run, as markdown a researcher can read."""
    lines: list[str] = []
    add = lines.append

    add("# AI 研究报告")
    add("")
    add(f"- 研究目标：{result.goal}")
    add(f"- 运行模式：{result.mode.value}")
    add(f"- 会话：{result.session_id}"
        + (f"　研究：{result.study_id}" if result.study_id else ""))
    add(f"- 结果：{_VERDICT_LABELS.get(result.outcome, result.outcome)}")
    add(f"- 停止原因：{result.reason}")
    add(f"- 用时：{result.duration_ms / 1000:.1f}s"
        f"（决策 {result.decisions_used} 轮，工具 {result.tool_calls_used} 次，"
        f"模型 {result.model_calls_used} 次）")
    if result.model_provider:
        add(f"- 模型：{result.model_provider}/{result.model_name}"
            f"（prompt {result.prompt_version}）")
    add("")

    if result.awaiting is not None:
        add("## 需要你回答")
        add("")
        add(f"**问题**：{result.awaiting.question}")
        if result.awaiting.why:
            add("")
            add(f"原因：{result.awaiting.why}")
        if result.awaiting.expected_information:
            add("")
            add(f"期望的信息：{result.awaiting.expected_information}")
        if result.awaiting.options:
            add("")
            add("可选项：" + "；".join(result.awaiting.options))
        add("")

    if result.completion is not None:
        completion = result.completion
        add("## 结论")
        add("")
        add(completion.completion_reason)
        add("")
        if completion.achieved_objectives:
            add("**已达成的目标**")
            for item in completion.achieved_objectives:
                add(f"- {item}")
            add("")
        sufficiency = completion.evidence_sufficiency.value
        add("**证据充分性**："
            + _SUFFICIENCY.get(sufficiency, sufficiency))
        add("")
        if completion.unresolved_questions:
            add("**尚未解决的问题**")
            for item in completion.unresolved_questions:
                add(f"- {item}")
            add("")
        if completion.limitations:
            add("**本次研究的限制**")
            for item in completion.limitations:
                add(f"- {item}")
            add("")

    if result.blocked is not None:
        add("## 研究受阻")
        add("")
        add(f"{result.blocked.reason}（blocker: {result.blocked.blocker}）")
        if result.blocked.details:
            add("")
            add(result.blocked.details)
        add("")

    add("## 研究过程")
    add("")
    if not result.steps:
        add("本次运行没有执行任何工具调用。")
        add("")
    else:
        by_step = {o.step_id: o for o in result.observations}
        interpretation = {i.step_id: i for i in result.interpretations}
        for step in result.steps:
            observation = by_step.get(step.step_id)
            add(f"### {step.index + 1}. {step.tool or step.step_id}")
            add("")
            if step.arguments:
                add("参数：`" + json.dumps(step.arguments, ensure_ascii=False,
                                           default=str) + "`")
                add("")
            if step.note:
                add(f"目的：{step.note}")
                add("")
            if observation is None:
                add("未执行。")
                add("")
                continue
            add(f"观察结果：{observation.kind.value}"
                + (f" — {observation.summary}" if observation.summary else ""))
            add("")
            if observation.evidence_ids:
                add("获得证据：" + "、".join(observation.evidence_ids))
                add("")
            reading = interpretation.get(step.step_id)
            if reading is not None and reading.interpretation:
                add(f"**AI 解读**（不是事实）：{reading.interpretation}")
                add("")

    if result.hypothesis_ids:
        add("## 本次提出的假设")
        add("")
        add("以下是假设，不是已验证的事实。")
        add("")
        for hypothesis_id in result.hypothesis_ids:
            add(f"- `{hypothesis_id}`")
        add("")

    if result.evidence_ids:
        add("## 本次研究持有的证据")
        add("")
        for evidence_id in result.evidence_ids:
            add(f"- `{evidence_id}`")
        add("")
    else:
        add("## 证据")
        add("")
        add("本次研究没有获得任何证据 id。")
        add("")

    if result.unresolved_items:
        add("## 未解决的问题")
        add("")
        for item in result.unresolved_items:
            add(f"- {item}")
        add("")

    if result.failures:
        add("## 失败")
        add("")
        for item in result.failures:
            add(f"- {item}")
        add("")

    if result.diagnostics:
        add("## 诊断")
        add("")
        for item in result.diagnostics:
            add(f"- {item}")
        add("")

    if result.status is TaskStatus.FAILED and not result.evidence_ids:
        add("> 本次研究没有产出可引用的证据。报告中的任何陈述都不应被读作"
            "已验证的化学结论。")
        add("")

    return "\n".join(lines).rstrip() + "\n"


__all__ = ["render_intelligence_report"]

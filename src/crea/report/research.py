"""The research report, in the user's language.

A `Finding` is classified by where it came from, and the classification is the
point of the whole exercise:

    computed         RDKit said so; it is arithmetic
    evidence_backed  an Evidence supports it, and the evidence is listed
    inference        the model reasoned past the evidence -- labelled as such
    insufficient     the evidence does not establish it, and the report says so

A report that renders "80 °C" with no provenance is a failure regardless of
how fluent it reads. `render_report` therefore refuses to present a finding as
a fact unless it carries evidence or is marked computed.
"""

from __future__ import annotations

import json
from pathlib import Path

from crea.core.models.evidence import Evidence, EvidenceType, Locator
from crea.core.models.identity import (
    ChemicalIdentityResolution,
    StructureResolutionStatus,
)
from crea.core.models.reaction import YieldType
from crea.core.models.research import (
    Finding,
    ResearchReport,
    ResearchStep,
    SourceRef,
    TaskStatus,
)
from crea.core.models.route import StepStatus
from crea.core.models.session import ResearchSession, WorkflowStatus
from crea.core.provenance import utc_now_iso

KIND_LABELS = {
    "computed": "计算结果",
    "evidence_backed": "有证据支持",
    "inference": "推断",
    "insufficient": "证据不足",
    "fact": "事实",
}

INSUFFICIENT = "现有证据不足以确认该结论。"


def unsupported_findings_marker(report: ResearchReport) -> str:
    """The line that appears when a finding asserts more than it can show."""
    return "注意：有结论没有证据支撑，不应作为事实使用。"

_KIND_ORDER = ("computed", "evidence_backed", "inference", "insufficient", "fact")


def build_findings(claims, evidence, computed_notes: list[str] | None = None) -> list[Finding]:
    """Turn claims into findings, and refuse to overstate what is not there.

    A claim with no resolvable evidence is not silently promoted to a fact; it
    comes back as `insufficient`, and the report says the evidence does not
    settle it.
    """
    by_id = {e.evidence_id: e for e in evidence}
    findings: list[Finding] = []

    for note in computed_notes or []:
        findings.append(Finding(text=note, kind="computed", note="RDKit 计算值"))

    for claim in claims:
        resolved = [by_id[r] for r in claim.evidence_refs if r in by_id]
        missing = [r for r in claim.evidence_refs if r not in by_id]
        if resolved:
            findings.append(Finding(
                text=claim.text,
                kind="evidence_backed",
                claim=claim,
                evidence_refs=[e.evidence_id for e in resolved],
                note=("" if not missing
                      else f"未能解析的引用: {', '.join(missing)}"),
            ))
        else:
            findings.append(Finding(
                text=claim.text,
                kind="insufficient",
                claim=claim,
                evidence_refs=[],
                note=INSUFFICIENT,
            ))

    findings.sort(key=lambda f: _KIND_ORDER.index(f.kind)
                  if f.kind in _KIND_ORDER else len(_KIND_ORDER))
    return findings


def render_report(*, question, interpretation, claims, evidence, sources,
                  unresolved, steps, tool_calls, status,
                  computed_notes=None, reaction_details=None, routes=None,
                  route_notes=None, route_evidence=None, reactions=None,
                  reaction_links=None) -> ResearchReport:
    findings = build_findings(claims, evidence, computed_notes)
    report = ResearchReport(
        question=question,
        interpretation=interpretation or "",
        findings=findings,
        evidence=list(evidence),
        reaction_details=list(reaction_details or []),
        routes=list(routes or []),
        route_notes=list(route_notes or []),
        route_evidence=list(route_evidence or []),
        reactions=list(reactions or []),
        reaction_links=list(reaction_links or []),
        sources=list(sources),
        unresolved_items=list(unresolved),
        trace_summary=_trace_summary(steps, tool_calls),
        generated_at=utc_now_iso(),
        status=status,
    )
    return report


#: A field the paper did not mention is reported as unstated. "Atmospheric
#: pressure" would be a guess wearing a lab coat.
_NOT_STATED = "未在提供的 Experimental 证据中说明"

_ROLE_LABELS = {
    "reactant": "底物", "reagent": "试剂", "catalyst": "催化剂",
    "ligand": "配体", "solvent": "溶剂", "base": "碱",
    "additive": "添加剂", "product": "产物", "unknown": "未确定角色",
}


def _component_lines(title, components, amount_of=None):
    if not components:
        return [f"{title}：{_NOT_STATED}"]
    out = [f"{title}："]
    for item in components:
        amount = ""
        if item.amount is not None and item.amount.value is not None:
            amount = f"（{item.amount.value:g} {item.amount.unit}）"
        out.append(f"  - {item.name}{amount}"
                   + (f"  [{', '.join(item.evidence_refs)}]"
                      if item.evidence_refs else ""))
    return out


_SUPPORT_LABEL = {
    "no_support": "未找到",
    "partial_support": "部分支持",
    "structural_support": "结构匹配",
    "literature_support": "文献支持",
    "experimental_support": "实验条件支持",
    "user_document_support": "用户文档支持",
}

_STATUS_LABEL = {
    "candidate": "候选",
    "literature_supported": "有文献支持",
    "experimentally_supported": "有实验条件支持",
    "unverified": "未验证",
    "conflicting": "存在冲突",
}

_STOCK_LABEL = {
    "provider_stock_list": "provider stock 列表视为可购买",
    "not_in_stock_list": "不在 provider stock 列表",
    "unknown": "未知",
}

#: The reaction-level ladder, in Chinese. The order of the dictionary is the
#: order of the ladder; it is not a scale of how good the chemistry is.
_EVIDENCE_LEVEL_LABEL = {
    "retrosynthesis_candidate": "逆合成工具候选（无证据）",
    "literature_discovery": "文献检索发现（无反应级证据）",
    "literature_reaction_support": "文献反应级支持",
    "experimental_reaction_support": "文献实验记录支持",
    "user_document_experimental_support": "用户文档实验支持",
}

_MATCH_LABEL = {
    "exact": "EXACT（双侧完全匹配）",
    "partial": "PARTIAL（单侧匹配）",
    "transformation": "TRANSFORMATION（仅相关转化）",
    "no_match": "NO_MATCH（已比较但不匹配）",
    "discovery_only": "DISCOVERY_ONLY（记录无可比较的反应结构）",
    "discovery": "DISCOVERY（未找到相关反应）",
}

#: Words the route-evidence section must never contain. It describes evidence
#: and refuses to estimate outcomes; a sentence promising a yield or naming a
#: winner would be a claim this project cannot support from anything it holds.
_EVIDENCE_FORBIDDEN = (
    "成功率", "把握", "很可能", "推荐", "最佳", "最优", "首选",
    "confidence", "probability", "success rate", "recommended",
)


def _mol(molecule) -> str:
    return (molecule.canonical_smiles or molecule.smiles
            or molecule.name or molecule.molecule_id)


def _render_routes(add, report) -> None:
    add("## 候选合成路线")
    add("")
    add(f"目标：{_mol(report.routes[0].target)}")
    add("")
    for index, route in enumerate(report.routes, start=1):
        add(f"### 路线 {index}")
        add("")
        add(f"来源：{route.source}")
        # the provider's own heuristic, named as one. Never a confidence.
        if route.score is not None:
            add(f"{route.score_source.value} score：{route.score:g}")
        else:
            add("score：未提供")
        add("注意：以下步骤由逆合成工具提出，未自动视为已验证。")
        for warning in route.warnings:
            add(f"> {warning}")
        add("")
        for step in route.steps:
            add(f"#### 第 {step.step_index} 步")
            add("")
            add("产物：" + (" + ".join(_mol(m) for m in step.products) or "未说明"))
            add("底物：" + (" + ".join(_mol(m) for m in step.reactants) or "未说明"))
            if step.transformation and step.transformation.transformation_class:
                add(f"反应类型：{step.transformation.transformation_class}"
                    f"（来源 {step.transformation.transformation_class_source}）")
            add(f"状态：{_STATUS_LABEL.get(step.status.value, step.status.value)}")
            add("文献支持："
                + _SUPPORT_LABEL.get(step.support_level.value, step.support_level.value))
            if step.stock_assumptions:
                add("stock 假设：")
                for smiles, kind in step.stock_assumptions.items():
                    add(f"  - {smiles}: "
                        f"{_STOCK_LABEL.get(kind.value, kind.value)}")
            if step.evidence_refs:
                add("证据：" + ", ".join(step.evidence_refs))
            if step.reaction_record_ids:
                add("实验记录：" + ", ".join(step.reaction_record_ids))
            for warning in step.warnings:
                add(f"> {warning}")
            add("")
    _render_route_evidence(add, report)
    if len(report.routes) > 1:
        _render_route_comparison(add, report)

    if report.route_notes:
        add("### 未解决问题")
        add("")
        for note in dict.fromkeys(report.route_notes):
            add(f"- {note}")
        add("")


def _render_route_evidence(add, report) -> None:
    """The reaction-level evidence chain, per route and per step.

    Rendered as its own section rather than folded into the route listing,
    because it answers a different question. The route listing says what the
    provider proposed and what the structural validator made of it; this says
    how far the *evidence* climbed, on which evidence, and what is still
    missing. Collapsing the two is how a candidate step ends up described as
    verified.
    """
    chains = list(getattr(report, "route_evidence", []) or [])
    if not chains:
        return

    add("## 路线证据链")
    add("")
    add("本节只呈现证据等级与依据，不给出成功率、不排名、不推荐路线。")
    add("")

    records_by_id = {r.reaction_id: r for r in report.reaction_details}

    for index, chain in enumerate(chains, start=1):
        add(f"### 路线 {index} · 证据链")
        add("")
        if chain.provider_score is not None:
            add(f"{chain.score_source} score：{chain.provider_score:g}"
                "（该引擎自身的搜索启发式，不是置信度）")
        for warning in chain.warnings:
            add(f"> {warning}")
        add("")

        for step in chain.steps:
            add(f"#### 第 {step.step_index} 步"
                if step.step_index else f"#### {step.step_id}")
            add("")
            add(f"路线状态：{_STATUS_LABEL.get(step.candidate_status.value, step.candidate_status.value)}")
            add(f"证据等级：{_EVIDENCE_LEVEL_LABEL.get(step.evidence_level.value, step.evidence_level.value)}"
                f"（{step.evidence_level.value}）")
            add(f"匹配：{_MATCH_LABEL.get(step.match_type.value, step.match_type.value)}")
            if step.doi:
                add("文献：" + "；".join(step.doi))
            if step.document_ids:
                add("用户文档：" + "；".join(step.document_ids))
            if step.evidence_refs:
                add("证据 ID：" + "；".join(step.evidence_refs))
            if step.support_reason:
                add(f"依据：{step.support_reason}")
            if step.support_provenance:
                add(f"证据来源：{step.support_provenance}")

            for reaction_id in step.experimental_support:
                record = records_by_id.get(reaction_id)
                add(f"实验条件（{reaction_id}）：")
                if record is None:
                    add(f"  - {_NOT_STATED}")
                    continue
                conditions = record.conditions
                add(f"  - 温度：{_value_or_unstated(conditions.temperature)}")
                add(f"  - 时间：{_value_or_unstated(conditions.time)}")
                add(f"  - 收率：{_value_or_unstated(conditions.yield_value)}")
                if conditions.atmosphere:
                    add(f"  - 气氛：{conditions.atmosphere}")

            if step.unlinked_records:
                # Shown, not hidden. A procedure somebody read is a real
                # finding; it is simply not shown to be *this* step, and
                # dropping it would make the gap look like nothing was read.
                add("已读取但未作为本步骤支持的实验记录："
                    + "；".join(step.unlinked_records))
            if step.unresolved_evidence_refs:
                add("未能解析的证据 ID：" + "；".join(step.unresolved_evidence_refs))
            for warning in step.warnings:
                add(f"> {warning}")
            add("")

        for note in chain.unresolved:
            add(f"> {note}")
        if chain.unresolved:
            add("")


def _value_or_unstated(quantity) -> str:
    if not quantity.stated or quantity.value is None:
        return _NOT_STATED
    return quantity.render()


#: Stated in the report, and excluded from the verdict-word check -- it names
#: the words in order to deny them.
_DISCLAIMER = "  以上为各路线的事实差异。本阶段不做综合评分，也不给出推荐。"

_COMPARISON_ROWS = (
    ("步数", lambda r: str(len(r.steps))),
    ("provider score", lambda r: f"{r.score:g}" if r.score is not None else "未提供"),
    ("score 来源", lambda r: r.score_source.value),
    ("有文献支持的步数",
     lambda r: str(sum(1 for s in r.steps
                        if s.support_level.value in ("literature_support",
                                                     "structural_support",
                                                     "experimental_support",
                                                     "user_document_support")))),
    ("有实验条件的步数",
     lambda r: str(sum(1 for s in r.steps
                        if s.support_level.value in ("experimental_support",
                                                     "user_document_support")))),
    ("未支持的步数",
     lambda r: str(sum(1 for s in r.steps if s.support_level.value == "no_support"))),
    ("部分支持的步数",
     lambda r: str(sum(1 for s in r.steps
                        if s.support_level.value == "partial_support"))),
    ("证据条数", lambda r: str(sum(len(s.evidence_refs) for s in r.steps))),
)

#: Words a comparison must never contain. A route is not a product.
_FORBIDDEN_VERDICTS = ("最好", "更可靠", "推荐", "胜出", "最佳", "首选")


def _render_route_comparison(add, report) -> None:
    """Side by side, facts only.

    There is no combined score and no winner column, because ranking two
    candidate routes is a different decision from describing them, and doing it
    here would produce a verdict nothing in this project earned.
    """
    add("### 路线对比")
    add("")
    header = "  | 项目 | " + " | ".join(f"路线 {i}" for i in
                                        range(1, len(report.routes) + 1)) + "  |"
    add(header)
    add("  |" + "---|" * (len(report.routes) + 1))
    for label, getter in _COMPARISON_ROWS:
        cells = " | ".join(getter(route) for route in report.routes)
        add(f"  | {label} | {cells} |")
    add("  |")
    add(_DISCLAIMER)
    add("")


def _quantity_line(label, quantity, extra=""):
    if not quantity.stated or quantity.value is None:
        return f"{label}：{_NOT_STATED}"
    refs = f"  [{', '.join(quantity.evidence_refs)}]" if quantity.evidence_refs else ""
    return f"{label}：{quantity.render()}{extra}{refs}"


def _render_reaction(add, record) -> None:
    c = record.conditions
    add(f"### {record.reaction_description or '实验方案'}")
    add("")
    for title, items in (("底物", c.reactants), ("试剂", c.reagents),
                         ("催化剂", c.catalysts), ("配体", c.ligands),
                         ("溶剂", c.solvents),
                         # Named in the procedure, role not established. Shown
                         # rather than dropped: the substance is in the paper,
                         # and hiding it would understate what was found.
                         ("已提及但角色未确定", c.unassigned)):
        for line in _component_lines(title, items):
            add(line)
    add(_quantity_line("温度", c.temperature))
    add(_quantity_line("时间", c.time))
    add(_quantity_line("压力", c.pressure))
    if c.yield_value.stated and c.yield_value.value is not None:
        ytype = {"unknown": "未说明类型", "isolated": "分离收率",
                 "crude": "粗产物收率", "conversion": "转化率",
                 "selectivity": "选择性", "yield": "收率"}[c.yield_type.value]
        refs = (f"  [{', '.join(c.yield_value.evidence_refs)}]"
                if c.yield_value.evidence_refs else "")
        add(f"收率：{c.yield_value.render()}（{ytype}）{refs}")
    else:
        add(f"收率：{_NOT_STATED}")
    add(f"气氛：{c.atmosphere}" if c.atmosphere else f"气氛：{_NOT_STATED}")
    if c.workup:
        add(f"后处理：{c.workup}")
    if c.purification:
        add(f"纯化：{c.purification}")
    for conflict in record.conflicts:
        add(f"> 冲突：{conflict.field} 在不同证据中不一致"
            f"（{', '.join(f'{k}: {v}' for k, v in conflict.values.items())}）"
            f"；未替你选择其中之一。")
    if record.rejected:
        add("")
        add("以下候选字段未通过校验，已丢弃：")
        for item in record.rejected:
            add(f"  - {item.field}：{item.reason}")
    add("")


def _trace_summary(steps: list[ResearchStep], tool_calls: int) -> str:
    parts = [f"共 {len(steps)} 步，其中 {tool_calls} 次工具调用"]
    tools = [s.tool for s in steps if s.tool and s.result_status]
    if tools:
        parts.append("调用工具：" + "、".join(dict.fromkeys(tools)))
    failed = [s for s in steps if s.result_status
              and s.result_status.value in ("error", "rejected", "unavailable")]
    if failed:
        parts.append(f"{len(failed)} 步未成功")
    return "；".join(parts)


# --- human-readable output (Chinese) ----------------------------------------


def format_report(report: ResearchReport) -> str:
    out: list[str] = []
    add = out.append

    add("# 研究报告")
    add("")
    add(f"**问题**：{report.question}")
    add(f"**状态**：{_status_label(report.status)}")
    add(f"**生成时间**：{report.generated_at}")
    if report.interpretation:
        add("")
        add(f"**问题理解**：{report.interpretation}")
    add("")

    if report.findings:
        add("## 结论")
        add("")
        for index, finding in enumerate(report.findings, start=1):
            label = KIND_LABELS.get(finding.kind, finding.kind)
            add(f"{index}. [{label}] {finding.text}")
            if finding.evidence_refs:
                add(f"   - 证据：{', '.join(finding.evidence_refs[:4])}"
                    + ("…" if len(finding.evidence_refs) > 4 else ""))
            if finding.note:
                add(f"   - 说明：{finding.note}")
        add("")

    if report.routes:
        _render_routes(add, report)

    if report.reaction_details:
        add("## 实验条件")
        add("")
        for record in report.reaction_details:
            _render_reaction(add, record)
        add("")

    if report.evidence:
        add("## 证据")
        add("")
        for item in report.evidence:
            where = []
            if item.locator.page_start:
                where.append(f"第 {item.locator.page_start} 页")
            if item.locator.section:
                where.append(f"章节「{item.locator.section}」")
            elif item.locator.field:
                where.append(f"字段 {item.locator.field}")
            quote = item.raw_quote.replace("\n", " ").strip()
            if len(quote) > 220:
                quote = quote[:220] + "…"
            add(f"- `{item.evidence_id}` "
                f"[{item.evidence_type.value} / {item.strength.value}] "
                f"来源 {item.source}:{item.source_id} "
                f"（{'，'.join(where) or '未定位'}）")
            add(f"  > {quote}")
        add("")

    if report.sources:
        add("## 来源")
        add("")
        for source in report.sources:
            kind = "文献" if source.kind == "paper" else "本地文档"
            label = f" — {source.label}" if source.label else ""
            add(f"- [{kind}] {source.identifier}{label}"
                + (f"  {source.url}" if source.url else ""))
        add("")

    if report.unresolved_items:
        add("## 未解决事项")
        add("")
        for item in report.unresolved_items:
            add(f"- {item}")
        add("")

    unsupported = report.unsupported_findings()
    if unsupported:
        add("> 注意：以下结论没有证据支撑，不应作为事实使用。")
        add("")

    add("## 执行轨迹")
    add("")
    add(report.trace_summary)
    add("")
    return "\n".join(out)


def _status_label(status: TaskStatus) -> str:
    return {
        TaskStatus.PENDING: "待开始",
        TaskStatus.RUNNING: "进行中",
        TaskStatus.COMPLETED: "已完成",
        TaskStatus.PARTIAL: "部分完成",
        TaskStatus.FAILED: "失败",
    }.get(status, status.value)


def write_report(report: ResearchReport, path) -> Path:
    """Write the machine-readable report next to the human-readable one."""
    target = Path(path)
    target.write_text(json.dumps(report.model_dump(mode="json"),
                                ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return target


# --- Research Report v2 (Phase 3.7 §5) --------------------------------------
#
# A second renderer over the same models, organised the way a reader asks
# questions: what was asked, what structure was established, what was
# computed, what was merely found, what was actually evidenced, which routes
# exist and how far each step got, where every conclusion came from, and what
# is missing. `render_report` above is untouched; this function reads the
# same `ResearchReport` and adds two optional inputs the report model does
# not carry: the run's `ResearchSession` and its `ChemicalIdentityResolution`.
#
# Two rules this renderer enforces rather than hopes for:
#
# * **Status is never upgraded here.** The label is the weakest of what the
#   workflow and the task actually said. A report layer that declared
#   COMPLETED because nothing errored would be inventing a verdict.
# * **Provenance is never dropped for tidiness.** Every important finding
#   keeps its Evidence ID, source, locator and quote.

#: Evidence types that describe literature rather than a procedure.
_LIT_EVIDENCE_TYPES = (
    EvidenceType.METADATA, EvidenceType.ABSTRACT,
    EvidenceType.STRUCTURED_DATABASE,
)
#: Evidence types that carry actual text from a work or a user's file.
_FULLTEXT_TYPES = (EvidenceType.FULLTEXT, EvidenceType.EXPERIMENTAL,
                   EvidenceType.USER_PROVIDED_DOCUMENT)
#: Sources that are literature databases by name.
_LIT_SOURCE_NAMES = ("openalex", "crossref", "unpaywall")


def _locator_label(locator: Locator) -> str:
    """`page 1 Experimental` -- where a reader would look for the quote."""
    page = locator.page if locator.page is not None else locator.page_start
    parts: list[str] = []
    if page is not None:
        parts.append(f"page {page}")
    if locator.section:
        parts.append(locator.section)
    elif locator.field:
        parts.append(locator.field)
    return " ".join(parts) if parts else "unlocated"


def _is_literature_evidence(item: Evidence) -> bool:
    return (item.evidence_type in _LIT_EVIDENCE_TYPES
            or item.source in _LIT_SOURCE_NAMES)


def _v2_status(report: ResearchReport,
               session: "ResearchSession | None") -> str:
    """The report's status is what the workflow said -- the weaker claim wins.

    COMPLETED is displayed only when both the session and the task said
    COMPLETED. A completed session over an incomplete task is a contradiction
    and the weaker claim is displayed. The renderer derives nothing from "no
    errors were recorded"; a run with no verdict is not a finished run.
    """
    if session is not None:
        if session.status is WorkflowStatus.PARTIAL:
            return "PARTIAL"
        if session.status is WorkflowStatus.COMPLETED:
            return ("COMPLETED" if report.status is TaskStatus.COMPLETED
                    else report.status.value.upper())
        return session.status.value.upper()
    return report.status.value.upper()


def _stopped_at(session: "ResearchSession | None") -> str:
    """Where the research stopped, for a report that is not complete.

    The steps come from the session, or -- when the run was read back from
    the database and its session-level `steps` were not persisted -- from the
    task the session carries, which is. And "stopped at" names the last step
    that actually *ran*: a recorded-but-skipped step is not where a run
    stopped, and reporting one is how a run that completed literature ended
    up saying "before any step completed" while the report listed a success.
    """
    if session is None:
        return "not recorded (no session attached)"
    if not session.status.is_terminal:
        return f"phase {session.status.value}"
    steps = list(session.steps) or list(getattr(session.task, "steps", []) or [])
    ran = [s for s in steps if s.result_status is not None]
    if ran:
        last = ran[-1]
        return f"step {last.index} ({last.tool or last.action.value})"
    return "before any step completed"


def _render_identity_v2(add, identity: "ChemicalIdentityResolution | None") -> None:
    add("## 2. Chemical Identity")
    add("")
    if identity is None:
        add("Identity unresolved")
        add("")
        add("未提供结构输入（本次运行没有可报告的身份解析结果）；"
            "不猜测结构。")
        add("")
        return
    if identity.status is not StructureResolutionStatus.RESOLVED:
        add("Identity unresolved")
        add("")
        add(f"- resolution status: {identity.status.value}")
        if identity.reason:
            add(f"- reason: {identity.reason}")
        add(f"- input: {identity.input.kind.value} `{identity.input.value}`")
        add(f"- candidates: {len(identity.candidates)}（无一被选中）")
        add("- 未解析即不给出结构；以上候选均不是结论。")
        add("")
        return

    selected = identity.selected
    add(f"- input: {identity.input.kind.value} `{identity.input.value}`")
    name = selected.candidate_name or selected.input_name or ""
    if name:
        add(f"- resolved name: {name}")
    add(f"- SMILES: `{selected.canonical_smiles}`")
    if selected.inchi:
        add(f"- InChI: `{selected.inchi}`")
    if selected.inchikey:
        add(f"- InChIKey: `{selected.inchikey}`")
    add(f"- identity source: {selected.source} {selected.source_id}".rstrip())
    add(f"- resolution status: {identity.status.value}")
    add(f"- identity basis: {identity.selected_basis.value}")
    add("")


def _render_molecular_v2(add, report: ResearchReport) -> None:
    add("## 3. Molecular Analysis")
    add("")
    computed = [f for f in report.findings if f.kind == "computed"]
    if not computed:
        add("(no computed analysis was run)")
        add("")
        return
    add("以下为计算结果，不是文献证据：")
    add("")
    for finding in computed:
        add(f"- [Computed] {finding.text}")
    add("")


def _render_literature_v2(add, report: ResearchReport) -> None:
    add("## 4. Literature")
    add("")

    add("### Literature Discovery")
    add("")
    papers = [s for s in report.sources if s.kind == "paper"]
    if not papers:
        add("(no literature result)")
        add("")
    else:
        for source in papers:
            title = source.label or source.identifier
            bits = [f"- [Discovery] {title}"]
            if source.doi:
                bits.append(f"DOI: {source.doi}")
            bits.append(f"source: {source.identifier}")
            add(" | ".join(bits))
            if source.url:
                add(f"  {source.url}")
        add("")
        add("以上仅为 Discovery：检索到的条目，不是证据，也不构成实验支持。")
        add("")

    add("### Evidence-backed Literature")
    add("")
    lit_evidence = [e for e in report.evidence if _is_literature_evidence(e)]
    if not lit_evidence:
        add("(none)")
        add("")
        add("无真实 Evidence 支撑的文献条目；上方 Discovery 不进入本节。")
        add("")
        return
    for item in lit_evidence:
        title = item.doi or item.paper_id or item.source_id
        add(f"- [Evidence-backed] {title}")
        add(f"  - Evidence ID: `{item.evidence_id}`")
        add(f"  - strength: {item.strength.value}")
        add(f"  - Source: {item.source} / Source ID: {item.source_id}")
        add(f"  - Locator: {_locator_label(item.locator)}")
        quote = item.raw_quote.replace("\n", " ").strip()
        if len(quote) > 220:
            quote = quote[:220] + "…"
        add(f"  - Quote: “{quote}”")
        if item.evidence_type in _LIT_EVIDENCE_TYPES:
            add("  - 注意：metadata/abstract 只是发现级证据，不能升级为实验支持。")
    add("")


def _evidence_cell(refs: list[str], by_id: dict) -> str:
    if not refs:
        return "—"
    cells = []
    for ref in refs:
        item = by_id.get(ref)
        if item is None:
            cells.append(f"`{ref}`（unresolved）")
        else:
            cells.append(_locator_label(item.locator))
    return "；".join(cells)


def _render_experimental_v2(add, report: ResearchReport) -> None:
    add("## 5. Experimental Evidence")
    add("")
    if not report.reaction_details:
        add("(no experimental evidence)")
        add("")
        return
    by_id = {e.evidence_id: e for e in report.evidence}
    for record in report.reaction_details:
        c = record.conditions
        add(f"### Record {record.reaction_id}"
            + (f"（{record.document_id}）" if record.document_id else ""))
        add("")
        add("| Parameter | Value | Evidence |")
        add("|---|---|---|")

        def row(label: str, value: str, refs: list[str]) -> None:
            add(f"| {label} | {value} | {_evidence_cell(refs, by_id)} |")

        def quantity_row(label: str, quantity) -> None:
            value = quantity.render() if quantity.stated else "not stated"
            row(label, value, list(quantity.evidence_refs))

        quantity_row("Temperature", c.temperature)
        quantity_row("Time", c.time)
        quantity_row("Pressure", c.pressure)
        quantity_row("Concentration", c.concentration)
        if c.yield_value.stated and c.yield_value.value is not None:
            suffix = ("" if c.yield_type is YieldType.UNKNOWN
                      else f" ({c.yield_type.value})")
            row("Yield", c.yield_value.render() + suffix,
                list(c.yield_value.evidence_refs))
        else:
            row("Yield", "not stated", list(c.yield_value.evidence_refs))
        row("Atmosphere", c.atmosphere or "not stated",
            list(c.atmosphere_evidence_refs))
        row("Workup", c.workup or "not stated", list(c.workup_evidence_refs))
        row("Purification", c.purification or "not stated",
            list(c.purification_evidence_refs))
        add("")
        for conflict in record.conflicts:
            add(f"> 冲突：{conflict.field} 在不同证据中不一致；未替你选择。")
        add("")


def _render_reactions_v2(add, report: ResearchReport) -> None:
    """The reaction knowledge layer, labelled as itself (§71).

    Every line carries what kind of statement it is: [REACTION] a structured
    reaction, [EXPERIMENTAL] its experimental support, [UNRESOLVED] a
    component nobody established, [PARTIAL] a reaction with gaps, [DISCOVERY]
    a paper found with no experimental text behind it. Nothing here is
    promoted by being listed: the labels are the same facts the layers below
    already established, in the order a reader asks for them.
    """
    add("## Reactions")
    add("")
    reactions = sorted(report.reactions or [],
                       key=lambda r: (r.paper_scope(), r.reaction_id))
    links_by_step: dict[str, list] = {}
    for link in report.reaction_links or []:
        links_by_step.setdefault(link.step_id, []).append(link)

    if not reactions:
        if report.sources:
            add("该文献被发现，但当前没有足够实验文本建立 Reaction。")
            add("")
            add("[DISCOVERY] 文献检索有发现；无实验文本，不建立 Reaction。")
        else:
            add("(no reactions were established)")
        add("")
    else:
        add(f"共 {len(reactions)} 个 Reaction，按 paper / reaction id 排序。")
        add("")
        for reaction in reactions:
            label = "[PARTIAL]" if reaction.status.value == "partial" else "[REACTION]"
            add(f"### {label} `{reaction.reaction_id}`")
            add("")
            add(f"- status: {reaction.status.value}")
            where = []
            if reaction.paper_id:
                where.append(f"paper {reaction.paper_id}")
            if reaction.doi:
                where.append(f"DOI {reaction.doi}")
            if reaction.document_id:
                where.append(f"document {reaction.document_id}")
            if reaction.record_id:
                where.append(f"record {reaction.record_id}")
            if where:
                add("- source: " + " / ".join(where))
            for title, components in (("reactants", reaction.reactants),
                                      ("products", reaction.products),
                                      ("reagents", reaction.reagents),
                                      ("catalysts", reaction.catalysts),
                                      ("solvents", reaction.solvents),
                                      ("other", reaction.unassigned)):
                if not components:
                    continue
                rendered = []
                for component in components:
                    if component.entity_id:
                        rendered.append(
                            f"{component.name} → {component.entity_id}")
                    else:
                        rendered.append(f"[UNRESOLVED] {component.name}")
                add(f"- {title}: " + "；".join(rendered))
            if reaction.evidence_refs:
                kind = ("[EXPERIMENTAL]"
                        if reaction.status.value == "experimentally_supported"
                        else "[EVIDENCE]")
                add(f"- {kind} evidence: " + "；".join(reaction.evidence_refs))
            if reaction.unresolved_evidence_refs:
                add("- [UNRESOLVED] evidence: "
                    + "；".join(reaction.unresolved_evidence_refs))
            for note in reaction.notes:
                add(f"- [PARTIAL] {note}")
            add("")

    if links_by_step:
        add("RouteStep ↔ Reaction：")
        add("")
        for step_id in sorted(links_by_step):
            for link in links_by_step[step_id]:
                mark = ("exact match" if not link.screening_only
                        else "screening only, not support")
                add(f"- step `{step_id}` → `{link.reaction_id}`"
                    f"（{link.match_type.value}；{mark}）")
        add("")


def _render_retrosynthesis_v2(add, report: ResearchReport) -> None:
    add("## 6. Retrosynthesis")
    add("")
    if not report.routes:
        add("(retrosynthesis unavailable)")
        add("")
    else:
        add("候选路线由逆合成工具提出；本报告不排名、不推荐、不预测成功率。")
        add("")
        chains = {c.route_id: c for c in report.route_evidence}
        for index, route in enumerate(report.routes, start=1):
            add(f"### Route {index} (`{route.route_id}`)")
            add("")
            add(f"Source: {route.source}")
            add(f"Target: {_mol(route.target)}")
            add("")
            chain = chains.get(route.route_id)
            for step in route.steps:
                add(f"#### Step {step.step_index}")
                add("")
                reactants = " + ".join(_mol(m) for m in step.reactants) or "?"
                products = " + ".join(_mol(m) for m in step.products) or "?"
                reaction = f"{reactants} → {products}"
                if (step.transformation
                        and step.transformation.transformation_class
                        and step.transformation.transformation_class != "UNKNOWN"):
                    reaction += (f"（{step.transformation.transformation_class}）")
                add(f"Reaction: {reaction}")
                has_evidence = bool(step.evidence_refs
                                    or step.reaction_record_ids
                                    or step.paper_ids)
                if step.status is StepStatus.CANDIDATE and not has_evidence:
                    add("Evidence: Candidate only")
                else:
                    add(f"Status: {step.status.value}")
                    add(f"Support: {step.support_level.value}")
                    if step.evidence_refs:
                        add("Evidence: " + "；".join(f"`{r}`" for r in step.evidence_refs))
                    if step.reaction_record_ids:
                        add("Reaction records: "
                            + "；".join(f"`{r}`" for r in step.reaction_record_ids))
                    if step.paper_ids:
                        add("Papers: " + "；".join(step.paper_ids))
                    if not has_evidence:
                        add("Evidence: (refs not listed)")
                if chain is not None:
                    chain_step = chain.step(step.step_id)
                    if chain_step is not None:
                        add(f"Evidence level: {chain_step.evidence_level.value}"
                            f"（match: {chain_step.match_type.value}）")
                        if chain_step.support_reason:
                            add(f"Basis: {chain_step.support_reason}")
                        if chain_step.unresolved_evidence_refs:
                            add("未解析的证据 ID："
                                + "；".join(chain_step.unresolved_evidence_refs))
                for warning in step.warnings:
                    add(f"> {warning}")
                add("")
    for note in dict.fromkeys(report.route_notes):
        add(f"> {note}")
    if report.route_notes:
        add("")


def _render_provenance_v2(add, report: ResearchReport) -> None:
    add("## 7. Evidence and Provenance")
    add("")
    by_id = {e.evidence_id: e for e in report.evidence}
    important = [f for f in report.findings
                 if f.kind in ("fact", "evidence_backed", "inference",
                               "insufficient")]
    if not important:
        add("(no findings to source)")
        add("")
        return
    for finding in important:
        add(f"- Finding [{finding.kind}]: {finding.text}")
        if not finding.evidence_refs:
            add("  - Evidence: none — 该结论没有证据支撑，不应作为事实使用。")
        for ref in finding.evidence_refs:
            item = by_id.get(ref)
            if item is None:
                add(f"  - Evidence ID: `{ref}`（unresolved）")
                continue
            add(f"  - Evidence ID: `{item.evidence_id}`")
            add(f"    - Source: {item.source} / Source ID: {item.source_id}")
            add(f"    - Locator: {_locator_label(item.locator)}")
            quote = item.raw_quote.replace("\n", " ").strip()
            if len(quote) > 220:
                quote = quote[:220] + "…"
            add(f"    - Quote: “{quote}”")
        if finding.note:
            add(f"  - note: {finding.note}")
    add("")


def _limitations_v2(report: ResearchReport,
                    session: "ResearchSession | None",
                    identity: "ChemicalIdentityResolution | None") -> list[str]:
    """The gaps, derived from what the run holds. Never padded, never hidden."""
    items: list[str] = []

    if (identity is None
            or identity.status is not StructureResolutionStatus.RESOLVED):
        items.append("identity unresolved: 未建立确定结构")

    papers = [s for s in report.sources if s.kind == "paper"]
    if not papers:
        items.append("no literature result")

    lit_evidence = [e for e in report.evidence if _is_literature_evidence(e)]
    if papers and (not lit_evidence or all(
            e.evidence_type in (EvidenceType.METADATA, EvidenceType.ABSTRACT)
            for e in lit_evidence)):
        items.append("metadata only: 文献仅为发现级，不能升级为实验证据")

    if not any(e.evidence_type in _FULLTEXT_TYPES for e in report.evidence):
        items.append("no full text")

    experimental = (report.reaction_details
                    or any(e.evidence_type in (EvidenceType.EXPERIMENTAL,
                                               EvidenceType.USER_PROVIDED_DOCUMENT)
                           for e in report.evidence))
    if not experimental:
        items.append("no experimental evidence")

    # -- the reaction knowledge layer (Phase 3.10) -------------------------
    if not report.reactions:
        items.append("no reactions established"
                     + (": 文献被发现但没有足够实验文本建立 Reaction"
                        if papers else ""))
    else:
        if any(r.status.value == "partial" for r in report.reactions):
            items.append("reaction partially resolved: 部分组分或字段未解析")
        if not any(r.status.value == "experimentally_supported"
                   for r in report.reactions):
            items.append("no reaction is experimentally supported")
    if report.routes and report.reaction_links is not None:
        linked = {link.step_id for link in report.reaction_links
                  if not link.screening_only}
        route_steps = [s for r in report.routes for s in r.steps]
        if route_steps and not linked:
            items.append("no route step is linked to a literature reaction")

    if not report.routes:
        items.append("retrosynthesis unavailable")
    else:
        all_steps = [s for r in report.routes for s in r.steps]
        if all_steps and all(s.status is StepStatus.CANDIDATE
                             for s in all_steps):
            items.append("retrosynthesis candidate only")
        if not any(s.status is StepStatus.EXPERIMENTALLY_SUPPORTED
                   for s in all_steps):
            items.append("route not experimentally validated")

    if any(item.startswith("LLM 未完成") or item.startswith("LLM 未返回")
           for item in report.unresolved_items):
        items.append("LLM stopped early")

    if _v2_status(report, session) == "PARTIAL":
        items.append("workflow PARTIAL")

    return items


def render_research_report(
    report: ResearchReport,
    *,
    session: "ResearchSession | None" = None,
    identity: "ChemicalIdentityResolution | None" = None,
) -> str:
    """Render one research run as Markdown.

    `render_report` keeps producing the Chinese report for existing callers;
    this is the v2 structure (§5): research task, chemical identity,
    molecular analysis, literature split into discovery and evidence-backed,
    experimental evidence table, retrosynthesis per step, provenance for
    every important finding, and limitations derived from the run's actual
    state. It renders what exists and says what is missing. It never
    completes a gap with a plausible value, and it never upgrades status.
    """
    out: list[str] = []
    add = out.append

    add("# Research Report")
    add("")
    add(f"Status: {_v2_status(report, session)}")
    outcome = getattr(report, "outcome", "") or ""
    if outcome and outcome != "completed":
        add(f"Outcome: {outcome.upper()}")
    if _v2_status(report, session) != "COMPLETED":
        add(f"Stopped at: {_stopped_at(session)}")
    add("")

    # -- 1. Research Task ---------------------------------------------------
    add("## 1. Research Task")
    add("")
    add(f"- Question: {report.question}")
    if session is not None:
        add(f"- Task ID: {session.task.task_id}")
        add(f"- Session ID: {session.session_id}")
    add(f"- Generated: {report.generated_at}")
    if report.interpretation:
        add(f"- Interpretation: {report.interpretation}")
    add("")

    # -- 2. Chemical Identity ----------------------------------------------
    _render_identity_v2(add, identity)

    # -- 3. Molecular Analysis ---------------------------------------------
    _render_molecular_v2(add, report)

    # -- 4. Literature -----------------------------------------------------
    _render_literature_v2(add, report)

    # -- 5. Experimental Evidence -----------------------------------------
    _render_experimental_v2(add, report)

    # -- Reactions (the knowledge layer, Phase 3.10) ----------------------
    _render_reactions_v2(add, report)

    # -- 6. Retrosynthesis -------------------------------------------------
    _render_retrosynthesis_v2(add, report)

    # -- 7. Evidence and Provenance ---------------------------------------
    _render_provenance_v2(add, report)

    # -- 8. Limitations ----------------------------------------------------
    add("## 8. Limitations")
    add("")
    gaps = _limitations_v2(report, session, identity)
    if not gaps:
        add("(none identified)")
    else:
        for gap in gaps:
            add(f"- {gap}")
    add("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Report v3 (Phase 3.12): the knowledge-aware renderer
# ---------------------------------------------------------------------------

#: Stable order for the knowledge sections' object lists. Fixed so two runs
#: over the same knowledge render the same bytes (§29/§65).
_KNOWLEDGE_KINDS = ("entities", "papers", "reactions", "evidence",
                    "reaction_records", "routes", "mentions")

_KIND_LABELS = {
    "entities": "Entities",
    "papers": "Papers",
    "reactions": "Reactions",
    "evidence": "Evidence",
    "reaction_records": "Reaction Records",
    "routes": "Routes",
    "mentions": "Mentions",
}

_STATE_LABELS = {
    "known": "KNOWN",
    "partially_known": "PARTIALLY_KNOWN",
    "unknown": "UNKNOWN",
}


def render_research_report_v3(
    report: ResearchReport,
    *,
    session: "ResearchSession | None" = None,
    identity: "ChemicalIdentityResolution | None" = None,
    knowledge=None,
) -> str:
    """The knowledge-aware report: v2, plus what knowledge changed (§98-§100).

    A pure renderer (§99): domain/query results in, Markdown out. It
    distinguishes **existing knowledge** (what the knowledge base already
    held -- a record of past sources, never a claim of currency, §15),
    **new knowledge** (what this run established), **conflicts** (two
    sources disagreeing, both cited, nothing averaged), and **knowledge
    gaps** (what a complete answer would have and we do not). Without a
    `knowledge` payload the output is exactly v2's -- the API is additive
    and every existing caller keeps working.
    """
    markdown = render_research_report(report, session=session, identity=identity)
    if knowledge is None:
        knowledge = getattr(report, "knowledge", None)
    if knowledge is None:
        return markdown

    out: list[str] = []
    add = out.append

    # -- Existing Knowledge ------------------------------------------------
    add("## Existing Knowledge")
    add("")
    add("既有知识来自本知识库已记录的来源（各条均含 provenance 与 "
        "retrieved_at）；它记录的是“曾经建立”，不代表当前最新（§15）。")
    add("")
    existing = knowledge.existing or {}
    wrote_any = False
    for kind in _KNOWLEDGE_KINDS:
        ids = existing.get(kind) or []
        if ids:
            wrote_any = True
            add(f"- {_KIND_LABELS[kind]}: {', '.join(ids)}")
    if not wrote_any:
        add("(none -- 这是本次研究第一次建立知识)")
    add("")

    # -- New Knowledge -----------------------------------------------------
    add("## New Knowledge")
    add("")
    new = knowledge.new or {}
    wrote_any = False
    for kind in _KNOWLEDGE_KINDS:
        ids = new.get(kind) or []
        if ids:
            wrote_any = True
            add(f"- {_KIND_LABELS[kind]}: {', '.join(ids)}")
    if not wrote_any:
        add("(none -- 本次研究没有新建知识对象)")
    add("")

    # -- Conflicts ---------------------------------------------------------
    add("## Conflicts")
    add("")
    if knowledge.conflicts:
        for conflict in knowledge.conflicts:
            add(f"- {conflict.field}: CONFLICT")
            for observation in conflict.observations:
                cited = observation.evidence_id or "(no evidence id)"
                where = observation.paper_id or observation.doi or \
                    observation.source_id or "(no source)"
                raw = observation.raw_value or str(observation.value)
                unit = observation.raw_unit or observation.unit
                add(f"  - {raw} {unit}".rstrip()
                    + f" ← {cited} ({where}, {observation.reaction_id})")
            add(f"  - {conflict.note}")
    else:
        add("(none)")
    add("")

    # -- Knowledge Gaps ----------------------------------------------------
    add("## Knowledge Gaps")
    add("")
    if knowledge.gaps:
        for gap in knowledge.gaps:
            add(f"- [{gap.facet.value}] {gap.reason}")
            if gap.suggested_action:
                add(f"  - 建议：{gap.suggested_action}")
    else:
        add("(none identified)")
    add("")

    # -- coverage states, compact -----------------------------------------
    if knowledge.coverage is not None and knowledge.coverage.facets:
        add("## Knowledge State")
        add("")
        for facet in knowledge.coverage.facets:
            add(f"- {facet.facet.value}: "
                f"{_STATE_LABELS.get(facet.state.value, facet.state.value)}"
                + (f" -- {facet.detail}" if facet.detail else ""))
        add("")

    anchor = "\n## 8. Limitations"
    head, sep, tail = markdown.partition(anchor)
    if not sep:  # pragma: no cover - the anchor is contractual
        return markdown + "\n".join(out)
    return head + "\n".join(out) + sep + tail

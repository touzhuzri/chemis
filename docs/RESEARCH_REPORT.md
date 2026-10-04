# Research Report（Stage 7.3）

## 目标

把 `ResearchStudy` 组织成**可读、可追溯**的报告：

```text
ResearchStudy → StudyReportBuilder → StudyReport → Renderer → Markdown
```

报告组织研究结果、整理章节、引用来源、展示证据/路线、总结已有分析、标记限制与未解决问题。**Report ≠ Final Audit**：报告即使生成成功，也**不**声称 `VERIFIED` / `VALIDATED` / 最终科学结论（§二/§六十四）——事实一致性与证据完整性审计是 7.4。

## 命名说明（重要）

既有 `core/models/research.py::ResearchReport`（**一次 research task** 的报告，Phase 3.x）与 `report/research.py` 的渲染器**未改动**。本阶段是**不同对象**（研究 session 的报告），命名为：

- `core/models/study_report.py`：`StudyReport` / `ReportSection` / `ReportSourceRef` / `ReportReference` / `ReportWarning` / `SourceRefType`（`REPORT_VERSION = "crea-research-report-v1"`）。
- 渲染器：`report/study.py::render_study_markdown`（**新增文件**，未触碰 `report/render.py`、`report/research.py`）。

## 先审计（已执行）

审计确认：`report/render.py`（结构匹配报告）· `report/research.py`（workflow 报告）· `core/models/report.py::StructureMatchReport`。**无**既有 study-report 模型 → 新建，且**未覆盖任何既有文件**。

## 报告是 StudyArtifact，不是新表（§五/§四十三）

Report 作为 `ArtifactType.RESEARCH_REPORT` 的 `StudyArtifact` 保存（复用 7.1 系统）。**未**新增 `research_reports` 表。**版本累积**（§四十四）：每次生成新 artifact（`report_id` + `generated_at`），**不覆盖历史**（`latest` 取最后一个，`by artifact_id` 取指定版本）。

## 章节（固定顺序，§八/§四十五）

```text
question · scope · molecules · reactions · evidence · routes
analyses · workflows · notes · limitations · open_questions · references
```

- **Molecule/Structure**（§十一/§十二）：复用 `ChemicalEntity`（SMILES / Standard InChIKey / formula / MW / names）；**不**在报告层重算化学；无名称 → `unavailable`。
- **Reactions**（§十三）：复用 `ReactionTemplate`（class / transformation / reagents / conditions / selectivity）。
- **Evidence & Literature**（§十六/§十七/§十八）：保留 `ReactionEvidence → Paper` 关系（evidence_id / type / status / **support_scope** / locator / `[P1]`），不只列 Paper。DOI 用既有 normalized 形式。
- **Routes**（§二十二/§二十三/§二十四/§二十五）：来自 `ROUTE_EXPLORATION` artifact，状态**原样保留**（COMPLETE/PARTIAL/BLOCKED），**不**改写（PARTIAL 不写成 "possible complete route"；BLOCKED 不写成 "impossible"，只写 "blocked because ..."）。无完整路线 → 明写 *"No complete deterministic route was found in the current CREA reaction knowledge."*（**≠** "No synthesis exists"）。
- **Analyses**（§四十六）：**原样引用**已保存的 `MODIFICATION_ANALYSIS`/`ROUTE_REASONING`/`AGENT_RESULT` artifact；**不**重新调用 LLM。
- **Workflows**（§五十七）：读 `TaskWorkflow` 状态与任务计数；**不**重跑。
- **Notes**（§五十九/§六十）：`USER_NOTE`，**绝不**升级为科学事实。
- **Limitations / Open Questions**（§三十三/§三十四）：只列 study **实际**记录的限制与未解决问题（blocked/failed task、缺失引用），不 AI 自由生成"未来方向"。
- **References**（§三十五）：由 evidence-linked literature **去重**（paper_id / normalized DOI），分配稳定 `[P1]`。

## 只读、可复现（§四十一/§四十二/§四十五/§四十六）

Builder **只读** study/references/events/artifacts；**不**重算 chemistry、**不**跑 search、**不**跑 Agent/LLM、**不**联网（§七十三/§七十四）。唯一写入 = 保存 report artifact。相同 study state → 相同 section/reference/source 顺序。**不**依赖 DB natural order。

## Unsupported / Warnings（§六十三/§六十五）

结构化数据缺 source → 记 `ReportWarning`（如 `MOLECULE_NOT_FOUND` / `REACTION_NOT_FOUND` / `EVIDENCE_NOT_FOUND` / `LITERATURE_NOT_FOUND` / `TASK_NOT_COMPLETED`），**不**硬编 citation。恒定 warning `NOT_AUDITED` 说明报告未经 7.4 审计。存在 warning **不**删除报告。

## Source graph（§六十二）

每个 section 携带 `source_refs`（type: molecule/reaction/evidence/literature/route/artifact + id），为 7.4 的 Citation Audit 打基础。

## HTML 安全（§五十一）

本阶段只产 **Markdown**（§五十：不要求 PDF）。渲染器对 `|`、换行、反斜杠**转义**，用户笔记/标题无法注入结构。

## API（§五十二/§五十三/§五十四）

```text
POST /api/research/sessions/{study_id}/report              → 生成并保存（新版本）
GET  /api/research/sessions/{study_id}/report              → 最新版本
GET  /api/research/sessions/{study_id}/report/{artifact_id} → 指定版本
```

返回 `{artifact_id, report_version, status:"generated", report, markdown}`。不存在 → 404。生成**不**改 Study/Workflow 状态（§五十五）。

## UI

`#/sessions/{id}` 内「Research Report」区：`Generate Report` → Markdown 展示。

## 测试

`tests/research/test_study_report.py`（13）：固定章节顺序、source_refs、确定性、evidence→literature 关系 + reference 去重、USER_NOTE 标记不外升、route 状态不被改写 + 无完整路线限制句、**生成不改变 study**（无新 artifact/event）、artifact 版本累积 + latest/by-id、**绝不含 VERIFIED/VALIDATED** + NOT_AUDITED、未知 study 结构化错误、Markdown 转义、API（POST/GET/latest/by-id/404）。

架构测试 +2：`services/study_report.py` / `report/study.py` / `core/models/study_report.py` 不 import providers/sources/agent/llm/httpx/sqlite3/rdkit；`study_report.py` 不含 `"VERIFIED"`/`"VALIDATED"` 字面量。

## 停止

**Stage 7.3 completed.** 到此停止——**不**进入 Stage 7.4（Final Audit：evidence audit / citation completeness / claim consistency / scientific validation / final approval）。

# Final Audit（Stage 7.4）

## 目标

CREA V1 固定路线的**最后一阶段**。对一个 `ResearchStudy` 及其研究报告执行确定性的一致性审计：

```text
结构完整性 · 引用完整性 · Evidence 连贯性 · Literature 关系
Route 一致性 · AI grounding · Workflow/Artifact · Report source · 已知限制
        ↓
FinalAuditResult
```

**Audit ≠ Scientific Truth**：审计能证明系统**内部一致**（引用存在、evidence 可追溯、AI claim 有 grounding、报告引用对应数据、状态自洽），**不能**证明实验一定成功 / 反应一定发生 / 文献结论一定正确 / 路线实验可行 / 产率数值。

## 先审计（已执行）

审计确认：**既有** `services/knowledge_integrity.py::KnowledgeIntegrityService.audit(kb)`（Phase 3.12 的 **KB 级** 完整性审计，返回 `KnowledgeIntegrityReport`/`IntegrityIssue`）。它**未改动**。本阶段新增 **study 级** `FinalAuditService`（不复用其模型、不覆盖其文件）。

## 只读 / 离线 / 确定性 / 不修复（§六十七/§六十八/§六十九/§七十/§八十八）

- **无网络**（不调 Crossref/OpenAlex/Unpaywall/PubChem/HTTP）。
- **无 AI**（不让 AI 审 AI）。
- **无修复**：发现问题只报告，**不**自动补 DOI / 建 Paper / 修 Evidence。
- **确定性**：同 study state → 同 check 状态与 warn/fail 分类（除 audit_id/created_at）。
- 唯一写入 = 保存 audit artifact（`StudyArtifact` type `FINAL_AUDIT`，**无新表**）。

## Status（§六）

`PASS`（全部必检通过且无严重问题）· `PASS_WITH_WARNINGS`（结构正确但有 unknown/unavailable/partial/incomplete 等非致命限制）· `FAIL`（dangling reference / invalid relation / 存在性问题）。

## 16 项检查

| check | 内容 | FAIL 条件 |
|---|---|---|
| study_integrity | session 元数据自洽（updated≥created） | 时间倒置 |
| reference_integrity | 每个引用解析（molecule/reaction/literature/evidence）；重复 → WARN | 引用不存在 |
| molecule_identity | entity_id/inchikey/smiles 存在 | 无 entity id |
| reaction_references | template 存在 + 有 substrate pattern | 不存在/无 pattern |
| evidence_chain | evidence 存在；literature source 存在；**curated 不升格** | 悬空文献 / curated 被标 literature |
| evidence_dedup | (template,source_type,source_id,type,scope) 去重 | — (WARN) |
| evidence_locator | locator 字段合法（page 为 int） | 非法字段 |
| evidence_provenance | provenance 存在 | — (WARN) |
| literature_integrity | paper 存在；DOI 规范；DOI 去重 | paper 不存在 |
| literature_evidence_reverse | 正向 evidence→paper 能被反向查询找到 | 正反向不一致 |
| fulltext_permission | permission 与 evidence 独立；unknown → WARN | — |
| route_consistency | route artifact 状态完整；无 complete → WARN | 缺 status 字段 |
| ai_grounding | AI artifact 引用的 reaction/paper 真实存在 | 幻觉 id |
| workflow_integrity | task 依赖解析；blocked/failed → WARN | 依赖缺失 |
| report_integrity | 报告 source_refs / references 解析 | 引用不存在 |
| timeline_consistency | event 不早于 study | 时间倒置 |

## 已知限制 = WARN，不是 FAIL（§六十二–§六十五）

`PRODUCT_STRUCTURE_UNAVAILABLE` / `NO_DETERMINISTIC_ROUTE` / `FULLTEXT_PERMISSION_UNKNOWN` / `NO_LITERATURE_EVIDENCE` / `PARTIAL_ROUTE` / `BLOCKED_WORKFLOW` / `MISSING_EXPERIMENTAL_DATA` → **WARN** 并列在 summary。Unknown 不是损坏；not-available 是模型边界；no-route 不是软件错误（只要 Explorer 正常运行 → 该检查 PASS + 可 WARN）。

## Summary（§六十六）

```json
{"total_checks": 16, "passed": N, "warnings": N, "failures": N}
```

## 固定陈述（§九十）

`FinalAuditResult.statement`：**"…This is not a scientific verification: it does not establish that any reaction, route or citation is experimentally correct."**

## Report ↔ Audit（§七十四/§七十五）

审计记录 `report_artifact_id`（Report v1 → Audit A1，Report v2 → Audit A2 可追踪）。**不覆盖历史**：每次重审新增 artifact（`Audit v1`、`Audit v2`…）。审计**不修改**旧 Report 内容（§五十二）。

## API（§七十一）

```text
POST /api/research/sessions/{study_id}/audit   {report_artifact_id?} → 运行并保存（新版本）
GET  /api/research/sessions/{study_id}/audit                          → 最新
GET  /api/research/sessions/{study_id}/audit/{artifact_id}            → 指定版本
```

返回 `{artifact_id, audit_version:"crea-final-audit-v1", status, audit, summary}`。不存在 → 404。

## UI（§七十六）

`#/sessions/{id}` 内「Final Audit」区：`Run Final Audit` → **Status + ✓/⚠/✗ 逐项检查 + 计数 + 固定陈述**。

## 测试

`tests/research/test_final_audit.py`（16）：全检查运行、悬空文献 → **FAIL**、缺失引用 → FAIL、literature↔evidence 正反向一致、unknown permission → WARN 非 FAIL、no-route → WARN、报告源解析、报告悬空源 → FAIL、AI 未 ground → FAIL / 已 ground → PASS、blocked workflow → WARN、**确定性 + 不改变 study**（无新 artifact/reference）、陈述非科学结论、未知 study 结构化错误、API（POST/GET/latest/by-id/404）、audit 版本累积。

架构测试 +1：`services/final_audit.py` / `core/models/final_audit.py` 不 import providers/sources/agent/llm/httpx/sqlite3/rdkit；代码（去 docstring）不含 `save_`/`delete_`/`add_reference`/`link_literature`/`create(` 写路径。

## 边界（§九十三/§九十四）

无网络 · 无 AI · 无修复 · 除 audit artifact 外不改数据库 · 不声称科学验证。

## 最终停止

**Stage 7.4 completed.** 这是固定 24 阶段路线的最后一阶段。**不**进入 Stage 8，**不**自行加入新 Reaction Library / AI Provider / PDF-OCR / 实验数据库 / 自动论文抓取 / 自动实验规划 / Agent memory / 新 Workflow Engine。

最终产品链：

```text
Molecule → Reaction → Literature → Evidence → Route → AI
        → Research Session → Workflow → Report → Final Audit
```

原则：**Chemistry Tool First, AI Second** · **AI 可以推理，但 Evidence 决定事实边界。**

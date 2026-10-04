# CREA V1 — Acceptance / Trial Report

> **原则：Freeze Features, Test Reality.** 只验收、只修 BUG、只加回归测试。
> 未加任何新功能。

## 0. 基线与结论

```text
基线（进入本阶段）：1999 passed, 50 skipped, 0 failed · npm run build 通过
结束（修复 1 个 P1 后）：2000 passed, 50 skipped, 0 failed · npm run build 通过
```

```text
P0: 0     P1: 1（已修 + 回归测试）     P2: 2（记录）     P3: 车库（不实施）
```

**验收结论：CREA 能作为一个化学研究工具工作。** 发现并修复了一个真实缺陷（离线模式被绕过），其余为观感/后续需求，未实施。

---

## 1. Round A — 系统健康

| 项 | 结果 |
|---|---|
| A1 后端启动 | ✅ 无 traceback（`create_app` + 全端点 200） |
| A2 前端 | ✅ `npm run build`（tsc + vite）通过；页面路由 `#/molecule` `#/reactions` `#/library` `#/literature` `#/sessions` `#/chat` 齐备 |
| A3 health / 语义 | ✅ 确定性工具与 AI 解耦：offline 下 `/api/ai/agent` 与 `/api/literature/search` 返回 503，而 `molecule/inspect`、`molecule/3d`、`reaction-templates`、`transformations/search` 全部 200 |
| A4 offline / CLI | ✅ `crea demo`、`crea research`、`crea kb sessions` 正常；`research` 如实 PARTIAL |

**发现（P1，已修）**：`--offline` 被 **workspace 对话路径**绕过（见 §7）。

---

## 2. Round B — Chemistry Tool

| 分子 | formula | DBE | 官能团 | 反应数 |
|---|---|---|---|---|
| `CCO` | C2H6O | 0 | Alcohol | 4 |
| `c1ccccc1` | C6H6 | 4 | — | 5 |
| `C#Cc1ccccc1` | C8H6 | 6 | Alkyne, Terminal Alkyne | 12 |
| `CC(=O)O` | C2H4O2 | 1 | Carboxylic Acid | 1 |
| `CCBr` | C2H5Br | 0 | Alkyl Halide | 4 |

- **B5**：`CCO` 命中酒精相关模板——Primary Alcohol Oxidation / Alcohol Dehydration / →Carboxylic Acid / Alcohol → Alkyl Halide。与实际生产库一致（不要求"必须找到期待的反应"，只验一致性）。✅
- **B4 二甲苯异构体**：o/m/p 三者 `scaffold=identical`、Tanimoto 分别 0.375/0.385/0.429、`common_atoms=7`——与项目声明的"Murcko 骨架忽略取代位置，Morgan 分数不区分位置"caveat 一致。✅
- 3D：`C#Cc1ccccc1` 14 原子 14 键，ETKDG 生成。✅

---

## 3. Round C — Evidence / Literature 闭环

```text
Reaction → Evidence → Literature → Reaction   （双向）
```

- C1 Reaction Detail：name/transformation/reagents/conditions/selectivity/limitations/evidence 均可返回（selectivity 为该模板 null，属数据而非缺陷）。
- C2 Curated：`CURATED_KNOWLEDGE` 显示为 `curated`；**未被升格**为 literature。
- C3 Literature-linked：经 `link_literature` 的证据显示 `source_type=literature` + `support_scope`。
- C4 反向：`GET /api/literature/{paper}/reaction-evidence` 找到同一条 → **双向一致**。✅

---

## 4. Round D — AI 边界（Fake AI）

| 场景 | 结果 |
|---|---|
| D1/D2 捏造 `reaction_template_id` | ✅ 进 `unsupported`，**不进** candidate |
| D3 条件幻觉（0 °C / 2 h） | ✅ 只存在于 AI `rationale`（标注为 AI interpretation）；输出**无 condition 通道**，无法成为事实 |
| D4 捏造 DOI/paper | ✅ 文献 id 被剥离 + warning「not in the context」 |
| D5 空 `reaction_smarts` 下的 Route Reasoning | ✅ 捏造的 `route_id` → `unsupported`，observations=0，状态仅 `partial`——**未自造路线** |
| D6/D7 Agent grounding + 上限 | ✅ 重复工具调用 → `repeated_tool_call` 停止 |

**AI 可以推理，但不能成为化学事实的来源。** ✅

---

## 5. Round E — 全链（HTTP）

```text
POST /api/molecules            → CHEM-00000001
POST /api/research/sessions    → study
POST .../references ×2         → 2 refs
POST .../workflows (template)  → 4 tasks
POST .../start + tasks/{}/execute → completed + artifact
POST .../report                → crea-research-report-v1，12 sections
POST .../audit (report id)     → PASS，16/16，report_artifact_id 关联
timeline: study_created → molecule_added → reaction_added → task_created
        → workflow_created → workflow_started → task_started
        → task_completed → workflow_completed
```

✅ 全链贯通，审计 PASS，报告与审计版本互相关联。

---

## 6. Round F — 回归 / 架构

```text
pytest -q                       → 2000 passed, 50 skipped, 0 failed
pytest tests/architecture -q    → 416 passed
pytest tests/architecture/test_no_network.py → 253 passed（离线守卫仍生效）
npm run build                   → 通过
```

无回归。架构钉（分层 / 无网络 / 无第二模型 / 只读边界）全部保持。

---

## 7. P0 / P1 Bug 列表

### P1-1（已修 + 回归测试）— 离线模式被 workspace 对话路径绕过

**现象**：`--offline` 下 `POST /api/workspaces/{id}/messages` 返回 200 且带**真实模型回答**——`_workspace_agent_factory()` 未接收 `offline`，仍从 `.env` 构造 `OpenAICompatibleLLMProvider`，即**在离线标志后发起了真实网络调用**。这违反"offline = 零网络请求"的全局契约。

**修复**：`_workspace_agent_factory(offline)` 在 offline 下不构造 provider，直接抛 `ProviderUnavailable`；`send_message` 将其转为诚实的"（未能生成回答：服务不可用）"+ diagnostic。已核对**全部** provider factory（AI / literature / unpaywall / pubchem / research stack / workspace）均已 offline 门控。

**回归测试**：`tests/workspace/test_workspace.py::test_offline_workspace_never_builds_a_live_provider`——即使环境里有 key，offline 也**不得**构造 provider（monkeypatch 让构造器失败）。

---

## 8. P2 Backlog（观感 / 可用性，未实施）

- `/api/health` 只返回 `{status, service, db, offline}`，未报 **AI provider 可用性**与确定性化学层状态；A3 期望更明确的语义。（改动 = 新字段，属功能，记录待批。）
- **Report / Audit 不写入 StudyEvent**：study 时间线含 workflow/task 事件，但"生成报告""运行审计"只在 Artifacts 列表可见。可考虑补 `REPORT_GENERATED` / `AUDIT_RUN` 事件。

---

## 9. V1 Parking Lot（P3 —— 明确的后续需求，**本阶段禁止实施**）

```text
- PDF / HTML 报告渲染与导出（当前仅 Markdown）
- Product SMARTS / reaction_smarts 批量补全 → 完整确定性路线
- RxnMapper atom mapping（ToS 清理后）
- 扩充 Reaction Library
- 更多 AI Provider / 本地模型
- 分子绘制 / 结构编辑器输入
- 付费墙全文抓取（须政策门控）
- 可观测性仪表盘（health/metrics 扩展）
- 跨 Study 知识查询
- Agent memory / 更长会话
```

**已知设计限制（记录，非缺陷）**：无完整确定性路线（`reaction_smarts` 空）· fulltext permission 默认 UNKNOWN · 不建模产率 · `search_reactions` 仅限已建立知识。

---

## 10. V1 验收通过标准核对

| 维度 | 结果 |
|---|---|
| 系统（启动 / API / 前端 / 数据库） | ✅ |
| Chemistry Tool（Explorer / Reaction / Similarity / 3D） | ✅ |
| Evidence（Reaction ↔ Evidence ↔ Literature 双向） | ✅ |
| AI（不可用不影响基础工具 / claim 可 grounding / 不造事实） | ✅ |
| Report（结构 / 来源 / 引用） | ✅ |
| Final Audit（确定性 / 离线 / 不修复 / 不声称科学验证） | ✅ |

## 11. 最终判定

$$\boxed{\text{V1 ACCEPTED}}$$

CREA 能作为一个化学研究工具工作——**Chemistry Tool First, AI Second**；
**AI 可以推理，但 Evidence 决定事实边界**。

**验收阶段到此停止。** 不进入 Stage 8，不扩功能。P2/P3 全部留待后续明确开启。

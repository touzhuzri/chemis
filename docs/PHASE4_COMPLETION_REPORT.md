# Phase 4 Completion Report — Chemical Research Workbench

> **Final status: Phase 4 COMPLETE**

把 Phase 3.x 的 Research Intelligence Engine 封装成真正可用的
**Chemical Research Workbench**：浏览器里创建研究、输入问题与结构、上传
PDF、启动研究、实时观察 Identity / Existing Knowledge / Literature /
Entities / Reactions / Evidence / Routes / Gaps / Conflicts / Report，
知识入 KB、下次自动复用。Phase 3 的核心 domain semantics **零改动**
（唯一修改均为 persistence/并发兼容，附回归测试）。

## 1. Architecture（§2 严格保持）

```text
core → chem → services → repositories → agent → report → app → api → Web UI
```

新增两层，全部建立在封板引擎之上：

```text
src/crea/app/     dto.py · events.py · jobs.py · knowledge.py · research.py · demo.py
src/crea/api/     app.py · dependencies.py · errors.py
src/crea/chem/    draw.py        （RDKit→SVG，§18）
src/crea/agent/   live.py + build_live_workflow_stack（组合根）
web/              React + TypeScript + Vite（构建产物 web/dist/）
```

架构测试钉死（§2/§39-§41）：app 层禁 sqlite3/RDKit/FastAPI（纯用例层）；
api 层禁 sqlite3/RDKit/Store；agent/live 仍是唯一网络组合根；
no-network 守卫覆盖新模块。前端 → HTTP API → Application → Service →
Repository → SQLite 是唯一路径。无 ORM、无 Django、无 Next.js、无
Redis/向量库（§40/§124）。

## 2. API（FastAPI，§5）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/research | 创建研究（异步 job）|
| GET | /api/research[/id] | job 列表 / 状态 |
| GET | /api/research/{id}/events | 事实事件流（polling，§42）|
| GET | /api/research/{id}/report | Report v3（markdown + JSON）|
| POST | /api/research/{id}/cancel | 协作式取消（§37）|
| GET | /api/sessions[/{id}] | 会话 / 工作区全视图（§60）|
| GET | /api/entities[/{id}[/navigation|/mentions]] | 实体与 provenance 链（§97）|
| GET | /api/reactions[/{id}[/navigation|/records|/evidence]] | 反应链（只读 §94）|
| GET | /api/evidence/{id} | 证据（只读 §93）|
| GET | /api/papers/{id} · /api/routes/{id} | 论文 / 路线 |
| GET | /api/knowledge/state | KnowledgeState + Gaps + Conflicts（§20/§21）|
| GET | /api/knowledge/search | 结果标注 EXACT / SCREENING（§99）|
| GET | /api/knowledge/export | 确定性知识导出 |
| POST/GET | /api/documents | PDF 上传 / 列表（§57-§59）|
| GET | /api/molecule/svg | RDKit 2D SVG（§18）|
| GET | /api/health | 健康检查 |

错误一律结构化 `{"error": {code, message, detail}}`：INVALID_STRUCTURE /
INVALID_FILE / INVALID_QUERY / NOT_FOUND / INTERNAL——RDKit/SQLite/LLM
异常名与堆栈绝不进响应（§56/§36）。

## 3. Web UI（§13-§20，React + TypeScript + Vite）

- **Research Home**（§15）：问题 / 结构（SMILES·InChI·Name）/ 上传 PDF /
  启动研究 + Recent Research + Knowledge Base 入口
- **Research Workspace**（§16）：Question → Identity Card（§17，含 2D
  分子图与 AMBIGUOUS 明示）→ Existing Knowledge（KNOWN/
  PARTIALLY_KNOWN/UNKNOWN + New）→ Progress（事件时间线）→ Literature →
  Reactions → Evidence → Routes（EXACT/SCREENING 标注）→ Knowledge Gaps
  → Conflicts（双引用、无均值）→ Final Report
- **Knowledge 浏览**（§97）：Entity/Reaction/Evidence/Route 卡片 +
  provenance 链（Reaction→Evidence→Paper→Mention→Entity 一路可点）
- 中文 UI、技术字段英文（§104）；工作台布局不是聊天框（§105）
- `npm install && npm run build` 产出生产 bundle；FastAPI 直接伺服
  `web/dist`（§102/§103，已实测 `GET /` 200）

## 4. Job system（§10/§37/§38/§90-§92）

- **ResearchJob**：queued → running → completed | partial | cancelled |
  interrupted；线程执行真实 `ResearchWorkflow`，HTTP worker 永不阻塞（§101）
- **取消**（§37）：`cancel_requested` 协作式标志，工具步骤边界安全停止，
  绝不 kill、绝不破坏事务；已持久化知识保留
- **并发**（§38）：多 job 并行；store `busy_timeout` + registry
  duplicate-recovery（entity/reaction 注册竞争 → 重读合并，不重复 mint、
  不悬挂 evidence）；jobs.json 原子写（唯一 tmp 名 + 写锁）
- **重启**（§90/§91）：遗留 queued/running → INTERRUPTED（绝不假装完成）；
  崩溃前持久化的 Entity/Reaction/Evidence 完好可查
- **幂等**（§92）：同研究重跑复用同 entity/reaction/paper/evidence
  （Phase 3 identity 语义），session 累积
- **事件**（§11/§12）：事实事件（"Reaction RXN-001 created"），只带
  对象 id 引用，JSONL append-only 日志 + polling 接口；SSE 未引入
  （§42：polling 优先）

## 5. Persistence

SQLite 仍是唯一持久层（§40）：KnowledgeBase / Repository Protocol /
确定性查询全部复用 Phase 3.11/3.12。job 注册表 `research-jobs.json`、
事件 `research-events.jsonl`、文档注册 `documents.json`（文档本体按
content hash 命名）。Evidence/Reaction 无写接口（§93/§94）。

## 6. Security

- **上传**（§57/§58）：扩展名 + PDF magic + 解析校验（不信 filename/
  Content-Type）；文件名净化、存储名=content hash，路径穿越不可能
- **结构输入**（§56）：启动前校验，INVALID_STRUCTURE 结构化错误
- **泄漏**（§36）：错误响应无堆栈/库名/SQL；provenance 入库即 scrub
  credential 类键（Phase 3.12 §87）；导出无 secrets
- **本地优先**（§75）：serve 默认 127.0.0.1；LLM 只经
  API→Application→Workflow→LLMProvider（§41）

## 7. Testing

新增 30 个（1405 → 1435，50 skipped 不变）：

- `tests/unit/test_app_layer.py`（6）：事件事实性/id 引用/JSONL、job
  完成不阻塞、协作取消、重启 INTERRUPTED、上传 content-hash 与路径
  净化、非 PDF 拒绝
- `tests/integration_offline/test_workbench_api.py`（10，TestClient 全真
  引擎）：HTTP 研究全链（B-H）、§92 重研究复用不翻倍、§38 并发
  A/B（审计 ok、无重复 mint、无悬挂 evidence）、PDF 进文档管线（K）、
  结构化错误无泄漏（L）、重启保留知识 + zombie INTERRUPTED（M）、
  EXACT/SCREENING 标注 + SVG（§99/§18）、demo SYNTHETIC、前端 bundle 伺服
- 架构测试 +1（workbench 分层），其余为新模块的自动分层/离线守卫用例

## 8. E2E（真实 Workflow / ToolRegistry / Repository / SQLite / domain
## models / Evidence validation，§114）

`test_research_runs_over_http_through_the_real_engine`：上传 PDF →
POST /api/research → 异步 job → session 全视图 → Report v3 → Reaction
导航（Reaction→Evidence 链）→ Entity 导航（Entity→Reactions）→
KnowledgeState/Gap → 事实事件。另：demo 命令全链（identity/knowledge/
reaction/evidence/report）+ serve 实进程冒烟（health + 静态 UI 200）。

## 9. Known limitations

- 事件为 polling（SSE 预留未实现，§42 允许）
- 默认 serve 走 live providers（OpenAlex/Crossref 免凭据）；无 LLM key
  时反应抽取诚实缺位（discovery-only）。`--offline` 用合成 fixture
- Research Assistant 聊天入口未做（§106 允许延期；工具 API 已就绪）
- UserAnnotation（§95）未实现（用户备注≠实验事实，允许延期）
- 3D viewer 仅预留接口（§19）；2D SVG 已接 RDKit
- job 注册表为 JSON 文件（单机语义）；多进程服务器需换 SQLite 表
- 无用户系统/权限（§118 留待后续）

## 10. Deferred features（留给后续阶段，明确不做）

Web UI 高级可视化 / Semantic search / RAG / embedding / 向量库 /
多 agent / 云部署 / 用户认证 / PostgreSQL 迁移 / Electron·Tauri /
Next.js·Nuxt / 3D 结构生成。

## 11. Completion criteria（§121 对照）

| # | 标准 | 状态 |
|---|---|---|
| A | Web UI 可启动 | PASS（serve + web/dist，实测 200）|
| B | Research 可从 HTTP 创建 | PASS |
| C | ResearchJob 异步执行 | PASS |
| D | 真实 ResearchWorkflow 被调用 | PASS |
| E | KnowledgeBase 真正被调用 | PASS |
| F | SQLite 真正持久化 | PASS |
| G | ResearchSession 可查询 | PASS（/api/sessions/{id}）|
| H | Report v3 可展示 | PASS |
| I | Entity/Reaction/Evidence/Route 可导航 | PASS（§97 链）|
| J | Conflict/Gap/Partial 正确显示 | PASS |
| K | PDF 经 API 进入文档管线 | PASS |
| L | LLM/provider unavailable 行为正确 | PASS（discovery-only / PARTIAL）|
| M | 重启后历史知识仍存在 | PASS |
| N | 无数据泄漏 / 只读知识 / 安全边界 | PASS（§36/§93/§94 测试钉住）|

## 12. Full Test Result

```
pytest -q: 1435 passed, 50 skipped, 0 failed
```

基线（§122 对照）开始前已确认：`1405 passed, 50 skipped, 0 failed`。

## 13. 最终结论

```text
Phase 4 COMPLETE
```

CREA 已从 **Python chemistry research engine** 升级为
**Chemical Research Workbench**：打开浏览器 → 输入问题与结构 → 上传 PDF →
启动研究 → 观察过程 → 查看知识/证据/冲突/缺口 → 查看报告 →
下一次研究继续复用。核心原则始终成立：

```text
LLM = Research Orchestrator（≠ Truth）
RDKit = Chemical Computation
Provider = External Information
Evidence = Support
KnowledgeBase = Persistent Memory
ResearchWorkflow = Deterministic Research Process
Web UI = Human Interface
```

按要求停在 Phase 4 COMPLETE，**不进入 Phase 5**。

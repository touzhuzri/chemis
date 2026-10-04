# Stage F Report — Chemistry Workspace MVP

> **Stage F: COMPLETE**
> 基线：1524 passed, 50 skipped, 0 failed（Stage E 末）
> 最终：**1544 passed, 50 skipped, 0 failed**（零回归）

一句话：CREA 从「有一个 ChemistryAgent」推进到「**用户打开 CREA → 创建一个
化学 Workspace → 输入自然语言 → Agent 调用真实化学工具 → 返回带
evidence/provenance 的回答 → 继续追问，Agent 利用当前 Workspace 上下文理解
“它”**」。到线即停——没有长期记忆、没有多 Agent、没有 streaming、没有登录。

```text
Browser
  ↓
Chemistry Workspace (Web UI + API)
  ↓
WorkspaceApplication (orchestration + context builder)
  ↓
ChemistryAgent (Stage E, 零化学)
  ↓
ToolRegistry → 真实化学工具 → evidence / provenance
```

## 1. pytest

```text
1544 passed, 50 skipped, 0 failed
```

新增 `tests/workspace/test_workspace.py`（11，含 HTTP E2E）+ `tests/architecture/`
（+3）。新增 4 个源文件同时扩充了参数化架构测试矩阵（全部通过）。

## 2. Workspace 领域模型（§3/§4）

`core/models/workspace.py`（ADDITIVE）：
- `MessageRole`（`user` / `assistant`，两个角色）。
- `Workspace`（`workspace_id` / `title` / `created_at`）。
- `WorkspaceMessage`（`message_id` / `workspace_id` / `role` / `content` /
  `tool_calls: list[ToolResult]` / `evidence` / `provenance` / `diagnostics`）。

**复用唯一结果词汇**（架构钉 `test_the_workspace_domain_reuses_the_one_
message_and_result_vocabulary`）：`tool_calls` 直接是 Stage E 的 `ToolResult`
列表——无 `Message2`/`ToolResult2`。

## 3. 持久化（§4/§20/§21）

SQLite **additive**（`_SCHEMA_STATEMENTS` 增两表，`CREATE TABLE IF NOT
EXISTS`，无需版本 bump）：
- `workspaces(workspace_id, title, created_at)`
- `workspace_messages(workspace_id, position, message_json)`（`position` 为追加
  顺序，消息体单列 JSON 完整携带 AgentResult 字段）。

`SQLiteStore` 增 `save_workspace` / `get_workspace` / `list_workspaces` /
`append_message` / `list_messages`；`SQLiteWorkspaceRepository` 薄适配；
`KnowledgeBase.workspace_repository()` 一个门面。协议 + `InMemoryWorkspace
Repository` 在 `services/workspace.py`（与 entity/reaction 仓库同构）。

只建 `workspaces` + `messages` 两个对象，无 documents/entities/artifacts/
graph（§4 明令留后）。

## 4. Workspace 用例层（`app/workspace.py`）

`WorkspaceApplication(db_path, agent_factory)`：
- `create_workspace` / `list_workspaces` / `get_workspace` / `list_messages` /
  `send_message`（§5/§21）。
- 每次操作开一个短生命周期 `KnowledgeBase`（沿用 `KnowledgeApplication` 的
  「无跨线程共享连接」纪律）；工作区仓库与 agent 的化学注册表共享同一 store。

**§22/§30 边界（架构钉钉住）**：
- `app/workspace.py` 不 import rdkit/chem/semantic/providers/sources/tools/
  repositories/llm——化学只经注入的 `agent_factory`（`test_the_workspace_app_
  reaches_chemistry_only_through_the_agent`）。
- `agent/chemistry_agent.py` 不 import 任何 workspace/repository/app——
  Agent 只收到中性消息列表，从不知 Workspace（`test_the_agent_never_reaches_
  the_workspace_repository`）。

## 5. 多轮上下文（§8/§9/§10）

`ChemistryAgent.run(user_message, *, history=None)`（Stage E ADDITIVE）：
调用方把 workspace 历史作为中性 `{"role","content"}` 列表种子线程，Agent
自己无 workspace 概念。

`WorkspaceApplication._history(messages)` = 最近 `MAX_CONTEXT_MESSAGES=30`
条 → 中性 LLM 消息。只保留 role+content 对话面；tool 结果/证据每次由 Agent
重新产出，不从存储回放。硬上限，无自动摘要、无长期记忆（§10/§29）。

## 6. API（§5/§16/§22）

```text
POST /api/workspaces                    创建（{} 或 {title}）
GET  /api/workspaces                    列表
GET  /api/workspaces/{id}               读取
GET  /api/workspaces/{id}/messages      消息列表
POST /api/workspaces/{id}/messages      发送（{content}）
```

`POST messages` 返回 `{message, tool_calls, evidence, provenance, diagnostics}`
（§16：Stage E 结果在 Web 层不被拍平成 `{answer}`）。`api/app.py` 零化学
（只经 `ctx.workspace`）；`api/dependencies.py`（composition root）构造
`_workspace_agent_factory`——真实 OpenAI-compatible LLM + `agent/
workspace_stack.py::build_workspace_agent`（OPSIN+PubChem 名称链、真实
SemanticResolver、SQLite 反应注册表）。`agent/workspace_stack.py` 登记为
composition root（`test_no_network.py`）。

## 7. 前端（§11-§18）

复用 React 18 + Vite 5 + TS（不换栈）。新增 `web/src/pages/ChatPage.tsx`
（`#/chat` / `#/chat/<id>` 路由）+ `api.ts`/`types.ts` 增补：
- 左侧 workspace 列表（新建/切换，§13）；右侧聊天区（§12/§14）。
- assistant 消息带可展开 `▶ 工具调用 / Evidence / Provenance`（默认折叠）。
- loading「正在思考…」+ input/send disabled 防重复（§18）。
- 错误区分（§17）：`unavailable` 显示「当前数据源不可用」而非「请求失败」。
- 无 streaming（§19）。`npm run build`（tsc + vite）通过。

## 8. 不确定性处理（§17）

复用 Stage E 的诚实中继：`AMBIGUOUS` 不猜 `candidates[0]`、`UNAVAILABLE ≠
空结果`，均体现在 assistant 消息的 `tool_calls`/`diagnostics` 里并随 HTTP
往返到达前端。`workspace not found` → 404；agent 失败（如无 LLM key）→ 诚实
的 assistant 消息 + diagnostics，而非假答案。

## 9. 测试（§23/§24/§26，deterministic）

`tests/workspace/test_workspace.py`（ScriptedLLM，真化学工具）：
domain+SQLite/InMemory 往返、send 双侧持久化、多轮「它」解析（回调断言历史
到达模型）、evidence/provenance 保留、ambiguous 保留、unavailable≠empty、
not-found、agent 失败诚实、上下文硬上限、**HTTP E2E**（TestClient + 真
resolver + 真 ReactionRegistry，两轮「苯乙炔是什么？→ 它有哪些已知反应？」）。

## 10. End-to-End 示例（§26）

`test_workspace_chat_over_http_end_to_end`：

```text
POST /api/workspaces  →  ws_xxx
POST /api/workspaces/ws_xxx/messages {"content":"苯乙炔是什么？"}
  → resolve_chemical → analyze_structure → 回答「苯乙炔是含碳碳三键的芳烃」
POST /api/workspaces/ws_xxx/messages {"content":"它有哪些已知反应？"}
  → 历史进入上下文 → search_reactions(product=苯乙炔) → RXN-HTTP
  → {message, tool_calls, evidence:[EV-1], provenance, diagnostics}
GET /messages → [user, assistant, user, assistant]
```

第二句「它」基于第一轮上下文解析。只有 LLM 脚本化；ChemistryAgent 与真实
化学工具是真实实现。

## 11. 已知限制

- **无长期记忆 / 用户画像 / 跨 workspace memory / 向量库**（§29 明令）。
- **无登录/用户系统**（§27，本地优先单用户）。
- **无文件系统 workspace**（§28：无 PDF/图片/SDF 拖入）。
- **无 streaming**（§19，普通 HTTP request/response）。
- **上下文仅最近 30 条**，超出即截断（§10，不做摘要）。
- 生产 agent 需 `CREA_LLM_API_KEY`；无 key 时 `send_message` 诚实报错。
- 左侧列表无文件夹/标签/搜索/排序/权限/协作（§13 明令不做）。

## 12. 下一阶段边界（不执行，§32）

Workspace 2.0、对话摘要、长期记忆、Chemistry Canvas、3D viewer、文档管理、
PDF 摄取、知识图谱、图/向量库、协作编辑、认证、streaming、MCP、多 Agent、
自主科研、递归合成——全部留后。

**Stage F 到此停止，不自行进入 Stage G。**

胜利线（§33）：用户打开 CREA → 创建化学 Workspace → 输入自然语言 →
ChemistryAgent 用真实化学工具 → 返回带 evidence/provenance 的结果 → 继续追问，
Agent 利用当前 Workspace 上下文理解后续问题——已达成。

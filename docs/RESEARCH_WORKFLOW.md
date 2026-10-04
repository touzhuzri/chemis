# Research Workflow（Stage 7.2）

## 目标

在 Stage 7.1 的研究 session（`ResearchStudy`）之上，实现**有界的研究工作流**：

```text
ResearchSession → Workflow → Task 1 → Artifact
                          → Task 2 → Artifact
                          → Task 3 → Artifact
```

每个 Task 有状态、输入、输出（artifact）、依赖、时间线，可恢复。**还没到"生成研究报告"的阶段**（7.3）。

## 命名说明（重要）

既有 `services/research_workflow.py::ResearchWorkflow`（Phase 3.7 的**单次运行流水线**）与 `core/models/research.py::ResearchTask`（**一次研究问题** + report）已存在。本阶段的对象是**不同的东西**（任务 DAG 编排）。为避免重复模型，本阶段命名为：

- `core/models/task_workflow.py`：`TaskWorkflow` / `WorkflowTask` / `TaskWorkflowDetail` / `WorkflowStatus` / `TaskStatus` / `TaskType`。
- 存储表：`task_workflows` / `workflow_tasks`（**未**与既有 `research_sessions`/`research_workflows` 冲突）。

## Workflow Status（§五）

`READY`（已建未跑）· `RUNNING`（执行中）· `PAUSED`（用户暂停）· `BLOCKED`（因缺数据/输入/工具错误无法继续）· `COMPLETED` · `CANCELLED`。**PAUSED（用户暂停）≠ BLOCKED（系统无法继续）**（§三十四）。

## Task Model（§六/§七）

`WorkflowTask`：`task_id`（`{workflow_id}::{key}`）· `task_type` · `status` · `title` · `input_refs` · `depends_on` · `output_artifact_ids` · `attempt_count` · `error` · 时间戳。

`TaskType`：`MOLECULE_INSPECTION` / `TRANSFORMATION_SEARCH` / `REACTION_COMPARISON` / `LITERATURE_SEARCH` / `EVIDENCE_REVIEW` / `ROUTE_EXPLORATION` / `MODIFICATION_ANALYSIS`(AI) / `ROUTE_REASONING`(AI) / `AGENT_ANALYSIS`(AI)。

`TaskStatus`：`PENDING`（依赖未满足）· `READY` · `RUNNING` · `COMPLETED` · `FAILED`（工具/provider 出错）· `BLOCKED`（缺输入）· `SKIPPED` · `CANCELLED`。

**不复制结果**（§十二）：Task 只记 `output_artifact_ids`，结果在 Artifact，领域对象各归其位（`Study / Task / Artifact / Domain Object` 分层）。

## DAG 与边界（§十三/§十四/§十五/§五十八）

`depends_on` 支持 DAG（**非**复杂 DAG 引擎）。创建时检查：**循环 → `WORKFLOW_CYCLE`**（三色 DFS）；超 `MAX_TASKS=20` / 深度超 `MAX_WORKFLOW_DEPTH=8` → `WORKFLOW_LIMIT_REACHED`。依赖必须同 workflow。task 顺序确定（按 position）。

## Workflow ≠ Agent Loop（§十六/§五十）

```text
Workflow（orchestrator）→ Task → Service / Agent（worker）→ Artifact → Workflow
```

Workflow 可调 Agent；Agent **不能**改 Workflow definition / 造 Task / 改 state。

## 模板（§三十七/§七十二）

内置少量固定模板：`molecule_modification`（search → compare/evidence → modify）· `route_study`（search → route → reason）· `evidence_review`。**Task 集合必须来自显式模板或显式用户列表**——AI 无限生成被严格禁止（架构测试钉住：`task_workflow.py` 不含 `self._llm`/`EvidenceAgent(`/`ModificationAnalysisService(`）。

## 执行（§二十一–§二十三/§四十三–§四十五/§四十六–§四十九）

- **创建不执行**：只生成 `PENDING`→`READY` 任务（根任务才 READY）。
- **start** → `RUNNING` + `WORKFLOW_STARTED`，返回 ready 任务，**不**自动跑。
- **execute_task**（显式 `POST .../tasks/{id}/execute`）才执行**一个**任务：`task_type → 对应 service → 结果 → artifact（绑定 task）→ COMPLETED`。
- **不自动无限执行**：无 `while`、无自动 Task 2/3/4。
- Executor **直接**调 service（`ROUTE_EXPLORATION → RouteExplorerService`），**不**绕 Agent Tool（§四十七）；AI 任务直接调 6.2/6.3/6.4，继承只读/有界（§四十八/§四十九）。

## BLOCKED vs FAILED（§三十三/§六十）

- **缺输入 / 引用不存在** → `BLOCKED` + `TASK_INPUT_NOT_FOUND`（`input_refs` 必填键缺失，或 `molecule_ids`/`reaction_template_ids`/`paper_ids` 不解析）。
- **工具/provider 出错**（如无 AI key、无 literature service）→ `FAILED`。

## Retry（§三十一/§三十二）

`FAILED` 可重试，`attempt_count < MAX_TASK_RETRIES=2`，否则 `TASK_RETRY_LIMIT`。**不复制 artifact**：第一次失败无 artifact；成功的那次才保存；失败历史走 `StudyEvent`。

## Completion（§三十五/§三十六）

全部任务 `COMPLETED`/`SKIPPED` → `WORKFLOW_COMPLETED`；任一 `FAILED`/`BLOCKED` → `BLOCKED`（**不**算完成）。`SKIPPED` 必须记 `TASK_SKIPPED` + reason。

## Resume / Cancel（§三十/§四十五）

`PAUSED → resume → READY`；**不重跑 COMPLETED**。`CANCELLED` 终态，未完成任务 → `CANCELLED`，之后不可执行（`INVALID_WORKFLOW_STATE`）。

## Timeline（§二十四/§六十二）

复用 `StudyEvent`（无平行 execution model）。新增事件类型（additive）：`WORKFLOW_CREATED/STARTED/PAUSED/RESUMED/COMPLETED/CANCELLED/BLOCKED` · `TASK_CREATED/STARTED/COMPLETED/FAILED/BLOCKED/SKIPPED`。

## Study ≠ Workflow 状态（§五十一/§五十二）

`Study ACTIVE` + `Workflow PAUSED` 合法；两者独立，只有用户显式才完成 Study。

## 不要做（§六十七/§七十/§七十一/§七十三）

Workflow Completed **≠** "research verified"（无 "Validated Study"）· **不**生成 report / PDF / summary（7.3）· **不**做 Final Audit（7.4）· **不**自动研究循环。

## API

```text
POST   /api/research/sessions/{study_id}/workflows   {template|tasks, title?, params?}
GET    /api/research/sessions/{study_id}/workflows
GET    /api/research/workflows/{workflow_id}          → workflow + tasks
GET    /api/research/workflows/{workflow_id}/ready
POST   /api/research/workflows/{workflow_id}/start | pause | resume | cancel
POST   /api/research/workflows/{workflow_id}/tasks/{task_id}/execute
POST   /api/research/workflows/{workflow_id}/tasks/{task_id}/skip {reason}
```

路由声明在 `/api/research/{job_id}` **之前**（否则被贪婪 path 吞掉）。错误映射：`WORKFLOW_NOT_FOUND`/`TASK_NOT_FOUND` → 404；`WORKFLOW_CYCLE`/`INVALID_WORKFLOW_STATE`/`TASK_NOT_READY`/`TASK_RETRY_LIMIT` → 409；`WORKFLOW_LIMIT_REACHED` → 422；其余 → 400。

## UI

`#/sessions/{id}` 内「Workflows」区：选 template + params JSON 新建 → workflow 列表 → 打开显示任务表（type/status/artifacts，Execute/Skip 按钮）与 Start/Pause/Resume/Cancel。

## 测试

`tests/research/test_task_workflow.py`（21）：模板建 workflow、需 template/tasks、依赖图正确、**循环拒绝**、超限拒绝、ready 解析、执行一步使后继 READY、确定性任务→artifact（绑定 study）、AI 任务（fake LLM）、evidence/route 任务、literature 无服务 → FAILED + workflow BLOCKED、缺输入 → BLOCKED、pause/resume + 状态守卫、cancel 终态、完成诚实、skip 记 reason 且完成、创建不执行、时间线生命周期、API（含 409/404）、全部内置模板有效。

架构测试 +2：`services/task_workflow.py` 不 import llm/agent/providers/httpx；模块不含 LLM/agent/service 构造（task 只来自模板/用户）。

## 不越界

Research Report（7.3）· Final Audit（7.4）· 自动研究循环 · AI 造 task。

**Stage 7.2 completed.**

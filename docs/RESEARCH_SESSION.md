# Research Session（Stage 7.1）

## 目标

把一次实际的化学研究过程组织成一个**可持久化、可恢复、可审计、可继续、不复制
基础化学事实**的 Research Session：

```text
Research Session
├── Question
├── Molecules  (references)
├── Reactions  (references)
├── Evidence   (references)
├── Literature (references)
├── Artifacts  (ModificationAnalysis / RouteReasoning / AgentRun / …)
└── Timeline   (events)
```

**Research Session 是研究过程容器，不是第二套 Chemistry Database。**

## 命名说明（重要）

项目**已有** `core.models.session.ResearchSession`（Phase 3.3）——那是**一次
workflow run 的 transcript**，存于既有 `research_sessions` 表。本阶段的
"Research Session"是**不同的对象**（用户研究过程的持久容器）。

为避免**两个同名模型**（即用户明令禁止的「第二套模型」），本阶段的对象命名为
**`ResearchStudy`**（`core/models/research_study.py`），存储于新增的
`research_studies` / `research_study_references` / `research_study_events` /
`research_study_artifacts` 表，**未**触碰既有 `research_sessions` 及其链路。

## 定位（§一/§三/§五十五）

`Chemistry Tool First, AI Second`。Session 只做**研究过程记录**，不是新的
Molecule/Reaction/Evidence 数据库。`ResearchSession(Study)` ≠ `AIContext`：前者
持久保存研究过程，后者是某一次 AI 调用的结构化输入——**不合并**。

## Session 模型（§四/§五/§六/§七）

```text
ResearchStudy
├── study_id
├── title / question（session 的**主问题**，非全部输入，§八）
├── status: ACTIVE | PAUSED | COMPLETED | ARCHIVED
├── created_at / updated_at（UTC，§四十九）
└── start_molecule_id / target_molecule_id（可空）
```

`ACTIVE` **不**等于「Agent 正在运行」（§六）：Session 是容器，Agent Run 是其中
一次事件。**不是** chat history（§七）。

## Reference vs Artifact（§十三/§三十三）

- **Reference**：session 正在研究**已有对象**（molecule / reaction / literature /
  evidence）——只存**引用**，不复制内容。
- **Artifact**：session 中**产生的研究结果**（`MODIFICATION_ANALYSIS` /
  `ROUTE_REASONING` / `AGENT_RESULT` / `GRAPH_SNAPSHOT` / `ROUTE_EXPLORATION`），
  存 `content`（结构化结果）。Artifact **不**替代领域模型（§十六）：它引用/保存结果。

存储策略（§十七）：可由已有数据库重建的 → 存引用 + 参数；一次性的 AI reasoning →
存结构化结果。**不**存 system prompt / chain of thought（§十八/§六十七）。

## 生命周期（§四十七）

```text
ACTIVE → PAUSED / COMPLETED / ARCHIVED
PAUSED → ACTIVE / COMPLETED / ARCHIVED
COMPLETED → ARCHIVED（不可回 ACTIVE）
ARCHIVED → （终态）
```

非法转换 → `INVALID_STATUS_TRANSITION`。`question` 创建后**固定**，`update` 不
无痕覆盖（§二十七）。每次 reference add/remove、event append、status/title 变更 →
`updated_at` 刷新（§四十八）。

## 引用完整性（§四十/§六十三）

`add_reference` 校验对象**已存在**：molecule → `ChemicalEntity`；reaction →
`ReactionTemplate`；literature → `Paper`；evidence → `ReactionEvidence`。不存在 →
`REFERENCE_NOT_FOUND`，**绝不自动创建**。`remove_reference` 只删引用，**不**删源对象
（§三十二）。Session 不存在 → `STUDY_NOT_FOUND`，不自动创建。

## 时间线（§三十四/§三十五/§三十六/§六十五）

`StudyEvent`（append-only，无 PATCH）：`event_id / event_type / at / summary /
reference_id / artifact_id / note`。排序 `at, position`——**确定性**且同秒内保持
追加顺序（**非** DB natural order）。**不**存 chain of thought。

`StudyEventType`：`STUDY_CREATED / TITLE_CHANGED / STATUS_CHANGED /
{MOLECULE,REACTION,EVIDENCE,LITERATURE}_ADDED / REFERENCE_REMOVED /
TRANSFORMATION_SEARCH / ROUTE_EXPLORED / MODIFICATION_ANALYZED / ROUTE_REASONED /
AGENT_RUN / NOTE_ADDED`。事件由**领域操作**自动创建；`NOTE_ADDED` 是用户笔记，
**不**由 AI 自动产生（§六十）。

## 不做（§九/§十/§三十九 边界）

7.1 **只**建 Research Session。**不**做 workflow engine / automatic transitions /
planner（7.2）· **不**做 report / markdown / PDF（7.3）· **不**做自动上下文注入
（打开 session 不自动把所有历史塞进 LLM prompt）。

## Persistence（§四十二/§四十四）

复用 `SQLiteStore` / `KnowledgeBase`，新增 4 张表（additive，不与既有表冲突）：
`research_studies` / `research_study_references`（按 position 排序）/
`research_study_events`（按 at,position 排序）/ `research_study_artifacts`
（`content_json`，scrub 后存）。Session **不拥有**源对象。

## API

```text
POST   /api/research/sessions                      {title, question, start_molecule_id?, target_molecule_id?}
GET    /api/research/sessions?status=&limit=&offset=
GET    /api/research/sessions/{study_id}           → study + references + events + artifacts
PATCH  /api/research/sessions/{study_id}           {title?, status?}
POST   /api/research/sessions/{study_id}/references {kind, object_id, label?, note?}
DELETE /api/research/sessions/{study_id}/references/{reference_id}
POST   /api/research/sessions/{study_id}/events     {event_type, summary?, note?}
POST   /api/research/sessions/{study_id}/artifacts  {artifact_type, content, label?}
```

路由**声明在** `/api/research/{job_id}` **之前**，避免贪婪 path 参数吞掉
`/sessions`；既有 `/api/research`（research job）路由不受影响（测试钉住）。
错误：`STUDY_NOT_FOUND`/`REFERENCE_NOT_FOUND` → 404；`INVALID_STATUS_TRANSITION`
→ 409；不合法输入 → 400。**无** session 删除端点（§四十一），用 `ARCHIVED`。

## UI

`#/sessions`（列表 + 新建）/ `#/sessions/{id}`（详情）：状态按钮（Pause/Complete/
Archive）、References（可点进 Reaction / Literature Detail）、**Research Timeline**、
note 输入、Artifacts 列表。

## 测试

`tests/research/test_research_study.py`（13）：创建/读取/列表、未知 session 报错、
状态转换（允许 + 阻止）、question 不被覆盖、created_at 固定、引用增删（删引用不删
对象）、缺失引用被拒、时间线时序 + 追加、确定性、artifact 存结果 + 不存 prompt、
重开持久化、API（含 404/409 与 jobs 路由不被吞）。

## 不越界

不做 workflow / report / final audit（7.2–7.4）· 不建 AI/Agent 表以外的知识表 ·
不删源对象 · 不做自动上下文注入。

**Stage 7.1 completed.**

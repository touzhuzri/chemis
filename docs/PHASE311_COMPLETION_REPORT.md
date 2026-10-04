# Phase 3.11 Completion Report — Persistent Knowledge Base

> **Final status: Phase 3.11: PASS**

## 1. Phase Goal

把 CREA 从 "Research Agent" 推进到带持久知识的研究系统：第一次研究得到的
Entity / Paper / Evidence / Reaction / Route / Session，在进程结束后重新打开
SQLite，仍能被确定性查询；第二次研究复用第一次的知识，不产生错误重复、
不丢失 provenance。纵向闭环优先（§112）：

```text
CREATE → PERSIST → CLOSE → REOPEN → QUERY → REUSE
```

明确不是数据库重构（§二）：Repository Protocol / InMemory 全部保留，
SQLite 只是第二个 adapter。明确禁止（§三）：Neo4j / RDF / SPARQL / 图数据库 /
embedding / 向量库 / PostgreSQL / Redis / Elasticsearch。全程未引入。

## 2. 复用的既有架构（零重设计）

- **Repository Protocol**：`ChemicalEntityRepository`（3.8.1）与
  `ReactionRepository`（3.9）本就是设计好的 swap point，SQLite 直接实现
  它们（`SQLiteChemicalEntityRepository` / `SQLiteReactionRepository`），
  未新建重复 abstraction；InMemory 实现原地保留。
- **Domain models** 全部原样：ChemicalEntity / ChemicalMention / Paper /
  Evidence / Reaction(+Component) / CandidateRoute(+ReactionStep) /
  RouteStepReactionLink / ResearchSession。
- **Identity / Mention / Evidence / Reaction-key / RouteValidation /
  RouteEvidence / ReactionBuilder / AiZynthFinder** 封板未动（§101）。
  仅两处 persistence-compat 最小修改（§9），均附回归测试。
- RDKit 仍只在 `crea.chem`；结构查询经 chem abstraction，SQLite 只筛
  candidate（§33）。

## 3. 新增模块

```text
src/crea/repositories/
├── errors.py       RepositoryError / NotFound / Duplicate /
│                   ConstraintViolation / TransactionError / SchemaError
│                   (+UNSUPPORTED_SCHEMA_VERSION)；sqlite3 错误永不外泄（§59）
└── sqlite.py       SQLiteStore：schema v1、事务、七类知识对象读写、
                    JSON 确定性序列化；两个 Protocol adapter（§4）
src/crea/services/knowledge_base.py   KnowledgeBase facade（§26）
```

## 4. Schema（v1）

`schema_meta.schema_version = 1`；`initialize()` 幂等
（CREATE TABLE IF NOT EXISTS）；异版本 → `SchemaError`
`UNSUPPORTED_SCHEMA_VERSION`，绝不静默改库（§7/§8）。迁移入口预留
（schema version + registry 结构），未引入 Alembic。

17 张表：`entities` / `entity_names` / `entity_external_identifiers` /
`papers` / `evidence` / `mentions` / `paper_mentions` / `reactions` /
`reaction_components` / `reaction_evidence` / `routes` / `route_steps` /
`route_reaction_links` / `research_sessions` / `research_session_{entities,
mentions,reactions,papers,evidence,routes}`。核心关系结构化（component
保留 role/mention_id/entity_id/identity_key，§14；session 用关系表记录
"本次研究用了哪些知识对象" 而非复制，§25）；嵌套对象走确定性 JSON
（sort_keys、固定 separators、UTF-8，§57）。FK 全程
`PRAGMA foreign_keys = ON`（§53/§89）；无 WAL（单进程工作负载，回滚日志
更简单正确，§54）。业务 ID 全部沿用领域层；INTEGER rowid 仅实现细节
（§12）。无 pickle、无 RDKit 对象（§34）。

**反应去重约束（§15 的语义判定）**：`reaction_key UNIQUE` 服从既有 key
语义——同一 key 在两个 paper 是两个反应、name-only material 不得因同名
合并。故唯一约束实为 **(reaction_key, paper_scope) 上仅对 identity-based
key 生效的 partial unique index**；name-tainted key 自由共存、永不合并。
`reaction_id` 本身 UNIQUE 承担同报告重注册的合并。

## 5. Identity / Dedup（按既有语义，不改判定）

| 对象 | 规则 |
|---|---|
| Entity | identity_key UNIQUE；重注册返回 CHEM-00000001 并 merge names/external/provenance（§11/§92）；reopen 后新身份续号不撞旧 id（§12） |
| Reaction | 同 id 合并；identity-based key + 同 scope 合并；name-tainted 不合并；同 key 异 paper 保留两个（§15/§21/§93）；合并只追加 evidence/notes |
| Evidence | evidence_id UNIQUE + **不可变**：重写同 id 不改 raw_quote（§19/§48） |
| Mention | scope+locator+text 的 content_hash；compound 7 论文局部（§20/§21） |
| Paper | DOI 主身份（既有 literature merge 语义）；NO_DOI 各自保留；无 title+year 合并（§17）；merge_conflicts 只增不减（§94） |
| Session | 全部保留、不覆盖（§44） |

Append-only（§91）；无删除 API（§90）；状态不由 DB 修改（§49）；
LLM 永不直接写库（§107：proposal → validator → domain object → repository）；
查询只读（§108）。

## 6. Query / 关系（确定性）

排序一律 `created_at + stable id`（§29）；无 best/recommended/概率；
返回 domain model（§95）。实体四通道查询；name search 全量返回且不创建
（§30/§31）；`search_reactions` 至少一个过滤条件否则 INVALID_QUERY（§32）；
exact/substructure 经 `crea.chem`（§33）；`get_papers_for_entity` 走
Paper→Mention→Entity 中间层（§65）；`get_reactions_for_paper`（§66）；
`get_evidence_for_reaction` 只返回真实 evidence refs（§67/§77）；
`get_routes_for_reaction` 只返回已有 link，不临时生成（§68）；
`get_routes_for_entity` 返回 `{route_id, relation_role, step_id}`
（target/reactant/product/participant，§69）。

## 7. 事务

每个 `save_*` 是一个事务单元；`persist_research_result(...)` 把一次研究
（entities/mentions/papers/evidence/reactions(+components+links)/routes/
links/session）作为**一个事务**提交（§51/§52）；单个 Reaction 的内部关系
原子。中途 FK 失败整体回滚，不留半个 Reaction（§79）。

## 8. ResearchWorkflow / Agent / CLI 整合

- `ResearchWorkflow(..., knowledge_base=None)`（§36）：`run()` 兼容，
  新增 `paper_id`/`doi` 显式论文归属（caller 断言，不猜测）。kb 到位时
  registry 为持久 registry（一个 stack 一个 registry，身份/反应/工作流共享），
  run 结束 `persist_research_result` 一次提交。held Evidence 先入库再注册
  Reaction（citation FK 语义）。
- `search_literature` 增 `include_records`（默认关）：工作流取得完整
  `Paper` 领域对象入库；agent 摘要路径不受影响。
- Agent 工具（§60）：新增 `get_entity` / `search_entities` /
  `get_papers_for_entity` / `get_evidence_for_reaction` /
  `get_routes_for_entity`；3.10 已有的 `get_reaction` / `search_reactions`
  **扩展实现、未重复注册**——有 kb 时并入既有知识（§61："以前研究过哪些
  反应" 从知识库回答，不重搜互联网）。
- CLI：`research --db`（缺省 `data/crea.db`，相对 cwd，Windows/Linux 兼容，
  绝无硬编码盘符）；`kb entity|reactions|papers|reaction|evidence|session|
  sessions|export`，`--json` 机器可读（§38-§40）；exit 0/1/2。
- 报告零改动：Evidence/Provenance/Source 语义不因入库丢失（§47）；
  report 不 import sqlite3（架构测试钉住）。

## 9. 最小兼容性修改（§101，均附回归测试）

1. **Entity id 跨 reopen 续号**：`ChemicalEntityRegistry._allocate_entity_id`
   同时看仓库已用 id——否则重开库后新身份会撞号/复用旧号。分配顺序语义不变。
2. **merge 后落库**：`register_resolution` 合并路径补 `repository.add(existing)`，
   `ReactionRegistry._merge` 补 `repository.add(keep)`（`add` = 该对象状态的
   upsert）；InMemory `add` 改为幂等索引。内存语义不变。
3. **Session id 不再秒级撞号**：`hash(task_id|utc_now_iso)` 在同一秒内同任务
   两次运行会同 id 覆盖（§44 禁止覆盖）。id 载荷加入 `time_ns`。知识对象
   （CHEM/RXN/EVIDENCE）id 仍完全确定性；session 本就是"某一时刻的誊本"。
   回归测试即 E2E-2（同任务连跑两次 → 两个 session）。

另：`build_offline_workflow_stack` 现在把 entities/reactions 同时传给
`build_registry` 与 `ResearchWorkflow`（此前身份工具注册进无人读取的私有
registry）——一个 stack 一个 registry。

## 10. 测试

新增 44 个（1317 → 1361，50 skipped 不变）：

- `tests/unit/test_repositories_sqlite.py`（18）：schema init/version
  （UNSUPPORTED_SCHEMA_VERSION 拒绝且不改库）、entity CRUD/dedup/reopen/
  续号、name search 不创建、evidence 去重+不可变、mention scope、paper
  DOI/NO_DOI/conflict 保留、reaction 原子保存、dangling FK 回滚（§79）、
  key 语义（name-tainted 不合并/异 paper 分离）、route/link 往返、
  session 不覆盖、排序确定性、JSON 稳定、closed store 拒绝、FK 强制。
- `tests/unit/test_knowledge_base.py`（9）：facade 查询、INVALID_QUERY、
  Paper→Mention→Entity、evidence 只返回真实 refs、routes_for_entity 角色、
  `persist_research_result` 原子性（失败零残留）、export 确定性+无 secrets、
  registry-over-store 复用。
- `tests/integration_offline/test_knowledge_base_e2e.py`（9，全真实代码路径）：
  **§73** persist/close/reopen/query 六类对象（本阶段最重要 E2E）；
  **§74** 同 PDF 两跑不翻倍、session 累积；**§75** 增量 A→A+B 不覆盖；
  **§76** compound 7 论文局部性过库不丢 scope；**§77** 证据引用重开可取、
  无引用不借 support；**§78** RouteStep↔Reaction link 恢复且不伪造；
  **§79** 坏 commit 全回滚；**§80-§83** 真实子进程 CLI research→kb 查询→
  再研究复用（含 export 校验无重复）；**§60/§61** agent 知识工具回答存量
  知识、name search 无过滤 INVALID_QUERY。
- `tests/architecture/test_layering.py`（+3，§84/§85）：core 禁 sqlite3；
  repositories 禁 LLM/RDKit/agent/sources/providers；knowledge_base 禁
  provider/LLM；report 禁 sqlite3、agent 禁 store（允许 §59 错误词汇）。
- 既有 CLI 测试改为在 `tmp_path` 下运行（缺省 `data/crea.db` 被真实练习，
  但绝不污染仓库/真实数据库，§37）。

```
pytest -q: 1361 passed, 50 skipped, 0 failed
```

基线开始前已确认：`1317 passed, 50 skipped, 0 failed`（§102）。

## 11. 架构图（最终）

```text
Research Agent
      │  tools (get_entity / search_reactions / … §60)
      ▼
Research Workflow ── knowledge_base=None → 纯内存（旧行为不变）
      │              knowledge_base=kb   → 持久 registry + 原子提交
      ▼
Chemical Research Knowledge（validated domain objects, §107）
      │
      ▼
Persistent Knowledge Base (KnowledgeBase facade)
      │
      ▼
Repository Protocol ──► SQLiteStore (schema v1) ◄── InMemory（保留）
      │
 ┌────┼────────────┬─────────────┬──────────┐
 ▼    ▼            ▼             ▼          ▼
Entity Paper    Evidence      Reaction    Route ── RouteStep↔Reaction
 │      │          │             │          │
 └──────┴──────────┴─────────────┴──────────┘
                    │
                    ▼
            ResearchSession（关系表指向上述对象，§25）
                    │
                    ▼
            Future Research（REUSE：同 identity 同 id，不重复，不丢 provenance）
```

## 12. 新增能力

- SQLite 持久知识库：七类知识对象、schema version、事务、FK、append-only
- `KnowledgeBase` facade + `persist_research_result` 原子研究提交
- ResearchWorkflow 持久化整合（knowledge_base 注入 + 论文归属 + 复用）
- Agent 知识查询工具（§60 全清单）+ "以前研究过什么" 语义（§61）
- CLI `--db` + `kb` 查询命令 + 确定性 `kb export`
- 跨进程复用：同 PDF 重研究不翻倍；增量研究累积；compound 7 scope 过库保持

## 13. Remaining limitations

- `get_routes_for_entity` 的结构关系是比较 identity 层 canonical SMILES
  字符串，不是结构检索（§33 语义边界）
- Reaction 未存 ReactionRecord 的 conditions 字段（conditions 在 record/
  evidence 引文中）；跨论文条件冲突保持分反应/分引用，不做平均行（§63/§64）
- 会话不存 task.report/task.evidence 副本（§25/§106）；报告可另存 Markdown
- 单进程单连接；无并发多用户（§53 明确不做）
- Import 未实现（§72 允许）；无图数据库/embedding（§三 明令，也是建议）

## 14. blocking issue

**无。** 过程中修复（均为接口缺陷/新能力必需，非顺手重构）：
extraction→reaction 注册前先落 held Evidence（citation FK 写序）；
`KnowledgeBase.list_reactions` 补齐；测试夹具三次撞 identity key / 同 key
反应（夹具问题，产品按设计拒绝）。

## 15. 下一大阶段建议（未执行）

- 3.12 / 4.x：报告生成可查询知识库（"KnowledgeBase query + current
  research"，§46/§47 的后续）；DECIMER 图片结构识别接 mention 层；
  ReactionRecord conditions 的独立持久化（若需要按条件检索）
- 长期：知识图谱可视化/检索（仍建议不引入图数据库/embedding）

## 16. 最终结论

**Phase 3.11: PASS**

```text
pytest -q: 1361 passed, 50 skipped, 0 failed
```

# CREA Architecture Audit — 3.x → 4.0 迁移前的代码库审计

> Stage A（§25）。**本文只审计，不改代码。** 所有判断基于对
> `src/crea/`（30,045 行 / 12 包）、`tests/`（1,142 个测试）、`docs/`
> （27 篇）、`web/`（8 个源文件）、数据库 schema（21 表）、API（25 路由）
> 的实际清点，以及对外部候选项目的实测验证（见 §8/§13 与
> docs/LICENSES.md、docs/CHEMICAL_CLIPBOARD.md）。

---

## 1. 当前架构图

```text
┌──────────────────────────────────────────────────────────────────┐
│ web/  (React+TS+Vite, 8 files)     cli.py (match/describe/research│
│   Home → Research Job → Workspace   /kb/serve/demo)               │
└───────────────┬──────────────────────────────────────────────────┘
                ▼
┌─────────────────────────────────────────────┐
│ api/ (FastAPI, 25 routes)  ·  app/ (DTO,     │
│ ResearchJob, EventLog, Research/Knowledge    │
│ Application, demo)                           │
└───────────────┬─────────────────────────────┘
                ▼
┌─────────────────────────────────────────────┐
│ agent/ (ToolRegistry 27 tools, ResearchAgent │
│ loop, live/offline 合成根, workspace)        │
└───────────────┬─────────────────────────────┘
                ▼
┌─────────────────────────────────────────────┐
│ services/ (24 模块)                          │
│  identity · entity_registry · mention_       │
│  resolution · reaction_{builder,extraction,  │
│  matching} · route_{validation,evidence,      │
│  reaction_linking} · literature* · evidence · │
│  documents · research_workflow · research_    │
│  intelligence · knowledge_{base,integrity}    │
└──────┬──────────────────────────┬───────────┘
       ▼                          ▼
┌──────────────────┐   ┌─────────────────────────┐
│ core/models (22) │   │ repositories/           │
│ entity·identity· │   │  Protocol → InMemory /  │
│ mention·evidence │   │  SQLiteStore (21 表)    │
│ reaction·route·  │   └─────────────────────────┘
│ knowledge·report │
└──────┬───────────┘
       ▼
┌──────────────────┐   ┌─────────────────────────┐
│ chem/ (RDKit 唯一│   │ providers/ · sources/    │
│ 地: normalize·   │   │ PubChem · OpenAlex ·     │
│ align·draw·frag· │   │ Crossref · Unpaywall ·   │
│ scaffold…)       │   │ DECIMER · AiZynthFinder  │
└──────────────────┘   │ (隔离 worker) · pypdf    │
                       └─────────────────────────┘
```

数据流（3.x）：`User input → ResearchJob → ResearchWorkflow（7 步）→
ResearchSession + ResearchReport v3 + KnowledgeBase(SQLite)`。

## 2. 当前核心对象

| 对象 | 位置 | 现状 |
|---|---|---|
| **ChemicalEntity** | core/models/entity.py | CHEM-id + identity_key(InChI 系) + names + external ids + provenance；是"研究产物"，不是统一化学对象 |
| **ChemicalMention** | entity.py | 论文局部提及（scope+locator+text），compound 7 语义健全 |
| **ChemicalIdentityResolution** | identity.py | RESOLVED/AMBIGUOUS/NOT_FOUND/UNAVAILABLE/INVALID；**facets 已区分 stereo/isotope/tautomer**（same_stereo/same_isotope/same_tautomer: bool\|None） |
| **Evidence** | evidence.py | content-hash id、不可变、locator v2、metadata≠experimental |
| **ReactionRecord** | reaction.py | 实验抽取中间体（raw_value/raw_unit、conflicts、NOT_FOUND） |
| **Reaction** | reaction_knowledge.py | 稳定知识对象；components(mention/entity 链)、crea-reaction-key-v1、五态 status |
| **Route/RouteStep/RouteStepReactionLink** | route.py / reaction_knowledge.py | 候选路线 + 确定性比对链接（exact/screening_only） |
| **ResearchSession/ResearchReport** | session.py / research.py | 单任务誊本 + 报告（v3 含 Existing/New/Conflict/Gap） |
| **KnowledgeState/Coverage/Gap/FieldConflict/ResearchPlan** | knowledge.py | 3.12 的知识状态/缺口/冲突/确定性计划 |
| **Paper** | literature.py | 文献模型（DOI 主身份、access 三态、merge_conflicts） |

## 3. 当前依赖关系

严格分层（架构测试钉死）：`core → chem → services → repositories →
agent → report → app → api → web`。core 不见 sqlite3/RDKit；repositories
不见 LLM/Provider/网络；chem 是 RDKit 唯一驻地；api 不见 SQL/RDKit；
agent/live.py 是唯一网络组合根。LLM 只经 LLMProvider 提议，不经事实判定。
**该分层是 4.0 可直接继承的资产**（Semantic Layer 应插入 services 之上、
core 之下或之侧，见 CREA_4_ARCHITECTURE.md）。

## 4. 可以直接保留的模块（≈60% 代码）

| 模块 | 理由 |
|---|---|
| `chem/` 全部（normalize/align/attachment/fingerprint/fragment/scaffold/draw） | RDKit 化学正确性资产，4.0 的 molecular_graph/descriptors 直接依赖 |
| `core/models/{identity,entity,evidence,literature}.py` | facets/不可变证据/文献访问语义全部正确；ChemicalEntity 4.0 是**扩展**不是重写 |
| `core/models/mention_resolution.py` + `services/mention_resolution.py` | §13 要求保留；compound-N 作用域语义经多轮 E2E 验证 |
| `sources/{crossref,openalex,unpaywall,http,pdf}.py` + `services/{literature,literature_merge,document_*}.py` | §11 明确保留；fail-closed 访问政策、metadata≠fulltext 全在此 |
| `services/evidence.py` + evidence policy + provenance | 证据/来源/溯源核心 |
| `providers/{pubchem,decimer,retrosynthesis,worker_retrosynthesis,aizynthfinder 适配}` | §6 要求继续；隔离 worker 模式是所有重工具的模板 |
| `repositories/sqlite.py`（21 表、FK、append-only、确定性查询） | §12：SQLite 暂留；KnowledgeBase → Chemical Knowledge Store 只是**重新定位** |
| `agent/tools.py` + ToolRegistry 机制 | 稳定工具接口的雏形（§7 的 Tool Layer 可在其上扩展） |
| `llm/` + `engine/`（OpenAI-compatible + worker 协议） | LLM 边界与隔离执行 |
| `report/research.py` 的渲染内核 | §17 降级为 Artifact 生成器，内核保留 |
| `app/jobs.py` 的线程/取消/重启语义 | §16 降级为 background job，机制保留 |

## 5. 应重构的模块

| 模块 | 问题 | 4.0 方向 |
|---|---|---|
| `core/models/entity.py` 的 ChemicalEntity | 只有 smiles/inchi/inchikey 字符串 + names；无 representations 集、无 molecular_graph 语义、无 uncertainty 等级 | §4 一级对象：identity(策略显式) + representations(MOL/SDF/…) + chemical_semantics(graph/rings/scaffold/FG/fragments) + 逐字段 provenance |
| Identity 判定输出 | basis 是枚举（EXACT_STRUCTURE…），但**缺 §21 的证据等级**（EXACT/SUPPORTED/PROBABLE/AMBIGUOUS/UNRESOLVED） | 身份判断 = 身份 + 置信等级 + 证据 |
| `services/identity.py` 名称解析 | 仅 PubChem provider 查名；系统命名（IUPAC）无确定性解析器 | 接 OPSIN（实测 MIT，见 §8）；PubChem/CIR 作 identifier↔representation |
| `services/research_workflow.py`（~700 行） | 是**顶层控制器**：固定 7 步、write-through、报告组装混在一起 | §15 降级为 agent capability（research_literature/reaction/route/synthesize_evidence） |
| `agent/toolsets.py`（27 工具单文件 ~1500 行） | 工具定义/实现/DTO 混居；部分工具直接绑 workflow 概念 | §7 拆 Chemistry Tool 层：稳定接口 + adapter 注入 |
| `app/` DTO/SessionDetail | 围绕 ResearchJob/Session 形状 | 围绕 Workspace/Conversation/Artifact 形状 |
| `web/src/pages/{Home,Workspace}.tsx` | 围绕 Research Home→Job→Report | §18 Chemical Workspace（对话 + 当前对象 + 图 + 证据） |

## 6. 可以降级为 Tool / Adapter 的模块

- **ResearchWorkflow** → agent 的 `research_*` capability（§15）
- **ResearchJob + EventLog + 取消/重启** → 通用 `start_background_job`（§16）
- **ResearchReport v3** → `Artifact` 生成器，用户点名才生成（§17）
- **AiZynthFinder worker** → `retrosynthesize` tool adapter（现状已是 adapter，保持）
- **DECIMER worker** → `image_to_structure` 输入 adapter（§6：输入适配器，不是身份系统）
- **PubChem/OpenAlex/Crossref/Unpaywall** → `search_pubchem` / `search_literature` tools（现状已近似）
- **ReactionExtractor（LLM 提议 + 政策）** → `extract_reaction` tool（保留政策校验）
- **KnowledgeBase facade** → `knowledge_graph` 查询工具集

## 7. 应废弃的模块（或仅存于兼容壳内）

| 模块 | 处置 |
|---|---|
| ResearchWorkflow 作为顶层控制流 | 废弃其"产品主入口"地位；内部步骤函数保留为 capability |
| ResearchHome / "创建 Research Job" 产品交互 | 废弃；Workspace 取代（§14/§18） |
| `agent/offline.py` 的 fixture 文学/合成演示栈 | 降为测试夹具与 demo；不作为产品路径 |
| `agent/loop.py` 的逐步脚本式 ResearchAgent（prompt/action 循环） | Stage F 用真正 tool-calling agent 取代；工具层复用 |
| `report/render.py`（v1 中文旧报告） | 无调用方价值时删除（现仅 legacy CLI 路径） |
| session.py 中"session 不是记忆"的限制性设计 | 概念升级为 Workspace 上下文（不是用户画像记忆，是工作记忆） |

## 8. 重复实现

1. **PubChem 双份**：`providers/pubchem.py`（CompoundProvider）与
   `sources/pubchem.py`（历史遗留源）——后者应收编或删除。
2. **名称归一化多处**：mention 的 `normalized_text` 小写、reaction key 的
   `name:` 归一、entity `add_name` 去重——应统一进 Semantic Layer 的
   `naming.py`。
3. **结构相等判定两套**：`reaction_matching.compare_reactions`（结构集合
   比较）与 `chem/align`（MCS/子结构）语义相邻但接口不同；4.0 的
   `equivalence.py` 应统一出口。
4. **ToolRegistry vs Engine worker 协议**：两个"工具/能力"接口概念并行
   （agent/tools.py 的 ToolSpec 与 engine/protocol.py 的 worker 契约），
   4.0 Tool Layer 应给出单一 Tool 接口，worker 是其后端。
5. **demo/tests 夹具复制**：demo.py 与 tests 的 ScriptedModel/夹具 payload
   各有一份——可容忍，但 Stage C 应统一 demo 数据源。

## 9. 技术债务

- `toolsets.py`/`research_workflow.py` 单文件过大（>1000 行）。
- `web/src` 仅 8 文件，Workspace.tsx 承载全部面板（>700 行），需按 4.0
  信息架构重排。
- jobs.json/事件 JSONL 是旁路持久化（非 SQLite），多进程语义未定义。
- 命名漂移史：structure_input/smiles/chemical_input 多次出现过（已在
  3.x 收口归一为 structure_input，但 DTO→job→workflow 间靠约定而非类型
  链）；4.0 应用一个 `ChemicalInput` 类型贯穿。
- 测试 1,142 个中约 30% 锁定旧产品行为（见 §12）。
- pydantic/numpy 等依赖在 LICENSES.md 中仍是 UNCONFIRMED（分发前必须
  核清）。

## 10. 与 CREA 4.0 方向冲突的设计

| 冲突 | 现状 | 4.0 |
|---|---|---|
| **顶层工作单位** | ResearchJob = 一次性研究任务 | Workspace = 持续对话工作区（§14） |
| **控制流** | ResearchWorkflow 固定流水线 | Agent 按需调工具，workflow 只是能力（§15） |
| **ChemicalEntity 地位** | 研究流程的产物 | 系统统一化学对象、对话的主角（§4） |
| **化学表达统一** | 各处字符串 + 偶发 normalize | Semantic Layer 统一映射（§5） |
| **知识库定位** | "研究结果存档" | Chemical Knowledge Store / Graph（§12） |
| **Reaction 地位** | 附着于 Route/Record 的知识条目 | 一级对象 + Reaction Graph（§8/§9） |
| **报告地位** | 每次研究必产出 | 按需 Artifact（§17） |
| **身份判定** | 枚举 basis | 证据等级 EXACT/…/UNRESOLVED（§21） |

## 11. 测试中验证"正确化学行为"的（必须保留/平移）

- `unit/test_normalize.py`, `test_fingerprint.py`, `test_scaffold.py`,
  `test_fragment.py`, `test_attachment.py`, `test_align.py` —— RDKit 化学
  正确性（ortho/meta/para 区分等）
- `unit/test_identity_models.py`, `test_identity_policy.py`,
  `test_structure_resolution.py`（含 IMAGE/UNAVAILABLE 诚实语义）
- `unit/test_chemical_entity.py`, `test_mention_resolution.py`（compound 7
  作用域、ambiguity 不 first-pick）
- `unit/test_evidence.py`（不可变、id 语义）、`test_reaction_builder.py`/
  `test_reaction_knowledge.py`/`test_reaction_matching.py`（key 语义、
  name-tainted 不合并、状态诚实）
- `unit/test_literature_*` + `integration_offline/test_access_reconciliation.py`
  （metadata≠fulltext、fail-closed）
- `unit/test_repositories_sqlite.py`/`test_knowledge_base.py`（去重、
  原子性、重启）——化学身份持久化正确性
- `integration_offline/test_golden_phenylacetylene.py`（产品级身份闭环）
- `architecture/`（分层与离线守卫）

## 12. 测试中只"锁死旧架构"的（4.0 可删/重写）

- `unit/test_research_workflow.py`、`test_research_session.py` 的步骤序
  列/阶段名断言 —— 锁定 7 步流水线
- `unit/test_cli_research.py`、`integration_offline/test_cli_research_smoke.py`
  的 UX 断言（章节编号、旧入口）—— 锁定旧产品形态
- `integration_offline/test_research_*scenarios*`、PHASE310/311/312 的
  workflow 编排细节断言 —— 编排将变为 agent 能力
- `unit/test_report*.py` 中报告结构锚定（"## 8. Limitations" 等）——
  Artifact 化后重写
- `unit/test_toolsets.py` 类的注册表形状断言 —— Tool Layer 重排后重写
- golden/API 测试中的 ResearchJob 流语义 —— 保留化学断言、换工作单位

粗略比例：约 35% 断言属于 §12 类；其余为化学/语义/证据正确性，保留。

## 13. 数据库迁移风险

- 现 schema v1（21 表，FK 全、append-only、UNIQUE 语义含
  reaction-key partial index）。**风险低的路线：加表不改表**。
- 4.0 需要新增：`workspaces`、`conversation_messages`、`artifacts`、
  `chemical_representations`（entity 多表示）、`entity_semantics`（graph/
  FG/fragment 缓存）、`transformation`（反应图边）、`identity_assertions`
  （§21 等级）。全部可 ADDITIVE 增加，schema_version → 2，旧库拒绝策略
  沿用 UNSUPPORTED_SCHEMA_VERSION。
- **不迁移**的：entities/evidence/mentions/papers/reactions 表结构与语义
  几乎可原样进入 Chemical Knowledge Store。
- 风险点：(a) reaction_components.entity_id 现为硬 FK，语义层允许
  AMBIGUOUS 提及时要允许"候选 entity"关系表；(b) ResearchSession 表将
  降级为 job 记录，与 workspace 的关系要显式（job → workspace_id）；
  (c) 现唯一约束 `uniq_identity_reaction_key` 假设 key 已是身份级——4.0
  身份策略显式化后 key 推导可能升级（保留 crea-reaction-key-v1 值，
  新策略写 v2 并存）。
- 无 ORM 引入（§24），继续 repositories/protocol。

## 14. API 兼容风险

- 现 25 路由以 /api/research*、/api/sessions* 为中心 —— 与 Workspace
  模型冲突。建议**新增** /api/workspaces*、/api/conversations*、
  /api/tools*（agent 会话），旧路由标注 deprecated 并在一个版本后移除；
  不为兼容保留双写。
- /api/entities|reactions|evidence|knowledge/* 与 4.0 一致，保留并扩展
  （+ /api/entities/{id}/representations、/api/graph/...）。
- 前端唯一消费方是自带 React 端 —— 迁移窗口内同步切换即可，兼容成本低。

## 15. 前端重构范围

- 技术栈保留（React+TS+Vite，8 文件规模小，重排成本 < 重写成本的一半）。
- 交互重做（§18）：`Home/Workspace`（job 导向）→ `WorkspaceShell`：
  Conversation（主列）、Current Chemical Object（Identity/表示/命名）、
  Structure Viewer（现 SVG 管线复用）、Evidence/Literature、Reaction
  Graph（新）、Artifacts。状态管理现为 hooks+轮询——Workspace 会话模型
  下可保留轮询，SSE 留待 Stage G。
- 输入区规划：自然语言 / SMILES / name / 图片（DECIMER）/ 文件上传；
  clipboard（§19）后续。

---

# 附：CREA 4.0 外部项目评估（§6/§23，实测验证）

| 项目 | 用途 | 许可证 | 验证方式 | 结论 |
|---|---|---|---|---|
| **OPSIN** 2.9.0 | 系统命名（IUPAC）→ 结构 | **MIT**（POM 实证；社区流传的 LGPL 说法不实） | Maven Central `uk.ac.cam.ch.opsin:opsin-2.8.0.pom` 的 `<licenses>` 块 | **接入**：Java 库，经隔离 worker（AiZynthFinder 模式）暴露 `name_to_structure` |
| **NCI CIR**（cactus.nci.nih.gov） | identifier ↔ representation（name/SMILES/InChI/InChIKey/**CAS**/EINECS…） | 免 key 开放服务；**免责声明未取得**（/disclaimer 404，需人工核读） | 实测：`phenylacetylene→C#Cc1ccccc1`、`536-74-3→C#Cc1ccccc1`、names 含 CAS/EINECS | **接入为 resolver adapter**（缓存+provenance）；ToS 核读后定稿 |
| **RxnMapper** 0.4.3 | 反应原子映射 | **MIT**（wheel 内 LICENSE 实证）；**模型权重条款未核** | PyPI wheel 元数据 + LICENSE 文件 | **接入为 `map_reaction` tool**（Stage D）；权重条款核读后定稿 |
| **Open Babel** 3.1.1 | 格式互操作 | **GPL-2.0**（wheel 内完整 GPL v2 文本实证） | PyPI wheel LICENSE | **EXCLUDED**：与项目 Proprietary 许可冲突；且 RDKit 已覆盖 SMILES/InChI/MOL/SDF 互转，无增量价值（§6"不盲目加依赖"） |
| **PubChem / Crossref / OpenAlex / Unpaywall / DECIMER / AiZynthFinder** | 现状已接入 | 见 LICENSES.md 既有行 | 既有 Phase 审计 | 保留，继续 adapter 路线 |

# 附：与成功标准（§26）的差距摘要

Case 1/2（SMILES/名称 → Entity）：名称链缺 OPSIN/CIR，接入后即可；
Case 3（图片）：DECIMER 链已有（UNAVAILABLE 诚实）；Case 4-10 需要
Agent+Workspace（Stage E/F）。化学正确性底座（Case 1/3/8 的判定内核）
**已在 3.x 验证**，4.0 是组织方式的迁移，不是化学内核的重写。

---

# Stage B-1 之后的审计状态（2026-10-03）

| 审计项 | 状态 | 说明 |
|---|---|---|
| §8 PubChem 双份 | **已解决** | 定案：`crea.providers.pubchem` 为唯一上层入口；`crea.sources.pubchem` 是其传输层，架构测试禁止业务代码直接 import |
| §8 名称归一化三套 | **已解决** | 收敛到 `core/naming.py` 三个具名用途（identity/search/display），全部调用点换用，输出逐字节不变（identity key 契约有回归测试钉住） |
| §8 结构比较两套接口 | **部分解决** | 统一入口 `semantic.identity.facets_for`（IdentityFacets/measure_facets 唯一比较口）；`reaction_matching.compare_reactions` 仍是反应级 matcher，收口推迟到 Stage D（Reaction Graph） |
| §8 ToolSpec/worker 双协议 | **保持不扩大** | worker 侧新增 OPSIN 仍是 `crea-engine/1` 同一信封（架构测试钉住）；ToolSpec 与 worker 的合一推迟到 Stage C |
| §10 冲突：ChemicalEntity 地位 | **进行中** | 已加 representations + 逐字段 provenance（ADDITIVE）；完整语义字段（graph/FG/scaffold）留 B-2+ |
| §10 冲突：身份判定等级 | **已解决** | `IdentityEvidenceLevel` 五级落地并有语义测试 |
| §10 冲突：化学表达统一 | **已解决（B-1 范围）** | Semantic Resolver + Representation 模型；SMILES/InChI/InChIKey/CID/name/molblock 全部入同一实体 |
| §10 冲突：顶层控制流 | **未动** | ResearchWorkflow/ResearchJob 保持原状（§14 允许的 identity 调用方式已切换到 semantic resolver）；降级属 Stage E/F |
| §5 应重构：名称解析缺 OPSIN | **已解决** | OPSIN 隔离 worker 实证接入（MIT，实测解析 ethynylbenzene/phenylacetylene）；CIR adapter 就绪但 opt-in（ToS 未清） |
| §9 技术债务：DTO→job→workflow 字段链 | **部分解决** | `ChemicalInput`/`StructureInput` 显式分类贯通 resolver；全链 DTO 归一留 Stage E |
| 图片/clipboard 输入 | **分类已预留** | InputKind.IMAGE 已走既有链；clipboard molblock 输入经 `of_notation` 已通（CHEMICAL_CLIPBOARD.md 的实测清单仍待有 ChemDraw 的机器） |

**推迟到 Stage C+**：Reaction Graph/一级 Reaction（D）、Workspace/Conversation（E）、
Agent（F）、前端重排（G）、ToolSpec 合一（C）、reaction_matching 收口（D）、
NCI CIR 默认链启用（LICENSES ToS 清理后）。

---

# Stage C 之后的审计状态（2026-10-03）

| 审计项 | 状态 |
|---|---|
| §8 ToolSpec/worker 双协议 | **已收口**：Tool Contract → Execution Adapter → Worker Envelope 三层划清；agent 只见 Tool（bridge）；信封测试钉住不外浮 |
| §8 结构比较两套接口 | **已收口（工具面）**：`compare_structures` 四轴（identity facets / similarity screen / scaffold）单一出口；reaction_matching 的反应级收口仍留 Stage D |
| 能力调用方式多套（service/provider/worker/ToolSpec/直调） | **已统一（工具面）**：八个代表性工具覆盖主要能力；遗留 service 直调属 agent 内部编排，随 Stage E/F 迁移 |
| 其余项 | 同 Stage B-1 末表；Workspace/Agent/Reaction Graph 仍未动（D/E/F） |

---

# Stage D 之后的审计状态（2026-10-03）

| 审计项 | 状态 |
|---|---|
| §8 结构比较两套接口 | **已收口（反应级）**：reaction 身份用 `crea-reaction-key-v1`（顺序无关、不同产物不合并）；similarity 仍是 screening（§7 保持）；search_reactions 走统一 `search_results` |
| Reaction 一等对象 + n:m | **已达成**：复用既有 Reaction（零平行模型）；find_by_product/reactant/between + 闭环演示 |
| reaction_matching 的 atom mapping / transformation | **留 Stage E+**（RxnMapper ToS 未清；`atom_mapping` 字段就位、算法缺席诚实） |
| 其余项 | 同 B-1/C 末表；Workspace/Agent/前端仍不动（E/F/G） |

---

# Stage E 之后的审计状态（2026-10-03）

| 审计项 | 状态 |
|---|---|
| 能力调用方式多套（service/provider/worker/ToolSpec/直调） | **已统一（Agent 面）**：ChemistryAgent 只经 ToolRegistryLike 调能力；`describe()` 为工具 schema 单一事实来源；遗留 service 直调在旧 ResearchAgent 内，属 F 迁移 |
| 顶层控制流（ResearchWorkflow/ResearchJob 降级） | **未动**：Stage E 新增独立 ChemistryAgent，不动旧 workflow/job（§3 先审计不重写） |
| 结果词汇多套 | **保持唯一**：`AgentResult` 复用 `ToolResult`，无 Evidence2/Provenance2/ToolResult2（架构测试钉住） |
| Workspace/Conversation/多 Agent/长期记忆 | **仍留 Stage F+**（§22 明确排除） |
| `/api/agent/chat` | **留后**（§18：现有 composition root 不适合非自然改造） |
| 其余项 | 同 Stage D 末表；RxnMapper atom mapping、反应公开检索、前端重排仍留后 |

---

# Stage F 之后的审计状态（2026-10-03）

| 审计项 | 状态 |
|---|---|
| 顶层控制流（ResearchWorkflow/ResearchJob 降级） | **未动**：Stage F 新增独立 WorkspaceApplication 对话路径，不动旧 workflow/job/ResearchAgent |
| Workspace/Conversation | **已落地（MVP）**：`workspaces`+`messages` 两表、`WorkspaceApplication`、多轮上下文、HTTP API、React UI |
| Agent 与 Workspace 边界 | **已钉住**：Agent 不知 workspace（`chemistry_agent.py` 无 workspace/repository import）；app 层零化学（`app/workspace.py` 无 rdkit/llm/sqlite/tools import） |
| `agent/workspace_stack.py` 命名真实 transport | **已登记**：composition root（`COMPOSITION_ROOTS`），与 `agent/live.py` 同格 |
| 长期记忆/多 Agent/streaming/登录/文件系统 workspace | **仍留 Stage G+**（§27-§29/§32 明确排除） |
| 其余项 | 同 Stage E 末表；RxnMapper atom mapping、反应公开检索、前端重排、NCI CIR 默认链仍留后 |

---

# Stage G 之后的审计状态（2026-10-03）

| 审计项 | 状态 |
|---|---|
| 结构比较多套接口 | **保持唯一**：explorer 复用 `chem`（normalize/fingerprint/scaffold/fragment）与 identity facets；无 SimilarityEngine2/ScaffoldEngine2（§8，架构钉） |
| 第二 Agent 风险 | **已钉住**：无 ExplorationAgent/ChemistryExplorationAgent；explorer 是 services 能力，不 import agent/app/api/web/tools（§29/§30） |
| 反应证据分级 | **已落地**：DIRECT/RELATED/NO/UNAVAILABLE 四等，经 `find_between`/`find_by_component_entity`，unavailable≠empty |
| Knowledge Graph / 新数据库 / 递归探索 | **未引入**（§31/§33/§34）：ExplorationResult 即时计算不持久化，单层探索 |
| PubChem 结构相似搜索 | **未假装**：provider 无真正结构相似搜索，MVP 用本地 KB，source 诚实标记 local（§9/§28） |
| 其余项 | 同 Stage F 末表；RxnMapper atom mapping、反应公开检索、前端重排、NCI CIR 默认链仍留后 |

---

# Stage H 之后的审计状态（2026-10-03）

| 审计项 | 状态 |
|---|---|
| 第二 Reaction 模型风险 | **已钉住**：`Transformation` 引用 Stage D 唯一 `Reaction`（`reaction: Reaction`），无 TransformationReaction/Reaction2（架构钉） |
| 事实/推断边界 | **已落地**：transformation 只产出 DOCUMENTED；INFERRED 已声明未启用；similarity/scaffold/fragment 永不产生 transformation |
| no-evidence ≠ unavailable | **已钉住**：四/五种状态分开，测试钉死 unavailable≠empty、reverse≠forward、inference≠documented |
| 新数据库/新表/递归 | **未引入**（§二十六/§二十七）：Transformation 即时查询不持久化，单跳 |
| 第二个 Agent / web 依赖 | **未引入**：transformation service 不 import agent/app/api/web/tools/rdkit（架构钉） |
| 其余项 | 同 Stage G 末表；RxnMapper atom mapping、反应公开检索、前端重排、NCI CIR 默认链仍留后 |

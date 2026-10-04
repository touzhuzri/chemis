# CREA 4.0 Architecture — Chemistry-native Agent Workbench

> Stage A 设计稿。配套审计：docs/ARCHITECTURE_AUDIT.md。
> 一句话定位（§27）：**成熟化学软件负责"能力"，CREA 负责统一身份、
> 统一语义、统一工具接口、统一证据、统一 provenance、统一 Workspace 与
> Agent orchestration。**

## 1. 目标形态

```text
CREA 4.0
│
├── Workspace                    ← 用户工作单位（取代 ResearchJob）
│   ├── Conversation             持续讨论（多轮上下文）
│   ├── Chemical Objects         当前/历史 ChemicalEntity
│   ├── Documents                PDF/图片/上传
│   └── Artifacts                Report / Route set / Data export（按需生成）
│
├── Chemistry Agent              LLM：意图/规划/解释/组织讨论（§20 边界）
│
├── Chemical Semantic Layer      ← 4.0 最重要新模块（§5）
│   External Representation → 统一 ChemicalEntity
│   identity(策略显式) · equivalence · naming · mentions · normalization
│
├── Chemistry Tools              ← Agent 唯一入口（§7）
│   resolve/parse/validate/canonicalize/represent/name/analyze/
│   functional_groups/scaffold/fragments/compare/
│   search_pubchem · search_literature · search_reactions ·
│   extract_reaction · map_reaction · retrosynthesize ·
│   explore_precursors · generate_3d · image_to_structure
│
├── Chemical Knowledge Graph     ← Knowledge Store 重新定位（§8/§9/§12）
│   ChemicalEntity ↔ Reaction ↔ ChemicalEntity（DAG，非树）
│   Mention · Document · Evidence · Transformation · Workspace 关系
│
├── Evidence / Provenance        3.x 资产整体保留（§11）
│
└── External Integrations        RDKit · PubChem · OPSIN · CIR ·
                                 DECIMER · AiZynthFinder · RxnMapper ·
                                 Crossref/OpenAlex/Unpaywall（adapter，可替换）
```

核心数据流（§3）：

```text
External Chemical Representation
  → Chemical Semantic Layer → ChemicalEntity → Workspace
  → Chemistry Agent → Tools → Knowledge/Evidence/Reaction Graph
  → Agent response / Artifact
```

## 2. ChemicalEntity 4.0（§4）

```text
ChemicalEntity
├── internal_id            CHEM-XXXXXXXX（沿用）
├── identity               structural / stereo / tautomer / protonation /
│                          salt / solvate / isotope 策略显式化
│                          （复用 IdentityFacets，策略版本化）
├── molecular_graph        RDKit 图（缓存，不入库）
├── representations        canonical/isomeric SMILES · InChI · InChIKey ·
│                          MOL · SDF ·（CDXML 预留）
├── names                  IUPAC · common · 中文 · 文献局部名（带来源）
├── external_identifiers   PubChem CID · CAS · ChEBI · ChEMBL · …（带来源）
├── chemical_semantics     rings · aromatic · scaffold · functional groups ·
│                          substituents · attachment points · fragments ·
│                          descriptors（计算缓存 + provenance）
└── provenance             逐字段来源
```

**身份判定输出 = 身份 + 证据等级**（§21）：

```text
EXACT        结构完全一致（graph+stereo+isotope 按策略）
SUPPORTED    有可靠来源明确指向（如 PubChem 记录）
PROBABLE     名称/上下文高度指向但未逐项验证
AMBIGUOUS    多候选并存，绝不 first-pick
UNRESOLVED   证据不足
```

禁止（§24）：字符串相等当唯一身份判据、LLM 猜结构、similarity 当可行性。

## 3. Chemical Semantic Layer（§5）— `src/crea/semantic/`

| 文件 | 职责 | 复用 |
|---|---|---|
| `representations.py` | 各表示 ↔ entity；MOL/SDF 互转 | chem/normalize（RDKit） |
| `identity.py` | 身份策略（tautomer/protonation/salt/isotope 显式化）+ 等级 | core IdentityFacets |
| `equivalence.py` | same/different/ambiguous/unresolved 判定出口 | chem/align、reaction_matching 统一 |
| `naming.py` | 名称归一、系统命名解析 | **OPSIN（worker）**、CIR、PubChem |
| `resolver.py` | 输入（name/SMILES/InChIKey/CID/**CAS**/图片）→ entity 解析管线 | identity service、PubChem、CIR、DECIMER |
| `mentions.py` | ChemicalMention → 候选 entity 链接（same/different/ambiguous/unresolved） | 现 mention_resolution 整体迁入 |
| `normalization.py` | 归一化统一出口 | chem/normalize |
| `provenance.py` | 逐字段来源记录 | core Provenance |
| `entity.py` | ChemicalEntity 4.0 模型（core 迁移/扩展） | core/models/entity.py |

歧义原则不变：证据不足返回 AMBIGUOUS，绝不猜测。

## 4. Chemistry Tool Layer（§7）

统一 `ChemistryTool` 接口（输入/输出 DTO + 能力声明 + provenance 契约），
Agent 只见稳定 API；底层可替换：

```text
resolve_chemical_identity   parse_structure   validate_structure
canonicalize_structure      get_structure_representation  name_structure
analyze_structure           find_functional_groups   find_scaffold
find_fragments              compare_structures     search_pubchem
search_literature           search_reactions       extract_reaction
map_reaction                retrosynthesize        explore_precursors
generate_3d                 image_to_structure     research_*（§15 capability 化）
```

实现映射：RDKit（结构类）、OPSIN worker（name→structure）、PubChem/CIR
（identifier 类）、DECIMER worker（image→structure）、AiZynthFinder worker
（retrosynthesize）、RxnMapper（map_reaction）、LiteratureService
（search_literature）、ReactionExtractor（extract_reaction）。**Agent 永不
直接 import RDKit/Provider**（沿用 3.x 架构守卫）。

## 5. Reaction 一级对象 + Reaction Graph（§8/§9）

```text
Reaction
├── reactants / products : [StoichEntity]   （n:m，不假设 2→1）
├── reagents / catalysts / solvents
├── conditions（raw_value/raw_unit 保留）· yield · reaction class
├── atom_mapping（RxnMapper，缺失=未做，不猜）
├── source（Paper/Document）· evidence[] · provenance
└── transformation（图边语义：A+B→C）
```

Graph：`ChemicalEntity ↔ Reaction ↔ ChemicalEntity` 的 **DAG**——
支持 branching/merging/shared intermediates/convergent/alternative
routes。Route 退化为 Graph 上的可读投影（Artifact），不再是核心结构。

## 6. 递归前体探索（§10）

```text
Target → structural exploration（RDKit：变换/切断假设）
       → candidate predecessor（结构关系，≠可合成）
       → reaction hypothesis（transform 级别）
       → literature/reaction search（先例）
       → evidence（支持等级）
       → supported transformation / 明确 unsupported
```

五级区分（§10）：structural relationship < candidate transformation <
reaction precedent < literature-supported < validated route。相似 ≠
可合成；中间体复杂则递归；底层 DAG 不强制树。

## 7. Workspace 与 Agent（§14-§16/§20）

- **Workspace**：会话 + 化学对象 + 文档 + Artifacts 的持续工作单位；
  同一 Workspace 内"这是目标分子→找路线→换一条→查论文→出报告"连续成立。
- **Agent**：LLM 只做意图理解/工具规划/结果解释/讨论组织/提出下一步；
  化学事实判定全部走确定性工具（§20）。身份/等同/映射/文献事实/实验
  条件不由 LLM 最终裁决。
- **ResearchWorkflow**：降级为 `research_literature/research_reaction/
  research_route/synthesize_evidence` capabilities（§15）。
- **ResearchJob**：降级为 `start_background_job`（保留线程/取消/重启/事件
  流水）（§16）。
- **Report**：Artifact，用户点名才生成（§17）。

## 8. Knowledge Store（§12）

SQLite 保留；定位从"研究报告存档"改为 **CREA Chemical Knowledge Store**：
ChemicalEntity / Reaction / ChemicalMention / Document / Evidence /
Transformation / Provenance / Workspace 关系。新增表全部 ADDITIVE
（schema v2）；不做图数据库迁移（§24）。冲突保留双方出处、append-only、
只读证据等 3.x 语义全部延续。

## 9. 分阶段实施（§25）

| Stage | 内容 | 产出 |
|---|---|---|
| **A 审计**（本次） | 只审计 | ARCHITECTURE_AUDIT.md / CREA_4_ARCHITECTURE.md / CHEMICAL_CLIPBOARD.md / LICENSES.md 增补 |
| **B Semantic Core** | ChemicalEntity 4.0 + semantic/ + OPSIN/CIR/PubChem/DECIMER 接入 | 全部输入 → ChemicalEntity；身份等级 |
| **C Tool Layer** | ChemistryTool 接口 + adapter 化现有能力 | Agent 可用的稳定工具面 |
| **D Reaction Graph** | Reaction 一级化 + DAG + RxnMapper | Entity↔Reaction↔Entity |
| **E Workspace** | Workspace/Conversation/Artifact 模型 + API | 持续讨论工作单位 |
| **F Agent** | LLM agent 接 Semantic/Tools/Workspace | Case 4-10 体验 |
| **G Frontend** | Workspace 信息架构重排 | §18 界面 |

每阶段独立可验收；不并行大改。

## 10. 成功标准对照（§26）

| Case | 依赖 | 现状底座 |
|---|---|---|
| 1 SMILES→Entity | B | 3.x 已验证（golden case） |
| 2 Name→Entity | B（OPSIN/CIR/PubChem） | PubChem 链已有 |
| 3 Image→Entity | B | DECIMER 链已有 |
| 4 命名问题→tools | C+F | naming tool 规划 |
| 5 查文献（带上下文） | E+F | LiteratureService 已有 |
| 6 找合成路线 | C+D+F | AiZynthFinder adapter 已有 |
| 7 换一条路线（保持上下文） | E+F | Workspace 会话 |
| 8 Compound 7a 与我的分子 | B+D+F | Mention 语义已有 |
| 9 反向前体探索 | D+F | exploration 设计见 §6 |
| 10 整理成报告 | E+F+Artifact | Report 渲染内核已有 |

## 11. 非目标（§24 禁止事项复述）

不新增第二个 ResearchWorkflow/ResearchJob 类型；不造平行 ChemicalEntity；
LLM 不猜结构；不用字符串相等判化学同一；similarity≠可行性；candidate≠
validated；不重写 RDKit/AiZynthFinder/DECIMER；不为"未来"引入新数据库；
不为旧测试保留错误架构。

---

# Stage B-1（已实施）：Semantic Spine 的落地设计

> 本节记录 B-1 实际建成的模块与规则，与上文目标架构一一对应。

## SB-1. Chemical Semantic Layer（`src/crea/semantic/`）

| 文件 | 职责 | 依赖 |
|---|---|---|
| `representations.py` | 表示集构建/解析：canonical_smiles / isomeric_smiles / standard_inchi / inchikey / molblock，各带 source；无效表示显式失败（`RepresentationError`） | chem（RDKit 唯一入口） |
| `identity.py` | 等价判断（`facets_for` 复用 measure_facets，无第二套 identity）+ 证据等级推导（`identity_evidence_level`、`evidence_for_match` 降级不升级） | services.identity |
| `naming.py` | 名称链：`NameLookupProvider` 协议 + `NameResolver`（调度/去重/歧义/证据/来源） | core.naming |
| `resolver.py` | 统一入口：`ChemicalInput`（显式分类 §7）→ `ResolutionResult`（status + entity/candidates + identity_evidence + provider_results + provenance + diagnostics） | 上述全部 |

层析规则（架构测试钉死）：`semantic` 只可 import core/chem/services；
禁 providers/sources/agent/llm/sqlite3/rdkit。providers 作为 adapter 注入
（组合根装配）。

## SB-2. Resolver 架构

```text
ChemicalInput.of(kind, value)          # kind 是数据，绝不猜测
  ├─ STRUCTURE (SMILES/InChI/molblock) → RDKit → ChemicalIdentityResolution
  ├─ IDENTIFIER (InChIKey/CID)         → IdentityResolutionService（既有）
  ├─ NAME                              → NameResolver 链
  └─ IMAGE                             → 既有 resolve_by_image（分类保留）
        ↓
  ResolutionResult { status: RESOLVED|AMBIGUOUS|UNRESOLVED|INVALID,
                     entity, candidates, identity_evidence,
                     provider_results, resolution, diagnostics }
        ↓
  ChemicalEntityRegistry（既有 mint/reuse）→ representations + 逐字段 provenance
```

关键规则：provider 结构一律经 parse→normalize 再比较（`_materialize`），
字符串相等永不是身份判断（§5）；AMBIGUOUS 永不 `candidates[0]`。

## SB-3. Identity Evidence 模型（§21）

```text
EXACT      结构记号输入经确定性解析（SMILES/InChI/molblock）
SUPPORTED  主名称或确定性标识（CID/InChIKey/…）经可靠来源建立
PROBABLE   经同义词/未记录路径；或与既有实体比对有 facet 未定（降级不升级）
AMBIGUOUS  多个相异结构并存，选择权交给人
UNRESOLVED 证据不足
```

resolution status 与 evidence level 是两个维度，始终同时报告。

## SB-4. Provider 架构（§8/§10）

```text
NameResolver
├── OpsinNameLookup       → engine worker（隔离 Java，opsin_worker.py）
├── CompoundNameLookup    → 任意 CompoundProvider（canonical: providers.pubchem）
└── NCICIRNameLookup      → sources.cir（opt-in：ToS 未清，见 LICENSES.md）
```

Provider 只回答"这个名我有什么"；identity 由 semantic/identity 判定。
一个 provider 不可用=诊断，链条继续；全灭才 UNRESOLVED。

## SB-5. 表示模型（§3）

`ChemicalRepresentation(kind, value, source, source_id, retrieved_at)` +
`field_provenance: dict[str, Provenance]`（ADDITIVE 挂在既有
ChemicalEntity 上）。多表示 ≠ 多实体；canonicalization 全部 RDKit。

## SB-6. 既有系统的接点

- `ResearchWorkflow` → `resolve_chemical_identity` 工具 →（注入的）
  SemanticResolver：§14 的逐步迁移，未动 workflow 顶层结构。
- 名称归一化收敛到 `core/naming.py` 三具名函数（identity/search/display）。
- PubChem canonical 入口 = `crea.providers.pubchem`（架构测试禁
  业务代码 import `crea.sources.pubchem`）。

---

# Stage C（已实施）：Chemistry Tool Layer

```text
Agent → ToolRegistry → ChemistryTool（唯一契约）
                         ↓
              Execution Adapter（in_process / isolated_worker / remote_provider）
                         ↓
              Service / Provider / Worker(crea-engine/1) / RDKit
```

- 唯一契约 `crea.tools.contract.ChemistryTool`：schema/capabilities/
  execution_modes/timeout/provenance_policy/invoke
- 唯一结果 `core.models.research.ToolResult`（+diagnostics/+provenance）
- 八工具：resolve_chemical · name_structure · analyze_structure ·
  compare_structures · search_chemical · search_literature ·
  search_reactions · retrosynthesize
- `as_agent_spec()` 桥接进既有 agent ToolRegistry——一套契约一座桥
- 详见 docs/TOOL_LAYER.md、docs/STAGE_C_REPORT.md

---

# Stage D（已实施）：Reaction Intelligence MVP

```text
ChemicalEntity ──reactant──┐
                           ├── Reaction（一等对象，n:m 角色桶）──product──→ ChemicalEntity
ChemicalEntity ──reactant──┘
```

- `Reaction` 已是 n:m 一等对象；ADDITIVE +`reaction_class` +`atom_mapping`
- `ReactionRepository` 扩展 `find_by_product` / `find_by_reactant` /
  `find_between`（子集语义，共同发生不拆边）
- `search_reactions` 真工具 → `ReactionSearchResult`（reaction + 匹配实体 +
  role + evidence + provenance + diagnostics）
- 详见 docs/STAGE_D_REPORT.md

---

# Stage E（已实施）：Chemistry Agent MVP

```text
                    User
                      │
                      ↓
              ┌──────────────┐
              │ Chemistry    │
              │ Agent        │   orchestration，零化学（架构测试钉住）
              └──────┬───────┘
                     │ ToolRegistryLike（注入抽象，describe() 单一事实来源）
                     ↓
               ChemistryToolRegistry
                     │
       ┌─────────────┼─────────────┐
       ↓             ↓             ↓
 resolve        analyze        compare
       │             │             │
 search_chemical  literature  reactions
       │
       ↓
 existing chemistry layer
```

- `ChemistryAgent.run(message) -> AgentResult`（answer + tool_calls + evidence
  + provenance + diagnostics + status）
- `AgentResult` ADDITIVE 进 `core/models/research.py`，复用唯一 `ToolResult`
  / `Evidence` 词汇，零平行 Result 模型
- `MAX_ITERATIONS=8` 硬顶；AMBIGUOUS 不猜 `candidates[0]`，UNAVAILABLE ≠ 空
- 依赖抽象 `ToolRegistryLike`；Agent 不 import rdkit/chem/semantic/providers/
  sources/services/repositories/tools
- 详见 docs/STAGE_E_REPORT.md

---

# Stage F（已实施）：Chemistry Workspace MVP

```text
Browser → Web UI + API
            ↓
   WorkspaceApplication (orchestration + context builder, 零化学)
            ↓
   ChemistryAgent (Stage E, 注入, 不知 workspace)
            ↓
   ToolRegistry → 真实化学工具 → evidence / provenance
```

- `core/models/workspace.py`：`Workspace` + `WorkspaceMessage`（复用唯一
  `ToolResult`，零平行消息/结果模型）
- SQLite additive：`workspaces` + `workspace_messages` 两表；`SQLiteWorkspace
  Repository` + `KnowledgeBase.workspace_repository()`
- `app/workspace.py::WorkspaceApplication`：create/list/get/messages/send_message；
  每次操作短生命周期 store；agent_factory 注入（app 层零 rdkit/llm/sqlite）
- 多轮上下文：`ChemistryAgent.run(..., history=...)`（ADDITIVE）+ 最近 N 条
  （`MAX_CONTEXT_MESSAGES=30`），硬上限无摘要
- API：`/api/workspaces` + `/api/workspaces/{id}/messages`（POST 返回
  answer+tool_calls+evidence+provenance+diagnostics，§16 不拍平）
- 前端 `web/src/pages/ChatPage.tsx`（`#/chat`）+ workspace 列表/聊天/loading/
  错误；`npm run build` 通过
- 详见 docs/STAGE_F_REPORT.md

---

# Stage G（已实施）：Chemical Exploration MVP

```text
Target → ChemicalExplorer → Candidate Structures（ChemicalSpace）
        → ChemicalEntity（identity pipeline，去重）
        → Reaction Evidence（find_between / find_by_component_entity）
        → ExplorationResult（candidates + relationship + evidence + provenance）
```

- `services/exploration.py::ChemicalExplorer`：组合现有 `crea.chem`
  （normalize/fingerprint/scaffold/fragment）+ identity/reaction seams，零重写
  RDKit（§8）；不 import agent/app/api/web/tools（§29，架构钉）
- `core/models/exploration.py`：`ExplorationResult`/`Candidate`/
  `RelationshipKind`（SIMILAR/SAME_SCAFFOLD/FRAGMENT_RELATED）/
  `ReactionEvidenceStatus`（DIRECT/RELATED/NO/UNAVAILABLE）
- 候选统一经 `IdentityResolutionService + ChemicalEntityRegistry`，去重靠
  identity_key（§10/§24）；候选源默认本地 KB，`source="local"` 诚实（§9）
- 新工具 `explore_chemical_space`（§16-§18）；Agent 仍唯一（无 ExplorationAgent，
  §30，架构钉）；prompt 增探索语义（相似度≠前体，§20）
- UI 在 Chat 内渲染候选卡片（§21/§36），无独立 explore 页、无 graph canvas
- 详见 docs/STAGE_G_REPORT.md

---

# Stage H（已实施）：Evidence-backed Transformation MVP

```text
Target ChemicalEntity + Candidate ChemicalEntity
        ↓
TransformationEvidenceService（ReactionRegistry，一跳）
        ↓
已有 Reaction（复用 Stage D 唯一模型）
        ↓
Transformation（引用 Reaction，不替代）
        ↓
证据 + provenance + diagnostics → ChemistryAgent → Workspace
```

- `core/models/transformation.py`：`Transformation`（`reaction: Reaction` 引用，
  非第二反应模型）+ `TransformationSearchResult` + `TransformationStatus`
  （direct/related/no/unavailable/invalid）+ `TransformationEvidenceLevel`
  （documented/derived/inferred）
- `services/transformation.py::TransformationEvidenceService`：find_between /
  find_transformations，走注入 ReactionRegistry 的 find_between（正向）+
  find_between（反向）+ find_by_component_entity（related）；n:m 子集继承 Stage D
- 事实/推断边界（§十）：stored Reaction = DOCUMENTED；Stage H 不产出 INFERRED
- 新工具 `find_chemical_transformations`（candidate/target 或 reactant/product
  列表）；Agent 仍唯一，prompt 增 direct≠推断/四种结果不混/reverse 明示
- ChatPage 渲染 transformation 卡片（无绿红可行着色、无 Dashboard）
- 详见 docs/STAGE_H_REPORT.md

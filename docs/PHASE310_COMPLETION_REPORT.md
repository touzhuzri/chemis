# Phase 3.10 Completion Report — Research Workflow Integration

> **Final status: Phase 3.10: PASS**

## 1. Phase Goal

把 Phase 3.9 的 Reaction Intelligence 正式接入 ResearchWorkflow / Agent /
ResearchReport，形成一次完整、可运行、可报告的闭环：

```text
用户研究问题 → Chemical Identity → Literature Discovery → Chemical Mention
→ Chemical Entity → Reaction Discovery/Extraction → Reaction Evidence
→ Retrosynthesis Candidate → Reaction ↔ Route Validation → Research Report
```

整合验收，不是基础设施扩张。纵向 E2E 闭环优先于广度（§76）。

## 2. Existing Architecture Reused

全部复用，零重设计：ChemicalIdentity / ChemicalMention / ChemicalEntity /
Evidence / Reaction / Route / RouteEvidence（封板）、ReactionExtractor（LLM 提议 +
政策裁决）、RouteValidationService（唯一 support 写入者）、RouteEvidenceService
（step 级 rung）、compare_reactions（确定性比对）、ReactionBuilder /
ReactionRegistry / RouteReactionLinker（3.9）、ResearchWorkflow / ResearchSession /
Report v2（3.7）。AiZynthFinder worker、RDKit、OpenAlex/Crossref/PubChem 均未动。

## 3. ResearchWorkflow Changes

`services/research_workflow.py`（增量，`run()` 兼容）：

- **新生命周期内相**（§4）：外部状态机保持六值兼容；内部 phase 记录于
  `session.phases`：`researching.identity → researching.analysis →
  researching.literature → researching.mentions → collecting_evidence(文档证据) →
  researching.reactions → collecting_evidence.reactions → validating.routes →
  reporting`。
- **Step 4.5 知识层接入**：文档文本 → `extract_chemical_mentions`（确定性抽取）
  → `ChemicalMentionResolver`（含 ParentheticalContext）→ `ReactionBuilder`
  （record + mentions → Reaction）→ `ReactionRegistry.register`（reaction key 去重，
  §15）。无实验文本 → 不建 Reaction，报告明示"该文献被发现，但当前没有足够实验文本
  建立 Reaction。"（§9）。
- **Step 5.5 反应↔路线**：`RouteReactionLinker.link` 确定性比对每 (step, reaction)；
  exact 链接 `stage()` 入 validator 结构池（step 级匹配）并生成 `record_links` 传给
  `route_evidence_report`——rung 由既有 RouteEvidenceService 裁决。screening-only
  链接不提升任何东西（§35/§37）。
- **Session**：run 末填充 `reaction_ids` / `chemical_entity_ids` /
  `chemical_mention_ids`（去重、确定性顺序）与 phases/status（§5/§16）。
- 构造签名增量：`documents` / `entities` / `reactions`（可注入、缺省新建）。
- ResearchWorkflow 不 import 任何 LLM（静态测试钉住，§20）。

## 4. ResearchSession Changes

`core/models/session.py` 增量字段：`reaction_ids`、`chemical_entity_ids`、
`chemical_mention_ids`、`phases`。仍是单任务誊本、非长期记忆；既有字段全保留。
Agent 的 `_finish_session` 同步填充这三个 id 列表（从 registry 汇总、去重、顺序稳定）。

## 5. Agent Changes

`agent/toolsets.py` 新增四个工具（§17，复用既有能力，不重复造）：

| 工具 | 能力 |
|---|---|
| `extract_reactions` | 文档 →（extraction model 提议 + 政策校验）→ ReactionRecord → mention/entity 解析 → Reaction → registry。无实验文本 → `discovery_only`（§9/§19） |
| `get_reaction` | 读单个 reaction：组分、entity、状态、证据 |
| `search_reactions` | §34 过滤式查询（entity/product/paper/doi/key/structure），无过滤条件 → `INVALID_QUERY`；结构检索 exact/substructure 经 `crea.chem` 抽象（§33） |
| `validate_reaction` | link 精确比对 + stage + `RouteValidationService.validate/apply` 裁决；非 exact → screening only，不提升（§37/§57） |

Agent 不能凭 LLM 断言建 Reaction（§19）：唯一路径 Evidence → 提取政策 → identity 层
→ Reaction；工具层无任何绕过。

## 6. CLI Changes

`crea research`（workflow 形式）经 ResearchWorkflow 自动产出 Reactions 节与
session 记录；`--save` JSON 含 reactions。新增 `crea.chem.align.substructure_contains`
（§33，RDKit 仍只在 chem）。CLI 仍是入口：parse → build task/input → invoke →
render → output。

## 7. ResearchReport Changes

- `ResearchReport` 增量字段：`reactions`、`reaction_links`（additive）。
- Report v2 新增 `## Reactions` 节（§71 标签体系）：`[REACTION]`（结构化反应）、
  `[EXPERIMENTAL]`（实验支持）、`[EVIDENCE]`、`[UNRESOLVED]`（未解析组分/引用）、
  `[PARTIAL]`（缺口）、`[DISCOVERY]`（仅发现无实验文本）；Retrosynthesis 节输出
  RouteStep↔Reaction 链接（exact match / screening only）。既有编号节不变（零测试
  破坏），Reactions 以独立节插入。
- Limitations 增量：`no reactions established`、`reaction partially resolved`、
  `no reaction is experimentally supported`、`no route step is linked to a
  literature reaction`。
- 顺序确定性（§65）：reaction 按 `(paper_scope, reaction_id)` 排序。

## 8. 测试与 E2E

`tests/integration_offline/test_research_workflow_integration.py`（6 个，全为真实
代码路径——workflow.run / ToolRegistry.execute，无手工拼对象）：

1. **完整 E2E**（§10/§69/§70）：PDF→Document→Evidence→Mention→Entity→
   ReactionRecord→Reaction→Reaction↔RouteStep→Workflow→Session→Report。逐项回答
   §70：目标化合物、文献、实验文本、entities、reactions、条件（80 C/82%）、
   Evidence、对应 RouteStep、候选 step、真实实验支持（chain rung
   USER_DOCUMENT_EXPERIMENTAL_SUPPORT）、仍未知项。
2. **compound 论文局部性**（§14）：Paper A/B 的 "Compound 7" 在同一 registry 中
   解析为两个不同 entity（括号上下文：名称 vs InChI），互不污染，"compound 7"
   永不成为任何 entity 的 name。
3. **确定性**（§64/§65）：同输入两跑 → 相同 CHEM/RXN ids 与顺序。
4. **Agent 工具**（§17）：extract/get/search/validate 全走 ToolRegistry.execute；
   INVALID_QUERY；validate 的 link/step 状态由既有服务裁决。
5. **无提取模型**（§9/§19）：`discovery_only` + 明示句子，不建假 Reaction。
6. **§20 静态**：research_workflow 无 LLM/provider 实现 import。

回归：既有 3.9/3.8/3.7 测试全绿（一处 3.9 断言按既有 validator 语义校正——见 §10）。

```
pytest -q: 1317 passed, 50 skipped, 0 failed
```

## 9. 是否存在 blocking issue

**无。** 过程中三处最小修复（均属"新功能必需的接口缺陷"，§67）：

1. **extraction 丢弃 PRODUCT 组分**：`ReactionConditions` 无 products 桶，
   `buckets.setdefault(PRODUCT, [])` 把产品丢进游离列表。additive 增加
   `ReactionConditions.products` 并路由——否则"compound 8"产品根本进不了 Reaction。
2. **同名组分重复计入**：record 的 product 名与文档同名 mention 双记（前者无上下文
   → UNRESOLVED → 假 PARTIAL）。同名文档 mention **替换** record 组分（带上下文的
   那次才是同一组分）。
3. **name 双写 → key 污染**：3.8.2 registry 命中路径未写 identity_key（已在 3.8.2
   服务补齐）。

另有一处 3.9 测试期望按既有 validator 语义校正：document 级结构精确匹配本身即
USER_DOCUMENT_SUPPORT→EXPERIMENTALLY_SUPPORTED（step 级匹配，非 record 池覆盖）；
`stage()` 已停止把 record 灌入任务级 record 池（那会覆盖整条 route，违反 §37），
step 级实验 rung 由 `build_record_link`+`RouteEvidenceService` 承担。

## 10. 当前完整 CREA 架构图

```text
                ┌─────────────────────────────────────┐
                │      CLI / Agent / ResearchWorkflow   │
                └───────────────┬─────────────────────┘
                                │ tools · session · report
     ┌──────────────────────────┼──────────────────────────────┐
     │                          ▼                              │
┌────┴─────┐   ┌────────────────────────────────┐   ┌─────────┴──────────┐
│  Report  │◄──│  Services                        │──►│ Providers/Sources   │
│ v1 + v2  │   │  identity · entity_registry      │   │ OpenAlex/Crossref/  │
│ [§71标签] │   │  mention_resolution              │   │ PubChem/…（仅接口） │
└──────────┘   │  reaction_builder                │   └────────────────────┘
               │  reaction_extraction (LLM提议)    │
               │  route_reaction_linking           │
               │  route_validation · route_evidence │
               │  reaction_matching · literature   │
               │  document_evidence · research_    │
               │  workflow                         │
               └───────────────┬──────────────────┘
                               ▼
               ┌────────────────────────────────┐
               │ Core models（纯领域）            │
               │ reaction_knowledge · entity     │
               │ mention_resolution · identity   │
               │ evidence · reaction · route     │
               │ research · session              │
               └───────────────┬────────────────┘
                               ▼
               ┌────────────────────────────────┐
               │ chem（RDKit 唯一所在地，含新      │
               │ substructure_contains）          │
               └────────────────────────────────┘

知识闭环：Question → Identity → Literature → Mention → Entity → Reaction
        → Evidence → RouteStep ↔ Reaction → RouteEvidence → ResearchReport
```

## 11. 本阶段真正新增能力

- ResearchWorkflow 内建 knowledge 层（mentions/entities/reactions/linking）+ 内部 phase 日志 + session 产物
- 四个 Agent 反应工具（extract/get/search/validate）
- Report v2 的 Reactions 节与 §71 标签体系、RouteStep↔Reaction 可视化
- `search_reactions` 确定性过滤查询（含 exact/substructure）
- compound 论文局部性与确定性 id/顺序的 E2E 证明

## 12. Remaining limitations

- **substructure 检索**仅经 `substructure_contains` 精确谓词；无相似度/排序（设计如此）
- **无持久化**（内存 registry；SQLite 迁移点=Protocol）、**无图数据库/embedding**（§66/§67 明令）
- **product 识别**依赖 record 的 PRODUCT 组分或文档上下文定义（`Compound 8 (InChI=…)`）；自由文本中的产品推断仍属未做（precision 优先）
- **共享中间体**会被 validator 的结构匹配提升 support（既有 step 级语义）；"仅候选"的判据在证据链 rung
- 反应层尚未接进 v1 中文报告（v1 保持封板兼容）

## 13. 下一大阶段建议（未执行）

- **3.11 / 4.x**：Paper 级知识导出与查询（Paper→Mention→Entity→Reaction→Route 的确定性查询接口/CLI 输出）；SQLite 持久化（实现 ReactionRepository）；DECIMER 图片结构识别接入 mention 层
- 长期：知识图谱可视化/检索（仍建议不引入图数据库/embedding，除非届时有明确需求）

## 14. 最终结论

**Phase 3.10: PASS**

```text
pytest -q: 1317 passed, 50 skipped, 0 failed
```

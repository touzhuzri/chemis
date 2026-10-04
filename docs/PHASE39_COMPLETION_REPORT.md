# Phase 3.9 Completion Report — Reaction Intelligence

> 最终结论：**Phase 3.9: PASS**

## 1. 文件变更

| 文件 | 变更 |
|---|---|
| `src/crea/core/models/reaction_knowledge.py` | **新增**：`Reaction`、`ReactionComponent`、`ReactionStatus`、`RouteStepReactionLink`、`REACTION_KEY_VERSION`/`reaction_key`/`key_is_identity_based` |
| `src/crea/services/reaction_builder.py` | **新增**：`ReactionBuilder`、`ReactionRegistry`、`ReactionRepository`（Protocol）+ `InMemoryReactionRepository`、`evidence_level_for`、`evaluate_status` |
| `src/crea/services/route_reaction_linking.py` | **新增**：`RouteReactionLinker`（link / stage / literature_candidate） |
| `tests/architecture/test_layering.py` | 边界测试扩至 reaction 层；新增"反应服务必须走接口"测试（§57） |
| `tests/unit/test_reaction_knowledge.py` | 新增 14 |
| `tests/unit/test_reaction_builder.py` | 新增 11 |
| `tests/integration_offline/test_reaction_intelligence.py` | 新增 6（含完整 E2E） |
| `docs/PHASE39_COMPLETION_REPORT.md` | 本文件 |

**未动**：RDKit（2026.3.6）、AiZynthFinder 隔离（worker.py 原样、未下载模型/stock）、Evidence/RouteEvidence/RouteValidation 语义、Identity 语义、OpenAlex、pyproject、.env、ResearchWorkflow、ResearchReport、CLI、Agent loop。

## 2. 当前 CREA 架构图

```
                    ┌──────────────────────────┐
                    │   Agent / CLI / Workflow │
                    └────────────┬─────────────┘
                                 │ tools + report
      ┌──────────────────────────┼───────────────────────────┐
      │                          ▼                           │
┌─────┴──────┐   ┌───────────────────────────┐   ┌──────────┴─────────┐
│  Report    │   │   Services（编排/判定）      │   │   Providers/       │
│  v1 + v2   │◄──│  identity · evidence ·      │──►│   Sources          │
└────────────┘   │  mention_resolution ·       │   │  (OpenAlex/Crossref/ │
                 │  entity_registry ·          │   │   PubChem/…, 仅接口) │
                 │  reaction_builder ·         │   └────────────────────┘
                 │  route_reaction_linking ·   │
                 │  route_validation ·         │
                 │  route_evidence ·           │
                 │  reaction_matching ·        │
                 │  research_workflow          │
                 └────────────┬────────────────┘
                              ▼
                 ┌───────────────────────────┐
                 │ Core models（纯领域，无 IO） │
                 │ reaction · reaction_linking│
                 │ reaction_knowledge · entity│
                 │ mention_resolution ·       │
                 │ identity · evidence ·      │
                 │ route · research · session │
                 └────────────┬───────────────┘
                              ▼
                 ┌───────────────────────────┐
                 │ chem（RDKit 唯一所在地）      │
                 └───────────────────────────┘

知识链（本阶段打通的纵向连接）：
Paper → ChemicalMention → ChemicalEntity (CHEM-xxxx) → Reaction (RXN-xxxx)
      → Evidence / Locator → RouteStep link → RouteEvidence → ResearchReport
```

## 3. 已完成能力（全阶段累计）

- **3.1–3.4**：化学核心、文献/证据体系、Agent 工具环、文档证据
- **3.5**：Route / RouteValidation / RouteEvidence 阶梯（candidate 不等于 evidence）
- **3.6**：Identity 体系（name 永不猜结构；AMBIGUOUS 不选第一）
- **3.7**：ResearchWorkflow / ResearchSession / workflow 模式 / Report v2 / demo CLI / 场景验收 A–E
- **3.8.1**：ChemicalEntity + ChemicalEntityRegistry + CHEM-id + identity key + PubChem external id
- **3.8.2**：ChemicalMention → ChemicalEntity 解析（precision 优先；compound 7 论文局部）
- **3.9（本阶段）**：见下

## 4. 本阶段新增能力

1. **Reaction 模型**（`Reaction`）：组件→mention→entity 全链可追溯；`ReactionStatus` 五态（CANDIDATE/STRUCTURED/EVIDENCE_BACKED/EXPERIMENTALLY_SUPPORTED/PARTIAL），无 confidence。
2. **ReactionBuilder**：`ReactionRecord + ChemicalMentionResolution + Evidence + Paper → Reaction`。保留 record 原始信息，绝不二次 LLM 抽取覆盖（§12）；角色以 record 的角色列表为准，UNKNOWN 保持 UNKNOWN（§7）；product 未解析时反应保留（§9）。
3. **状态推导**（`evaluate_status`）：gaps 优先 → PARTIAL；实验 rung（含 §11 允许的 primary evidence）→ EXPERIMENTALLY_SUPPORTED；非 primary 引用级证据 → EVIDENCE_BACKED；仅 metadata/abstract **绝不** EVIDENCE_BACKED（§11）；validated record → STRUCTURED；裸提案 → CANDIDATE。不自动升级。
4. **证据级**（`evidence_level_for`）：复用既有 `ReactionEvidenceLevel` 五级（§17），不建第二套强度体系。
5. **Reaction key**（`crea-reaction-key-v1`）：identity-key 材料、各角色组排序（reagent/solvent 顺序差异不 fork 反应）、名称材料带 `name:` 标记且**永不用于合并**；不是 `hash(raw_text)`（§22）。
6. **ReactionRegistry / Repository**：同 key 同 paper scope 同 report 合并；不同论文同化学 → 保留两个；name 污染 key → 保留两个（§21）。Repository Protocol 为未来 SQLite→PostgreSQL 迁移点（§58），域模型不依赖任何 SQLite API。
7. **Reaction ↔ Route**（`RouteReactionLinker`）：`compare_reactions` 确定性比对 → `RouteStepReactionLink`（EXACT 才 `screening_only=False`）；`stage()` 只向 `RouteValidationService` 喂 pools，支持级别由它裁决（§57 不绕过）；`literature_candidate()` 供既有 `RouteEvidenceService` 使用。similar ≠ exact（§35/§37）。
8. **检索**（§60）：确定性精确检索 reaction_id / paper_id / doi / component entity id / reaction key；无 embedding。
9. **Provenance**（§62）：每条 Reaction 可回答来自哪篇 Paper、哪个 Document、哪个 Evidence、哪个 Mention 产生该 Component、采用哪个 Entity；任一层缺失 → PARTIAL + notes。

## 5. Reaction → Evidence / Paper / Route 的表现

- **Evidence**：`evidence_refs` 只保留真实持有的；未解析引用进 `unresolved_evidence_refs` 并使反应 PARTIAL（列出而非丢弃）；locator 来自既有 `Locator`（§20）。
- **Paper**：`paper_id`/`doi`/`source_id`/`document_id`；一对多（一个 paper/document 三个不同反应 → 3 条记录，§18/§19）。
- **Route**：exact 匹配 + record + held evidence → 经 `RouteValidationService` 升 EXPERIMENTALLY_SUPPORTED、经 `RouteEvidenceService` 达 `USER_DOCUMENT_EXPERIMENTAL_SUPPORT` rung；非 exact → `screening_only=True`、basis 非 EXACT、step 保持 CANDIDATE。

## 6. 测试

```
tests/unit/test_reaction_knowledge.py            14
tests/unit/test_reaction_builder.py              11
tests/integration_offline/test_reaction_intelligence.py  6（含完整离线 E2E）
tests/architecture（reaction 边界 + 接口约束）        +2
pytest -q: 1311 passed, 50 skipped, 0 failed
```
覆盖 §65 清单：Unit / Integration / Architecture / Offline E2E / Live opt-in（既有 tests/live，含 3.8.2 的 PubChem live）/ CLI（既有 smoke）/ Agent（既有）/ Workflow（既有）/ Report（E2E 内渲染）；必列项：Reaction model ✓、Reaction extraction ✓、Mention→Entity ✓、Reaction→Evidence ✓、Reaction→Paper ✓、Reaction→Route ✓、ResearchWorkflow ✓（既有回归）、ResearchReport ✓。

## 7. 架构测试（§57）

- Core（`reaction_knowledge.py` 等）：禁 agent/llm/sources/providers/rdkit ✓
- Reaction services：禁 OpenAlex/Crossref/PubChem 实现/AiZynthFinder/agent/llm/rdkit——必须走 provider/service 接口 ✓（`test_the_reaction_services_go_through_interfaces_not_implementations`）
- Route：不绕过 `RouteValidationService` ✓（linker 只喂 pools，行为测试断言升级由 validator 裁决）

## 8. 安全 / 许可（§63/§64）

全量测试含既有安全套件（.env 忽略、无密钥入库、日志脱敏）全部通过；未下载任何论文全集 / 模型 / stock；未改动 LICENSES.md 覆盖范围；未新增第三方依赖。

## 9. 已知 limitations

- **无持久化**：ReactionRegistry 内存实现（§58 允许）；迁移点是 `ReactionRepository` Protocol。
- **无图数据库**（§59）：关系/领域模型足够；知识图谱从现有关系构建。
- **反应检索无 substructure**：精确检索（id/doi/paper/entity/key）已具备；子结构检索可经既有 `crea.services.matching`，留待后续。
- **Reaction 未接进 ResearchWorkflow/CLI**：§67 禁止顺手重构 3.7 封板的 workflow；E2E 在集成层组合完整链并渲染 ResearchReport。产品级接入是下一阶段的自然工作。
- **名称识别召回率低**（3.8.2 既有原则）：precision/provenance 优先于 recall。
- **LIGAND/BASE/ADDITIVE** 组分落在 `unassigned` 桶但保留原 role 字段（Reaction 模型无独立桶位）。

## 10. 是否存在 blocking issue

**无。** 过程中的测试期修正均为测试侧问题或新代码内的小修：组件 role 映射误用 `item.role or role`（`Component.role` 默认 UNKNOWN 恒真，改为以 record 角色列表为准）、registry 同 id 覆盖（同 report 同 id 改为合并）、identity key 在 registry 命中路径缺失（3.8.2 服务补写——真实缺口，已在新代码修复并由测试钉住）、若干测试期望按真实语义校正（canonical 形式、provenance.source、PARTIAL 优先、distinct 反应才是一对多）。既有 3.8.x / 3.7 语义零改动。

## 11. 下一大阶段建议

- **3.9.x / 3.10：Reaction 知识接入产品入口** —— 将 ReactionBuilder/Registry 接进 ResearchWorkflow 与 CLI（增量），使 `crea research` 直接产出 Reaction 层结论；补 substructure 检索。
- **4.x：知识图谱查询层** —— Paper→Mention→Entity→Reaction→Route 的确定性查询/导出（仍不引入图数据库/embedding，除非届时有明确需要）。
- 长期：SQLite 持久化（实现 `ReactionRepository`）、DECIMER 图片结构识别接入 mention 层（3.6 已留接口）。

## 12. 验收对照（§73）

Reaction model ✓｜Reaction extraction ✓｜Mention→Entity ✓｜Reaction→Evidence ✓｜Reaction→Paper ✓｜Reaction→Route ✓｜ResearchWorkflow ✓｜ResearchReport ✓｜offline E2E ✓｜architecture ✓｜security ✓｜existing tests ✓

## 13. pytest -q 结果

```
1311 passed, 50 skipped, 0 failed in 19.37s
```

## 14. 最终状态

**Phase 3.9: PASS**

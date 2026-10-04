# Stage H Report — Evidence-backed Transformation MVP

> **Stage H: COMPLETE**
> 基线：1561 passed, 50 skipped, 0 failed（Stage G 末）
> 最终：**1577 passed, 50 skipped, 0 failed**（零回归）
> 前端：`npm run build`（tsc + vite）通过

一句话：CREA 第一次把「候选结构」和「已记录化学转化证据」连接起来——给定
一个 target 和一个 candidate，判断两者之间是否存在**已有、可追溯的化学转化
证据**，并严格区分「文献/反应事实」与「CREA 推断」。到线即停——**没有**
递归逆合成、路线搜索、Knowledge Graph、第二个 Agent、新数据库。

```text
Target ChemicalEntity + Candidate ChemicalEntity
        ↓
Transformation Evidence Search（ReactionRegistry）
        ↓
已有 Reaction（复用 Stage D 唯一模型）
        ↓
Transformation（引用 Reaction，不替代）
        ↓
证据 + provenance + diagnostics
        ↓
ChemistryAgent → Workspace
```

## 1. Stage H 目标（§一）

不是「找到一条路线」，而是把「两个结构之间可能存在转化」变成一个有证据
等级、有 provenance、可解释的对象。

## 2. 开始前架构审计

确认复用：Stage D 的 `Reaction` 唯一模型（`core/models/reaction_knowledge.py`）、
`ReactionRegistry.find_between`（子集语义，n:m 继承）/ `find_by_product` /
`find_by_reactant` / `find_by_component_entity`、`Evidence`/`Provenance`
唯一模型、Stage G 的 reaction evidence 调用方式、`ChemistryToolRegistry`
注册方式、ChatPage 的 ToolResult 展示。

**未新建平行模型**：`Transformation` 是能力层对象，**引用** `Reaction`，
不替代、不复制。

## 3. 复用了哪些现有模型

- `Reaction`（Stage D，唯一）：`Transformation.reaction` 直接持有它。
- `ReactionRegistry.find_between` / `find_by_component_entity`（Stage D）。
- `Evidence` / `Provenance`（唯一）：transformation 的 evidence 是反应自身
  citations，provenance 是反应自身的 provenance——零新建。
- `ChemicalEntity`：输入全走 entity_id（§五，identity 在 resolver 层统一）。

## 4. Transformation 的最终设计（§四/§九）

`core/models/transformation.py`：

```text
Transformation              ← 能力层对象（引用 Reaction，不替代）
├── transformation_id       TRANS-<hash(reaction_id|direction)>
├── reaction                Reaction（Stage D 唯一模型）
├── direction               forward | reverse（查询方向 vs 记录方向）
├── evidence_level          documented | derived | inferred
├── evidence                reaction.evidence_refs → [{"evidence_id":...}]
├── provenance              reaction.provenance.model_dump()
└── diagnostics             方向/事实说明
```

`TransformationSearchResult`：status + transformations + matched_reactions +
evidence + provenance + diagnostics。

## 5. Evidence 设计（§六/§十一）

`TransformationStatus`（§六/§八合一，5 个有意义的取值）：

```text
direct_transformation              已有 Reaction 直接连接 candidate ↔ target
related_transformation             有反应历史但无直接连接（不自动构造路线）
no_transformation_evidence         搜索完成，没有（≠「该转化不存在」）
transformation_search_unavailable  搜索能力不可用（≠ no_evidence）
invalid                            输入非法（空/重叠）
```

`TransformationEvidenceLevel`（§十一）：`DOCUMENTED`（stored Reaction）/
`DERIVED`/`INFERRED`。Stage H 只产出 `DOCUMENTED`——**INFERRED 绝不伪装成
DOCUMENTED**（§十）。

## 6. Provenance 设计（§二十一）

复用 `Provenance`，零新建 `TransformationProvenance`。transformation 的
provenance = reaction 自身 provenance（tool/source/source_id 可追溯）。

## 7. ReactionRepository 集成（§十三）

`TransformationEvidenceService`（`services/transformation.py`）只走注入的
`ReactionRegistry`：
- `find_between(reactants, products)` → FORWARD；
- `find_between(products, reactants)` → REVERSE；
- 无直接 → `find_by_component_entity` 判定 related / no-evidence。

n:m 子集语义继承 Stage D：`A+B+C→T` 可被 `[A,B,C]` 或 `[A,B]` 命中同一个
Reaction。

## 8. ToolRegistry 集成（§十六）

`tools/chemistry.py::FindChemicalTransformationsTool`（`find_chemical_
transformations`），`default_tool_registry` 自动装配（`transformation_service`
缺省则 `TransformationEvidenceService(reactions=...)`）。schema：

```json
{"candidate": "entity_id", "target": "entity_id"}          // 便捷
{"reactant_entity_ids": [...], "product_entity_ids": [...]} // n:m
```

## 9. Agent 集成（§十七/§十八）

Agent 只经 ToolRegistry 调用、解释结果，不自己查 ReactionRepository/解析
RDKit/判断证据/构造 provenance。prompt 增 3 条规则（14-16）：direct 是记录
事实非推断、四种结果不混、reverse 明示反向、stored reaction ≠ 可行性判断。

## 10. Workspace 集成（§十九）

ChatPage 的 `find_chemical_transformations` 结果渲染为卡片（无独立
Dashboard、无绿/红「可行/不可行」着色）：

```text
Direct transformation  R-001 · forward · documented
候选 ──R-001──→ 目标（已记录反应证据，非可行性判断）
```

no_evidence / related / unavailable 各有明确文案。

## 11. 测试结果（§十四/§二十四/§二十八）

`tests/transformation/test_transformation.py`（10）：direct（含 reaction/
reactants/products/evidence/provenance）、reverse 标记反向、no-evidence、
unavailable≠no-evidence、相似≠转化（两无反应实体永不产生 transformation）、
n:m 命中同一 Reaction、related≠direct≠none、invalid、tool 经 ToolRegistry、
Agent 自然语言 E2E（resolve×2 → find_chemical_transformations → 解释）。

架构测试 +2：transformation service 不 import agent/app/api/web/tools/rdkit；
`Transformation` 引用唯一 `Reaction`，无 `TransformationReaction/Reaction2`。

## 12. 架构测试（§二十二）

`test_the_transformation_service_is_a_capability_not_an_agent_or_web`、
`test_transformation_references_the_existing_reaction_and_never_replaces_it`。

## 13. 已知限制

- 仅一跳（candidate ↔ target），无递归（§二十七）。
- 只有 `DOCUMENTED` 证据等级实际产出；DERIVED/INFERRED 已声明未启用（§十一）。
- literature provider 未接 reaction evidence（遵守 fail-closed access
  policy，不 fake citation，§二十）。
- 输入是 entity_id（identity 在 resolver 层统一），transformation service
  不做结构解析。

## 14. 没有实现什么（§二/§三十二）

无递归逆合成、无 route search/optimization、无多步规划、无「最佳前体」、
无 Knowledge Graph、无新数据库/新表、无新 ChemicalEntity/Reaction/Evidence/
Provenance、无大型路线 UI、无成功率预测、无把 similarity 当 reaction。

## 15. Definition of Done（§三十）

ChemicalExplorer 之外新增：Transformation 领域对象（引用唯一 Reaction）✓ ·
TransformationEvidenceService ✓ · find_between / find_transformations ✓ ·
evidence 分级（documented）✓ · direct/reverse 区分 ✓ · no-evidence ≠
unavailable ✓ · ToolRegistry 暴露 ✓ · Agent 可调用 ✓ · Workspace 可显示 ✓ ·
deterministic tests ✓ · E2E ✓ · 零回归（1577/50/0）✓ · 报告 + 架构同步 ✓。

## 16. Victory Line（§三十一）

用户问「这个候选分子能不能通过已有反应转化成目标分子？」→ Workspace →
ChemistryAgent → ToolRegistry → find_chemical_transformations → candidate +
target → ReactionRegistry → Transformation Evidence（direct · documented ·
R-001）→ Agent 解释 → Workspace 显示 Evidence + Provenance。

无证据 → 「当前可查询数据源中没有找到直接转化证据。这不等于该转化不存在。」
数据源不可用 → 「当前反应检索能力不可用，因此无法判断。」

**Stage H 到此停止，不自行进入递归逆合成、路线优化、Knowledge Graph。**

胜利线已达成：CREA 能把「候选结构」和「已记录化学转化证据」连接起来，且
不把推断、相似性、无证据和数据源不可用混为一谈。

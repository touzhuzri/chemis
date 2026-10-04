# Stage D Report — Reaction Intelligence MVP

> **Stage D: COMPLETE**
> 基线：1499 passed, 50 skipped, 0 failed
> 最终：**1511 passed, 50 skipped, 0 failed**（+12，旧测试零回归）

一句话：CREA 第一次真正理解 **ChemicalEntity ↔ Reaction ↔ ChemicalEntity**，
并回答"这个分子参与过哪些已知反应 / 这个反应由哪些分子组成"。到线即停——
没有递归合成搜索、没有路线排序、没有 Reaction Graph UI。

## 1. pytest

```text
1511 passed, 50 skipped, 0 failed
```

## 2. Reaction domain model（§三——收口，非新建）

`core/models/reaction_knowledge.py::Reaction` 已是 n:m 一等对象（reactants/
products/reagents/catalysts/solvents 角色桶 + evidence_refs + provenance +
reaction_key）。Stage D **ADDITIVE** 扩展：
- `reaction_class: str = ""`（来源/抽取给的标签，不推断）
- `atom_mapping: dict | None = None`（§八：缺省正常；无 mapping 的反应是完整反应）

**未建平行模型**：审计确认既有 `Reaction`（知识对象）与 `ReactionRecord`
（抽取中间体）链已成立，全部复用（架构钉：`Reaction` 类唯一）。

## 3. n:m relation（§四）

`A + B → C`、`A + B → C + D`、`A + B + C → D` 全部可表达并测试。共同发生
关系不拆边：`A`/`B` 作为**同一反应**的 reactants 保存与查询（测试钉住）。

## 4. ChemicalEntity integration（§十四/§十五）

- ReactionComponent 只存 `entity_id`（+身份 key/规范 SMILES 的**引用**），
  反应自身不做结构计算、不自己解析名称——架构钉禁止 reaction 模块 import
  `crea.chem`/`crea.semantic`。
- 测试：`C#Cc1ccccc1` 经 SemanticResolver 成 entity，反应引用该 entity_id；
  reaction 无独立化学身份。

## 5. ReactionRepository（§十一）

`ReactionRepository` 协议 + InMemory + SQLiteStore 三处一致扩展：
`find_by_product(entity_id)`、`find_by_reactant(entity_id)`、
`find_between(reactant_ids, product_ids)`（子集语义：named reactants 全消耗、
named products 全生成、共同发生不拆边）。save/get 全往返（含新字段）。

## 6. search_reactions（§十二/§十三）

Stage C 的"诚实 TODO"升级为**真实 MVP Tool**：
`search_reactions(product_entity_id=…) / (reactant_entity_id=…) /
(entity_id/paper_id/structure)` → `ReactionSearchResult` 列表，每项含
`reaction` + `matched_entities` + `match_role`（product/reactant/component/
filter）+ `evidence`（引文，kb 接线时解析、否则保留 id）+ `provenance` +
`diagnostics`（"atom mapping not performed"、未解析组分）。Agent 无需再猜
结果为何返回。无过滤 → REJECTED；无知识源 → UNAVAILABLE；无匹配 → 空 +
"no known reaction matches"（绝不编造）。

## 7. evidence/provenance（§九）

沿用既有 Evidence/Provenance（零新建）。reaction_evidence 的 FK 语义被
测试证明：引用不存在的证据 → 写失败（不悬挂）。search result 的 evidence
随行；provenance 随反应本身。

## 8. 真实演示闭环（§十七）

```text
A ──┐
    ├── Reaction R1 ──→ Target      (find_between([A,B],[T]) = R1)
B ──┘
search_reactions(product=T) → R1（A+B → T，source/evidence 随行）
search_reactions(reactant=A) → R1
```

在 SQLite 上端到端成立，双向可查。

## 9. 新增测试

`tests/reaction/test_reaction_intelligence.py`（9）：n:m 三种配比、角色分离
（UNKNOWN 不猜）、顺序无关 key + 不同产物不合并、实体集成（反应引用 entity
不建身份）、InMemory+SQLite 双实现 repository 往返与三 finder、最小闭环
双向、无反应不编造、ReactionRecord↔Reaction 单模型链。`tests/tools/` 的
search_reactions 测试按新语义重写（§二十五：锁定旧"未完成"行为的测试改写）。

## 10. 架构测试（§二十二七钉）

reaction 模块禁 chem/semantic/rdkit（不自己算结构/解身份）· 唯一 `Reaction`
类 · 无 `NewReaction/ReactionEntity/…` 第二模型 · reaction 构造保
evidence/provenance · tools 禁 SQLite/sources.pubchem/worker 信封（Stage C
钉继续）· 无第二 identity 词表 · 无第三 worker 协议。

## 11. 文件变更

新增：`tests/reaction/test_reaction_intelligence.py`、`docs/STAGE_D_REPORT.md`。
修改：`core/models/reaction_knowledge.py`（+2 字段 +`ReactionSearchResult`）、
`services/reaction_builder.py`（协议+InMemory+Registry 三 finder +
`search_results`）、`repositories/sqlite.py`（三 finder + 新列 guarded
ALTER/CREATE + 往返）、`tools/chemistry.py`（search_reactions 真工具）、
`tools/registry.py`（工厂参数 `reactions=`）、`tests/tools/…`（重写旧断言）、
`tests/architecture/…`（+2 钉）。

## 12. Known limitations

- **无递归合成搜索**（§十八明令）：只到"Target 的已知前体"，不展开前体之
  前体；无路线排序/优化/planner/MCTS/beam search/可行性预测。
- **无 Reaction Graph UI**（§十九）：无 Cytoscape/React Flow/拖拽/动画；
  Python/API 返回 Entity-Reaction-Entity 即可。
- **atom mapping 无算法**（§八）：RxnMapper 权重条款未清 → `atom_mapping`
  为 None + diagnostics 明示，绝不伪造。
- **跨文献反应检索未做**：search_reactions 范围=已建立反应知识；公开
  反应库检索是未来工作。
- reaction_class 仅存不推断（来源/抽取给定才填）。
- 名称→反应的端到端（name input 直接喂反应构建）依赖 Stage B-1 的
  SemanticResolver 路径；本阶段反应测试用 entity 引用（§十允许的
  "manually constructed / existing Record → Reaction" 路线）。

## 13. Future work（不执行）

- Stage E：Workspace + Conversation（Agent 以 search_reactions 为能力）。
- 反应公开知识检索 + RxnMapper（ToS 清理后）atom mapping。
- 前体探索（§十八明确排除于本阶段；届时是 Agent 层的探索能力，不是
  本 MVP 的延伸）。

**Stage D 到此停止，等待下一步指令。不自行进入 Stage E。**

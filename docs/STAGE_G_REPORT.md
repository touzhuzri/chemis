# Stage G Report — Chemical Exploration MVP

> **Stage G: COMPLETE**
> 基线：1544 passed, 50 skipped, 0 failed（Stage F 末）
> 最终：**1561 passed, 50 skipped, 0 failed**（零回归）
> 前端：`npm run build`（tsc + vite）通过

一句话：CREA 第一次进入 **Chemical Exploration** —— 给定目标分子，从结构关系
出发找到合理候选结构，统一解析为 `ChemicalEntity`，检查这些候选是否存在于
反应知识中，并把 evidence / provenance 完整带回来。到线即停——**没有**递归
探索、逆合成规划、路线优化、Knowledge Graph、第二个 Agent。

```text
Target
  ↓  Structure Exploration（existing chemistry services）
  ↓  Candidate Structures（ChemicalSpace）
  ↓  Chemical Identity（IdentityResolutionService + ChemicalEntityRegistry）
  ↓  Known Chemical Space（local knowledge base）
  ↓  Reaction Evidence（ReactionRegistry）
  ↓  ExplorationResult
```

## 1. pytest

```text
1561 passed, 50 skipped, 0 failed
```

新增 `tests/exploration/test_exploration.py`（10）+ `tests/architecture/`
（+3）。新增源文件同时扩充参数化架构测试矩阵（全部通过）。

## 2. 本阶段不是 retrosynthesis（§1）

`ChemicalExplorer` 是**结构空间探索**，明确区分四个等式（§1）：

```text
结构关系 ≠ 合成可行性
结构候选 ≠ 已验证前体
相似分子 ≠ 反应关系
数据库存在 ≠ 文献证明可合成
```

无递归拆解、无反应预测、无路线搜索、无多级合成树、无最优路线（§31/§32）。

## 3. ChemicalExplorer（§4/§8/§29）

`services/exploration.py::ChemicalExplorer`——一个领域能力，不是第二个 Agent：

```python
result = explorer.explore(target_entity_id="CHEM-00000001")
```

- **组合现有 chemistry services，零重写**（§8）：`normalize`（RDKit 解析/规范）、
  `screen_similarity`（Morgan fingerprint Tanimoto）、`compare_scaffolds`（Murcko
  scaffold）、`decompose_molecule`（ring_linker fragment）。架构钉：
  `test_the_explorer_depends_on_chemistry_services_not_the_agent_or_web`。
- **不依赖 API/Web/Agent/Workspace**（§29）：只 import core/chem/services，
  不 import agent/app/api/tools/semantic/providers/sources/repositories。
- **单层**（§31）：`Target → Candidates`，从不 `Candidate → Candidate 的候选`。

## 4. Candidate 数据源（§9）

`ChemicalSpace` 协议（`candidate_smiles(target) -> list[str]`）。MVP 默认
`LocalEntitySpace` = 本地知识库中的其它实体，诚实标记 `source = "local"`；
测试用 `FixedChemicalSpace`。**不假装 PubChem 结构相似性搜索**——PubChem
provider 尚无真正的结构相似搜索，就绝不把本地数据包装成 PubChem 结果（§9/§28）。

## 5. Chemical Identity（§10/§24）

候选**绝不** `new ChemicalEntity(smiles)`。每条候选 SMILES 走：
`normalize → IdentityResolutionService.resolve(StructureInput) →
ChemicalEntityRegistry.register_resolution`——与现有 CREA 化学对象统一，去重
靠 `identity_key(InChI)`（§24），同一分子不同 SMILES 只出一个 Candidate。

## 6. Relationship（§11/§12）

三种关系 `RelationshipKind`：`SIMILAR` / `SAME_SCAFFOLD` / `FRAGMENT_RELATED`。
候选带：`similarity`（Tanimoto 数字，永不伪装成化学关系）、
`scaffold_relation`（same/related/different）、`fragment_related`（共享
ring_linker 片段）。相似度只是筛选信号（§12），「Tanimoto 0.84」只说「结构
相似度较高」，从不说「前体」或「可直接合成」。

## 7. Reaction Evidence（§13/§14）

四等 `ReactionEvidenceStatus`，**永不混为一谈**：

```text
DIRECT_REACTION_EVIDENCE      已有 Reaction 直接连接候选 → 目标
RELATED_REACTION_EVIDENCE     候选有反应历史，但无直接 候选 → 目标
NO_REACTION_EVIDENCE          查询成功，没有找到
REACTION_SEARCH_UNAVAILABLE   知识源不可用
```

查询走现有 `ReactionRegistry.find_between([candidate], [target])`（子集语义：
「候选 + 其它反应物 → 目标」）与 `find_by_component_entity`。

## 8. 排序 / 数量 / 去重（§22/§23/§24）

- 排序透明（非「化学价值评分」）：direct reaction evidence 优先 → similarity
  降序；依据写入 diagnostics，绝不输出 `Best precursor` / `Score 9.5/10`。
- 默认 `MAX_CANDIDATES = 10`，`returned_candidates` vs `total_candidates`
  明示「当前搜索范围内返回」而非「所有可能候选」。
- 无结构关系的分子（sim < 0.2 且不同骨架且无共享片段）被丢弃并计数报告。

## 9. Agent Tool（§16/§17/§18/§30）

`tools/chemistry.py::ExploreChemicalSpaceTool`（`explore_chemical_space`）：

```text
ChemistryAgent → ToolRegistry → explore_chemical_space → ChemicalExplorer
```

- 输入 `{entity_id}` / `{chemical: 名称}` / `{smiles}`；工具自己 resolve →
  entity → explorer（§17）。
- 输出完整 `ExplorationResult`（target/candidates/relationships/reaction
  evidence/evidence/provenance/diagnostics），绝不只是 SMILES 列表（§18）。
- Agent 仍只有一个 `ChemistryAgent`；无 `ExplorationAgent`（§30，架构钉
  `test_no_second_agent_appears_for_exploration`）。
- system prompt 增 3 条探索规则（§20）：解释「为什么找到」、相似度只是筛选、
  绝不排序为「最佳前体」（§15）。

## 10. UI（§21/§36）

只在 Chat 内展示，无独立 `/explore` 页面、无 Knowledge Graph canvas/3D
viewer。`ChatPage` 的 `explore_chemical_space` 工具结果渲染为候选卡片：

```text
目标 C#Cc1ccccc1 · 来源 local
same_scaffold  Cc1ccccc1
  similarity 0.32 · scaffold same · reaction direct_reaction_evidence
```

复用 Stage F 的 `▶ 工具调用 / Evidence / Provenance` 可展开区。

## 11. 测试（§27，deterministic）

`tests/exploration/test_exploration.py`（10）：similarity 候选（苯 vs 噻吩）、
去重（同一分子两种 SMILES → 一个候选）、same scaffold（苯乙炔 vs 甲苯）、
fragment 关系（二苯乙炔 vs 苯乙炔，不同骨架共享片段）、direct reaction
evidence（甲苯→苯乙炔）、no reaction evidence（空搜索）、unavailable≠empty、
候选 evidence+provenance、tool 经 ToolRegistry、**Agent 自然语言 E2E**
（「帮我探索一下苯乙炔周围的化学结构」→ resolve → explore → 回答，ScriptedLLM +
真化学工具）。

## 12. 真实数据 Smoke Test（§28）

公共 provider 无真正的结构相似搜索，未伪造；MVP 用 `FixedChemicalSpace`
fixture + `LocalEntitySpace`，`source` 诚实标记。报告中如实记录：
**real provider unavailable**（未绕过）。

## 13. 已知限制

- 候选源仅本地知识库 / fixture（PubChem 结构相似搜索缺失，未假装支持）。
- 单层探索，无递归（§31）；无自动接 retrosynthesize（§32）。
- 相似度阈值 0.2 是「有结构关系」的地板，不是化学结论。
- 排序是展示顺序，不是化学价值评分。
- 无 Knowledge Graph / 新数据库（ExplorationResult 即时计算不持久化，§33/§34）。

## 14. 下一阶段边界（不执行，§38）

递归探索、逆合成规划、路线优化/排序、多步合成树、Knowledge Graph、3D viewer、
反应编辑器——全部留后。

**Stage G 到此停止，不自行进入递归探索、逆合成规划、路线优化或 Knowledge
Graph。**

胜利线（§38）：用户在 Workspace 输入「帮我探索一下苯乙炔周围的化学结构」，
CREA 返回候选并明确区分——哪些只是结构相似、哪些共享骨架、哪些有直接反应
证据、哪些只是搜索结果、哪些没有反应证据、哪些数据源不可用——已达成。

# Route Explorer（Stage 5.4）

## 目标

在 5.1 Transformation Search / 5.2 Comparison / 5.3 Graph 之上，实现一个
**有限、确定性、前向**的路线探索器：

```text
Start Molecule → Reaction → Intermediate → Reaction → Target Molecule
```

**Route Explorer ≠ AI Retrosynthesis**：只用已存在、明确、可验证的数据关系。

## 定位（§一）

确定性、可追溯、基于 Reaction Graph、有限深度、可解释。不是 AI 合成规划器、
逆合成系统、最佳路线推荐器、成功率预测器、实验设计器。

## 只做前向探索（§十）

本阶段**只**实现 forward exploration：`start → 已知反应 → 已知产物 → 下一反应`。
**不**实现逆合成（target → retro reaction → precursor）——留给未来。

## 路线语义与完整性（§二/§七/§八）

`RouteStatus`：

- **COMPLETE**：每一步 input/output 结构都明确，且到达 target。
- **PARTIAL**：某步 reaction 匹配，但产物结构无法确定（或达到 max_steps）。
- **BLOCKED**：当前没有可继续的合法 transition（`NO_NEXT_REACTION`）。

`blocked_reason` ∈ {`NO_NEXT_REACTION`, `PRODUCT_STRUCTURE_UNAVAILABLE`,
`MAX_STEPS_REACHED`}。任何一步 output 未知 → **不**伪装成完整路线。

## Product Resolution（§十三/§十四/§十五）

产物**只能**来自 `ReactionTemplate.reaction_smarts`（一个确定性的
`reactant>>product` transform）：`chem/reaction_transform.py::apply_reaction_smarts`
用 RDKit 应用 transform，产出可验证、可复现的 SMILES。**空 transform → 无产物。**

**不猜产物**：无 heuristic / AI / name-based 生成。当前 46 条 seed 的
`reaction_smarts` 全为空，因此**当前真实库不会产生完整路线**——这是正常且诚实的，
由 diagnostic 说明；**不**为让 Explorer「看起来能工作」而补 product SMARTS。

> 注：`product_patterns`（用于*匹配*候选产物）不等于 `reaction_smarts`（用于*生成*
> 产物）。Route Explorer 需要后者；种子库两者都没有，故停在 PARTIAL/BLOCKED。

## Target Matching（§十六）

复用项目标准身份：`normalize` → Standard InChI → `identity_key`。`CCO`/`OCC` 是同一
分子，不因 SMILES 拼写不同而误判。

## Cycle Avoidance / Dedup / Ranking（§十八/§十九/§二十）

- 环避免：按**分子 identity key** 记录 visited（非数据库 entity id）。
- 去重：deterministic `signature` = `reaction_template_ids + molecule_identity_keys`。
- 排序：`COMPLETE` > `PARTIAL` > `BLOCKED` → 步数升 → signature 字典序。
- **禁止**：best route / recommended route / highest success / highest yield。

## 深度（§十一/§十二）

`max_steps`：默认 3，**只允许 1–3**；超出 → 结构化错误 `INVALID_ROUTE_DEPTH`
（不 clamp）。`max_steps`（化学步）与 5.3 的 graph `depth`（图遍历）是两个概念，
不混用 API 语义。

## Evidence / Confidence / Yield（§二十一/§二十二/§二十三）

每步显示 `evidence_count` / `evidence_summary`（curated/literature），可点进 Reaction
Detail / Literature Detail。**无** `route_confidence`/`success_probability`。
**Yield: Not available**——不估计单步或总收率。

## 分层与持久化（§三十七/§三十八）

```text
ReactionGraphService（knowledge representation）
        ↓
RouteExplorerService（bounded path search）
```

两个 service 分离，不合并成巨型类。路由结果为 **transient DTO**，**不**新增
`routes`/`route_steps` 表。

## 数据/网络/AI 边界（§三十二/§三十三）

完全基于本地数据；**不**调 Crossref/OpenAlex/Unpaywall/PubChem，**不**联网；
**不**用 LLM/Agent/AI route reasoning。架构测试钉住
`services/route_explorer.py` 不 import providers/sources/agent/llm/httpx/aizynthfinder。

## API（§二十四/§二十五/§二十六）

```text
POST /api/routes/search
     {"start_molecule": "CCO", "target_molecule": "CC=O", "max_steps": 3}
→ {"routes": [...], "partial_routes": [...], "blocked_paths": [...],
   "count": n, "diagnostics": [...]}
```

- 空结果 → `200`（`routes: []`），不是 500。
- product 不可用 → 不报错，返回 `status=partially` + `blocked_reason=PRODUCT_STRUCTURE_UNAVAILABLE`。
- 非法 SMILES → 400 `INVALID_MOLECULE`；`max_steps` 越界 → 400 `INVALID_ROUTE_DEPTH`。

## UI（§二十七–§三十）

Molecule Explorer 内「Route Explorer」区：start=当前分子 + target + Explore → 路线卡片
（逐步 input → reaction → output，标注 evidence count），完整/partial/blocked 明确区分。
Reaction 点击进 Reaction Detail。无大型图布局、无绿色「route found」误导。

## 测试

`tests/reaction/test_route_explorer.py`（13）：一步 COMPLETE、两步 COMPLETE、
无路线、product 不可用 → PARTIAL（`PRODUCT_STRUCTURE_UNAVAILABLE`，无伪造 step）、
生产库无完整路线、target 身份归一化（OCC/CCO）、max_steps 越界结构化错误、非法
SMILES、环避免、确定性、COMPLETE 优先排序、无 probability/yield/recommendation
字段、API（空 routes + partial、400×3）。

**测试用带明确 `reaction_smarts` 的 test ReactionTemplate 验证完整路线算法——未修改
生产 Reaction Library。**

架构测试 +1。

## 未做（§三十一/§三十四/§三十九）

不自动从 Literature 恢复产物、不联网、不用 AI、不修改 Reaction Library / 46 模板、
不补 product SMARTS / reagent / conditions、不做逆合成、不做路线推荐、不预测 yield。

**Stage 5.4 completed.** 到此停止——不进入 Stage 6（AI Context / Modification
Analysis / Route Reasoning / Evidence-aware Agent）。

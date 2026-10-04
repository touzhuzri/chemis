# Reaction Comparison（Stage 5.2）

## 目标

把两个或更多 `ReactionTemplate` 并排比较它们**已知的事实**：

```text
Reaction A  vs  Reaction B  vs  Reaction C
```

比较的是 **Known Reaction Facts**（结构匹配 / reaction class / transformation /
reagents / conditions / mechanism / selectivity / limitations / evidence /
literature），**不是** Experimental Success Prediction。不做 steric/electronic/
solvent score、不做 yield model、不做 ML success prediction。

## 瞬态 DTO（不持久化）

`ReactionComparisonResult` 是 transient response DTO；不重复持久化
`ReactionTemplate`，不复制 provenance（§二十六）。

## 比较维度（§五–§十四）

| 维度 | 来源 |
|---|---|
| Transformation / class | `ReactionTemplate.transformation` / `class_family` |
| Substrate compatibility | 复用 **Transformation Search (5.1)** 的 `match_level` / `matched_substrate_atoms` |
| Reagents | `ReactionTemplate.reagents`（缺失 = Not specified） |
| Conditions | `ReactionTemplate.conditions`（缺失 = Not specified） |
| Mechanism | `mechanism_class` |
| Selectivity | `SelectivitySpec`（regio/stereo/chemo） |
| Limitations | `Limitation` |
| Evidence | `ReactionEvidenceService`（count + summary） |
| Literature | literature-linked evidence → `Paper`（year/journal/DOI/title） |
| Yield | **Not available**（`yield_available=False`；无 yield 模型，不推断） |

- **不创建主观「兼容性分数」**：substrate compatibility 直接用 5.1 的 match level，
  不产生 `compatibility_score = 0.87`。
- **不重新实现 SMARTS**：唯一结构匹配入口是 `TransformationSearchService` /
  `matches_smarts`（§十八）。
- **不复制 Literature Record**：只显示 count + 少量元数据；点击进 Literature Detail。
- **语义**：`Literature evidence` / `Linked literature`，**不写** `Successful examples`；
  Evidence count ≠ 该反应验证过该底物（§十二/§十三）。

## 禁止项（§二十）

结果模型**不含**：`best_reaction` / `recommended_reaction` / `winner` / `score` /
`confidence` / `success_probability` / `predicted_yield`（测试钉住）。

## 确定性（§十九）

结果按**输入顺序**（显式，非 dict/database/hash 顺序）排列；相同输入结果逐字节一致。

## API（§十五）

```text
POST /api/reactions/compare
     {"reaction_template_ids": [...], "substrate": "...", "target": "..."}
```

- 空列表 → `INVALID_INPUT`(400)
- 未知模板 → `TEMPLATE_NOT_FOUND`(404)
- 结构化错误，无 traceback。

## UI（§二十一/§二十三/§二十四）

在 **Molecule Explorer** 的「Transformations — Candidate Reactions」表中：勾选 ≥2 个
候选 →「Compare selected」→ 下方 **Reaction Comparison** 面板（维度 × 反应的矩阵）。
每列标题链接进 **Reaction Detail**；Linked literature 链接进 **Literature Detail**。
不复制新的 Reaction Detail 页面。

## Evidence 与 Fulltext Permission 独立（§二十五）

比较显示 `Literature linked`；Fulltext permission 仍是它自己的判断（可能 UNKNOWN）。
不因 UNKNOWN 删除 Literature，也不因 MAY_RETRIEVE 提高 Evidence 权重。

## 测试

`tests/reaction/test_reaction_comparison.py`（13）：比较两个、比较三个、match level
复用、未给 substrate 则不评估、reagents/conditions/selectivity 展示、缺失不编造、
evidence + literature 集成、yield 不可用、无推荐字段、确定性 + 输入顺序、空输入
`INVALID_INPUT`、未知模板 `TEMPLATE_NOT_FOUND`、API。

架构测试 +1：`services/reaction_comparison.py` 不 import providers/sources/agent/
llm/httpx/sklearn/numpy。

## 未做（§二十九–§三十二）

不增加 Reaction Library 内容、不补 product SMARTS、不实现 Reaction Graph（5.3）/
Route Explorer（5.4）/ AI Assistant（6.x）。

**Stage 5.2 完成，到此停止。**

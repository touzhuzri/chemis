# Transformation Search（Stage 5.1）

## 目标

把「substrate（+ 可选 target）」映射到已有的 `ReactionTemplate`，产生一组
**候选反应模板**：

```text
结构变化 → Transformation Search → Candidate ReactionTemplate → Reaction Evidence
```

不是路线规划，不是 AI 推理。**Chemistry Tool First, AI Second**：无 LLM、无
embedding、无向量库、无 AI similarity、无自然语言猜反应。

## 复用（§二）

复用 `ReactionTemplate` / `Transformation(input_features, output_features,
note)` / `substrate_patterns` / `product_patterns` / `matches_smarts`（与
`ReactionMatcher` 同一 SMARTS 原语）/ `ReactionEvidenceService`。**未**创建
`TransformationTemplate` / `TransformationModel` / `ReactionCandidateModel`。

## Transformation 的三种情况（§三）

- **A 有 substrate + product pattern** → 最可靠：两者都匹配 = `TRANSFORMATION_MATCH`。
- **B 只有 substrate pattern** → 只能 `SUBSTRATE_MATCH`；明确标注「无法确认 target product」。
- **C 只有 Transformation 自由文本** → 仅作辅助；**不**据自由文本自动构造结构匹配。

## 两种搜索模式（§四/§五）

- **Substrate-only**：`normalize → SMARTS → candidates`。
- **Substrate + target**：substrate 匹配 **且** product 匹配 target →
  `TRANSFORMATION_MATCH`；模板**无** product pattern → `SUBSTRATE_MATCH`
  （诚实，不伪造）；模板**有** product pattern 但 target 不匹配 → 排除（是另一种转化）。

因此 substrate-only 总是产生 ≥ substrate+target 的候选集。

## Match Level（§六）

`TransformationMatchLevel`：

```text
TRANSFORMATION_MATCH   substrate + product 结构模板都与给定变化一致
SUBSTRATE_MATCH        只有 substrate 模式匹配
```

**SMARTS 匹配 ≠ 反应会发生 ≠ 实验验证过**（§十八）。UI 只写「Substrate matches
…」，绝不写「SN2 will happen」或「Recommended reaction」。

## 结果模型（§七）

`TransformationCandidate`：`reaction_template`、`match_level`、`transformation`、
`matched_substrate_atoms`、`matched_product_atoms`、`evidence_count`、
`evidence_summary`、`notes`。**无** success_probability / yield_prediction /
best_reaction_score。

## 排序（§八/§九）

确定性：`TRANSFORMATION_MATCH` > `SUBSTRATE_MATCH` → `evidence_count` 降序 →
`template_id` 升序。Evidence 只辅助排序，**不**改变化学匹配等级，**不**表示
「A is better / A will work / Most likely reaction」。

## Evidence 集成（§十/§十一）

`evidence_count` + `evidence_summary`（如 `{curated: 2, literature: 1}`）来自**本地**
`ReactionEvidenceService`。搜索**不**联网、**不**调用 Crossref/OpenAlex/Unpaywall、
**不**自动为模板搜索文献。

## API（§十二/§十三/§十四）

```text
GET /api/transformations/search?substrate=CCO
GET /api/transformations/search?substrate=CCO&target=CCCl
```

- 非法 SMILES → 结构化错误 `INVALID_MOLECULE`(400)，无 RDKit stack trace。
- 空结果 → `200 {candidates: []}`（不是服务器错误）。

## UI（§十五/§十六）

在既有 **Molecule Explorer** 内新增「Transformations — Candidate Reactions」区：
substrate = 当前分子，可输入 target；显示候选（`Transformation match` /
`Substrate match`、transformation、evidence count），点击经「View Reaction」进入
已有 **Reaction Detail**。无独立大页面、无结构编辑器（§十七）。

## 性能（§二十一）

对当前 46 条模板简单遍历即可，无向量库/索引服务；结构允许未来扩展。

## 测试

`tests/reaction/test_transformation_search.py`（11）：substrate-only、substrate+target
（无 product pattern → substrate_match 且带诚实 note）、substrate-only ⊇
substrate+target、注入 product-pattern 模板验证 `TRANSFORMATION_MATCH`、product
不匹配 target → 排除、排序（transformation 优先 + 稳定）、非法 substrate/target
→ `NormalizationError`（API 映射为 `INVALID_MOLECULE`）、evidence 集成（count +
summary，不改等级）、API（正常/空/400）。

架构测试 +1：`services/transformation_search.py` 不得 import providers/sources/
agent/llm/httpx/openai/numpy 等（无 AI、无网络）。

## 未做（§二十/§二十四/§二十五）

不伪造/补齐 product SMARTS、不增加 Reaction Library 内容、不实现候选打分/最佳反应
排序/路线搜索/反应图/AI 推理。

## 验收（§二十六）

1. `CCO` → 哪些模板的 substrate pattern 匹配 ✓
2. `CCO → CCCl` → 哪些模板 substrate+product 匹配（当前库诚实标 substrate_match）✓
3. 无 product pattern 的模板 → 诚实显示 `substrate_match only` ✓
4. 候选同时显示 Evidence count + 可进 Reaction Detail ✓
5. 全流程不联网、不用 AI、不自动搜文献、不声称反应一定成功 ✓

**Stage 5.1 到此停止，不进入 Stage 5.2。**

# AI Context（Stage 6.1）

## 目标

把确定性的化学数据整理成 **AI 可消费的结构化 Context**：

```text
Molecule + Reaction + Evidence + Literature + Graph + Route
        ↓
   AI Context（Fact Assembly）
        ↓
   future AI reasoning（6.2+）
```

本阶段**不**实现 AI 推理：无 Modification Analysis / Route Reasoning /
Evidence-aware Agent / LLM 调用 / AI 反应预测 / AI 路线规划。

## 定位（§一/§二）

`Chemistry Tool First, AI Second`。AI Context 是**事实组装（Fact Assembly）**，
不是创造化学事实；它是确定性化学层与未来 AI 层之间的 **Adapter / Context
Boundary**。它**读取** Molecule / ReactionTemplate / ReactionEvidence / Paper /
ReactionGraph / RouteExplorerResult，但**不修改**这些领域对象。

## AIContext 是结构化对象（§三/§四）

```text
AIContext
├── context_version      "crea-ai-context-v1"（显式稳定 schema 版本，§5）
├── created_at
├── query                （用户输入；不据其推测目标/反应/路线，§41）
├── source_scope         （本次请求覆盖范围，§24/§26）
├── molecules   [ContextMolecule]
├── reactions   [ContextReaction]
├── matches     [ContextReactionMatch]
├── evidence    [ContextEvidence]
├── literature  [ContextLiterature]
├── graphs      [ContextGraph]
├── routes      [ContextRoute]
├── warnings    [ContextWarning]
└── limitations [str]
```

**不含** `ai_answer` / `recommendation` / `reasoning` / `confidence` /
`prediction`——它是输入，不是 AI 输出（测试钉住）。**不**把数据库对象 `str()` 后
直接喂给 LLM。

## Molecule（§六/§七/§八）

来自现有 `ChemicalEntity`：`entity_id / identity_key / standard_inchikey / smiles /
formula / molecular_weight / formal_charge / atom_count / heavy_atom_count /
ring_count / names / name_status / functional_groups / scaffold`。

- **身份**以 Standard InChI / InChIKey 为核心；SMILES 只是 representation。
- **不猜名称**：无可靠名称 → `name_status="unavailable"`。
- 官能团/骨架**复用** `chem.functional_groups` / `chem.scaffold`，**不**重算第二套性质。

## Reaction（§九/§十）

来自 `ReactionTemplate`：`template_id / name / class_family / reaction_class /
transformation / mechanism_class / reagents / conditions / selectivity /
limitations / substrate_patterns / product_patterns / reaction_smarts /
evidence_count`。**未**新建 Reaction Model。围绕某 molecule 时附 `matches` 的
`match_level` / `matched_substrate_atoms`（复用 5.1）。

## Evidence ↔ Literature（§十三）

保留关系，不平铺：

```json
{"evidence_id": "...", "evidence_type": "transformation_support",
 "support_scope": "...", "literature_id": "P123"}
```

## FulltextPermission 独立（§十四）

Context 含 `oa_status` / `fulltext_permission`，但 **Permission ≠ Evidence**：
`fulltext_permission=unknown` 不表示 evidence 无效、paper 不可用。

## Scope（§二十四/§二十五/§二十六）

- molecule 请求 → molecule + 直接匹配 reactions + 那些 reactions 的 evidence +
  evidence 的 literature。**不**自动加载整个库。
- `include_graph=true` 才加 graph；`include_routes=true` 才加 routes。
- 序列化**稳定**：molecules 按 entity_id、reactions 按 template_id、evidence 按
  evidence_id、literature 按 paper_id、warnings 按 (code, ref)（§二十七）。

## Integrity / Diagnostics（§三十四/§三十五/§三十六）

- `check_integrity(context)`：evidence 引用的 literature / reaction、match 引用的
  reaction 必须在 context 内；否则 `ContextIntegrityError`（半残 Context 不返回）。
- 缺失（请求的 molecule/reaction 不在本地）→ **warning + Context incomplete**，
  不自动联网/创建/AI 补全。
- `warnings` 例如 `PRODUCT_STRUCTURE_UNAVAILABLE` / `MOLECULE_NOT_FOUND` /
  `REACTION_TEMPLATE_NOT_FOUND`。

## 不重塑事实（§三十七/§三十八/§三十九/§四十）

- 保留既有 Literature merge / `FieldResolution` / provenance，不自行挑「最合理」的值。
- **不**语义强化（`support_scope` 原样保留，不改写成「证明…」）。
- **不**删除化学细节（regioselectivity / stereoselectivity / limitations /
  conditions / reagents 尽量保留）。
- 本阶段不做 token 压缩 / 摘要 / embedding / vectorization。

## 与 agent/live.py 的关系（§三十一）

`agent/live.py` 保持不变；**不**与 AIContext 合并。正确关系是未来
`AIContext → Agent/AI layer`，而不是 `Agent → 直接查询所有数据库`。

## API（§三十二/§三十三）

```text
POST /api/ai/context
     {"molecule_ids": [...], "molecule_smiles": [...],
      "reaction_template_ids": [...], "include_graph": bool,
      "include_routes": bool, "route_target_smiles": "...", "query": "..."}
→ {"context_version": "crea-ai-context-v1", "context": {...}}
```

只返回 **context**，**不**返回 answer。Integrity 失败 → 422 `CONTEXT_INTEGRITY_ERROR`。
同时提供稳定的 Python service API：`AIContextService(kb).build(request)`。

## 未做（§一/§三十）

Modification Analysis、Route Reasoning、Evidence-aware Agent、LLM 调用、AI 预测、
Prompt Template / system prompt / persona / chain-of-thought。

## 测试

`tests/ai/test_ai_context.py`（16）：molecule 身份、名称不猜、context_version、
reaction 细节保留、match level、evidence↔literature 关系、permission 独立、无
answer/recommendation 字段、排序稳定、scope 受限、graph 仅按需、缺失→warning、
PRODUCT_STRUCTURE_UNAVAILABLE、dangling literature/reaction 检测、API。

架构测试 +1：`services/ai_context.py` 不 import llm/agent/providers/httpx 等。

## 停止

**Stage 6.1 completed.** 只完成 AI Context（fact assembly）。不进入 Stage 6.2
（Modification Analysis）/ 6.3（Route Reasoning）/ 6.4（Evidence-aware Agent）。

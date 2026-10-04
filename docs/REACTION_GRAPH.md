# Reaction Graph（Stage 5.3）

## 目标

基于已有本地数据，实现一个**确定性、可解释、可追溯**的化学知识图：

```text
Molecule ──SUBSTRATE_OF──> Reaction ──SUPPORTED_BY──> Literature
```

**Knowledge Representation，不是 Knowledge Generation**（§9）：图只表达已有事实，
不推导新化学事实。

## 节点与边（§六/§七）

**节点**：`Molecule` / `Reaction` / `Literature`（Evidence 作为边 metadata，不强制为
节点）。

**边**：

| 边 | 语义 |
|---|---|
| `SUBSTRATE_OF` | Molecule → Reaction：该 molecule 与 template 的 substrate pattern **匹配**（≠ 反应会发生，§8） |
| `SUPPORTED_BY` | Reaction → Literature：经 `ReactionEvidence`（携带 evidence_id/type/scope/status/locator，provenance 不丢） |
| `SOURCED_FROM` | Evidence → Paper |
| `PRODUCES` | Reaction → Molecule：**仅当 product 结构可确定性得到**时才建立 |

## 最大的边界：不猜产物（§三十/§三十一）

当前 46 条 `ReactionTemplate.product_patterns` **全为空**，因此：

- **永不**创建 `PRODUCES` 边；图给出 diagnostic：`product structure unavailable`。
- **不**从反应名或 substrate 猜 product；**不**用 LLM 生成 product SMARTS；**不**修改
  Reaction Library、**不**补 product pattern。

## 数据来源与持久化（§十/§十一/§三十七）

图由已有本地数据构建：`MoleculeLibrary` / `ReactionTemplate` / `ReactionEvidenceService`
/ `Paper`。**Derived / transient**——不建 graph database（无 Neo4j/NetworkX-as-store/
RDF），不引入外部图服务。**完全离线**：不调用 Crossref/OpenAlex/Unpaywall/PubChem。

## Traversal（§十七/§十八）

`depth` 默认 1，**上限 2**（`MAX_DEPTH=2`，超出被 clamp 而非服从）；API 用
`Query(ge=1, le=2)` 拒绝越界。不实现 `depth=unlimited`，**不让 traversal 演变成
Route Explorer**（§二十七）。

## Service（§十二）

`ReactionGraphService(kb)`：

- `molecule_graph(entity_id, depth)`：molecule → 匹配的 ReactionTemplate（复用
  **TransformationSearchService**，唯一结构匹配入口）→（depth 2）Literature。
- `reaction_graph(template_id, depth)`：reaction → literature evidence；depth 2 加
  本地库中匹配的 substrate molecules。
- `literature_graph(paper_id, depth)`：literature → 引用它的 reactions（复用
  **Stage 4.4 反向关系**，不重新查文献）。

## 确定性（§三十三）

节点按 id 排序、边按 (source, target, type) 排序；同输入多次请求返回稳定结果。

## API（§三十二）

```text
GET /api/reaction-graph/molecule/{entity_id}?depth=1|2
GET /api/reaction-graph/reaction/{template_id}?depth=1|2
GET /api/reaction-graph/literature/{paper_id}?depth=1|2
```

不存在 → 404（`MOLECULE_NOT_FOUND` / `TEMPLATE_NOT_FOUND` / `LITERATURE_NOT_FOUND`）。

## 节点 metadata（§二十一–§二十四）

- Molecule：entity_id / canonical_smiles / inchikey / names / identity_source
  （label = 名称或 SMILES/InChIKey，**不编造名称**）。
- Reaction：template_id / name / reaction_class / class_family / mechanism_class /
  match_level / evidence_count。
- Literature：paper_id / title / year / journal / doi（缺失不猜）。
- 边：SUBSTRATE_OF → match_level + matched_atoms；SUPPORTED_BY → evidence_id /
  evidence_type / support_scope / status / locator。

## UI（§二十五/§二十六）

**Molecule Explorer** 内新增「Reaction Graph」区：`Build Reaction Graph` → 无依赖
**SVG** 渲染（无大型图库/WebGL）→ 三列布局（Molecule / Reaction / Literature），
节点可点击：Molecule → Molecule Explorer，Reaction → Reaction Detail，Literature →
Literature Detail。**无 Route Explorer**（§二十七）、**无图推荐**（§二十八）。

## Evidence 权重（§二十九）

`evidence_count=10` **不**改变边类型——仍是 `SUPPORTED_BY`，不是
`STRONGLY_SUPPORTED_BY`（无正式 Evidence Weight 模型）。

## 测试

`tests/reaction/test_reaction_graph.py`（12）：molecule depth1（有 reaction 无
literature）、depth2（加 literature）、SUBSTRATE_OF 语义（match_level）、reaction
根（literature + 本地 substrate 分子）、**永不产生 PRODUCES** + diagnostic、literature
根反向、depth clamp、确定性、三种 404、API（含 depth=99 → 422）。

架构测试 +1：`services/reaction_graph.py` 不 import providers/sources/agent/llm/
httpx/neo4j/networkx/rdflib。

## 未做（§二十七/§二十八/§三十八）

Route Explorer（5.4）、graph recommendation、graph database、修改 ReactionTemplate
数据 / 增加 Reaction Library / 补 product SMARTS。

**Stage 5.3 completed.** 到此停止——不进入 Stage 5.4（Route Explorer）或 Stage 6
（AI Assistant），尤其不把 traversal 变成 synthesis route planning。

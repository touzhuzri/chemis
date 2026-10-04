# Phase 3.8.2 Completion Report — Chemical Mention → Chemical Entity Resolution

## 1. 文件变更

| 文件 | 变更 |
|---|---|
| `src/crea/core/models/entity.py` | 扩展 `ChemicalMention`（`explicit_smiles`/`explicit_inchi`/`context_before`/`context_after`/`source_id`、`text` 别名、`MAX_CONTEXT_CHARS` 截断）；`MentionResolutionStatus` 增加 `NOT_FOUND` |
| `src/crea/core/models/mention_resolution.py` | **新增**：`ChemicalMentionResolution`、`MentionResolutionEvidence`、`MentionResolutionBasis`、`MentionCandidateInfo`、`ContextReference`、`MENTION_RESOLUTION_VERSION` |
| `src/crea/services/mention_resolution.py` | **新增**：`ChemicalMentionResolver`、`extract_chemical_mentions`、`ParentheticalContext`、`StaticMentionContext`、`is_numbered_reference` |
| `tests/architecture/test_layering.py` | 边界测试扩展至 mention 层两个文件 |
| `tests/unit/test_mention_resolution.py` | **新增** 24 测试 |
| `tests/integration_offline/test_mention_resolution.py` | **新增** 5 测试 |
| `tests/live/test_mention_resolution_live.py` | **新增** 1 opt-in live 测试 |
| `docs/PHASE38_2_COMPLETION_REPORT.md` | 本文件 |

未动：`entity_registry.py`（3.8.1 的 registry 原样，仅经公有 API 消费）、identity 服务/policy、Evidence/RouteEvidence、RDKit、OpenAlex、AiZynthFinder、pyproject、.env、任何 provider 的 HTTP 客户端。

## 2. 新增模型

- **`ChemicalMention`（扩展，不推翻）**：原有 `mention_id/raw_text/normalized_text/entity_id/resolution_status/source/locator/evidence_refs` 不变；新增 `explicit_smiles`/`explicit_inchi`（用户显式结构，§23）、`context_before`/`context_after`（上下文窗口，`MAX_CONTEXT_CHARS = 2000` 截断，§14）、`source_id`（文献/文档作用域，§17）。`text=` 作为 `raw_text` 的别名。
- **`MentionResolutionBasis`**：`EXPLICIT_STRUCTURE / REGISTRY_NAME_MATCH / REGISTRY_EXTERNAL_ID / PROVIDER_NAME_MATCH / PROVIDER_EXTERNAL_ID / CONTEXTUAL_REFERENCE / UNRESOLVED`。
- **`MentionResolutionEvidence`**（§15 的 "ChemicalMentionEvidence 或等价模型"）：basis、identity_key、source/source_id、`context_quote`、locator、`evidence_refs` + `unresolved_evidence_refs`、既有 `Provenance`、notes（人读链条，如 "explicit structure + RDKit validation + identity key"）。
- **`MentionCandidateInfo`**（§12）：候选的 name/SMILES/InChI/InChIKey/external ids；`entity_id` 仅当该候选的 identity key **已在** registry 中（lookup，绝不副作用注册）。
- **`ContextReference`**（§13）：name 或结构 + `context_quote` + source/source_id/locator。
- **`ChemicalMentionResolution`**：mention（已更新）、status、entity_id、basis、evidence、candidates；模型级校验"非 RESOLVED 不得带 entity_id"。

## 3. 新增 service

`ChemicalMentionResolver`（`services/mention_resolution.py`）+ 轻量确定性抽取器 `extract_chemical_mentions` + 两个上下文源（`ParentheticalContext`、`StaticMentionContext`）。无 LLM、无网络客户端、无第二套 identity hash。

## 4. provider 复用方式

完全复用 Phase 3.6 的 `IdentityResolutionService(compound_provider=...)` ——名字/InChI/InChIKey/CID 的解析都经它到达 `CompoundProvider` 协议（测试用 `FakeCompoundProvider`，live 用既有 `PubChemProvider(HttpxTransport)`）。**没有新增 HTTP client，没有重复实现 PubChem**；mention→PubChem lookup 只是把 mention 文本作为 `StructureInput(kind=NAME/PUBCHEM_CID)` 交给 identity service（§10/§11）。PubChem CID 不直接建 entity：始终 `PubChem → identity service → ChemicalEntityRegistry`。

## 5. mention extraction 规则（§20–22）

`extract_chemical_mentions(text, known_names=..., source=, source_id=)`，三类模式，无化学 NLP：
- **编号引用**：`\b(?:compound|intermediate|product|substrate)\s+(\d+[a-z]?)\b`，case insensitive（compound 7 / compound 7a / intermediate 3b / product 4 / substrate 8 / compound 12a）。
- **外部标识**：`PubChem CID <n>`。
- **已知名**：调用方提供的可信名单（entity names、provider 验证过的名称、显式候选名），词边界、case insensitive、长者优先。

排除集 `ANALYTICS_EXCLUSIONS = {NMR, HPLC, GC, TLC, MS, IR, UV}`（§22，显式小集合，不做大词典）。`7 mL / 7 h / 80 °C / 82%` 等裸数字单位**不会**成为 mention（§21：数字只有在关键字领出时才是 mention）。重叠匹配取最早最长。`context_before/after` 各截断至 2000 字符（§14）。

## 6. resolution pipeline（§4/§9/§23）

顺序（确定性，便宜路径在前）：
1. **显式结构**（mention 自带 SMILES/InChI，最高依据）→ identity service → registry → CHEM-id（basis `EXPLICIT_STRUCTURE`）。
2. **外部标识**（PubChem CID）→ 先 registry `find_by_external_id`（`REGISTRY_EXTERNAL_ID`，零网络）→ 否则 provider 经 identity policy（`PROVIDER_EXTERNAL_ID`）。
3. **registry 名称匹配**（`REGISTRY_NAME_MATCH`，零网络；命中多个 entity → AMBIGUOUS）。
4. **编号引用** → 上下文源（括号定义 / 显式表）→ 定义（名或结构）→ identity service → registry（`CONTEXTUAL_REFERENCE`，保存 `context_quote`/source/locator）；无上下文 → UNRESOLVED。
5. **普通名称** → 可信 provider（按原文拼写查找，NOT_FOUND 时回落大小写折叠的同一拼写——同一名称的另一种写法，不是猜测）→ 一个结构 → registry（`PROVIDER_NAME_MATCH`）。

每一步出口都经 `IdentityResolutionService` + `identity key（crea-identity-key-v1）+ ChemicalEntityRegistry`；不存在第二套 hash（§24）。registry 已存在返回原 CHEM-id，不存在才新建（§25）。

## 7. ambiguity 行为（§12/D）

多候选（provider 多结构、或同名命中多个已注册 entity）→ `AMBIGUOUS`，`entity_id=None`，**candidates 全部保留**（结构/外部 ID/可能已注册的 entity_id），**绝不选第一个、绝不按排名选择**（源码无 `candidates[0]`/`[-1]`/切片，AST 静态测试钉住）。registry 保持空（不为候选注册 entity）。

## 8. contextual compound 行为（§13/C）

`compound 7` 默认 `UNRESOLVED`（论文局部编号，§17：source_id 作用域随 mention 传递，Paper A 的 compound 7 不会借用 Paper B 的定义）。上下文存在（如 `Compound 7 (phenylacetylene)`）→ `CONTEXTUAL_REFERENCE`，`context_quote`/source/source_id/locator 原样保存。**"compound 7" 绝不写入 entity.names**——另一篇论文里它可能完全是另一个化合物（§16）。

## 9. provenance（§15/§19/F）

每条 resolution 带 `MentionResolutionEvidence`：basis、identity_key、source/source_id、locator、`evidence_refs`（仅保留真实持有的；其余进 `unresolved_evidence_refs`，列出而非丢弃）、既有 `Provenance` 模型（tool="chemical-mention-resolver"，tool_version=MENTION_RESOLUTION_VERSION，source/source_id 同义）。用户 PDF 链可回溯：Document → Evidence → Mention → Entity（locator.document_id/page、`source=user_provided` 保留）。回答"为什么认为该 mention 是该 entity"的答案是链条（explicit SMILES + RDKit + identity key / PubChem name match + identity match + document evidence），绝不是 "LLM said so"。

## 10. tests

```
tests/unit/test_mention_resolution.py             24
tests/integration_offline/test_mention_resolution.py  5
tests/live/test_mention_resolution_live.py         1 (opt-in)
架构边界测试扩展至 4 个文件（mention 层）
```
覆盖 §44 验收 A–H（见 §15）与 §20–23 抽取规则、§43 静态禁令（AST 检查无 candidates 定位选择、无 `hash(` 派生 id、无 LLM import）。

## 11. live tests（§37）

`tests/live/test_mention_resolution_live.py`：`pytest.mark.live` + `os.environ.get("CREA_LIVE_TESTS") != "1"` skipif（与 Phase 3.1 既有 live 测试同款）。**门控只读 shell 环境变量**：`tests/conftest.py` 在 `load_local_env()` 之前捕获显式值、之后弹出 .env 中的同名值——.env 里躺着 `CREA_LIVE_TESTS=1` 不会使默认 pytest 进入 live（"文件提供凭据，shell 提供同意"）。live 仅验证 phenylacetylene / ethynylbenzene 经真实 PubChem 解析为同一 identity key / 同一 CHEM-id；不下载论文、图片，不调用 DECIMER。网络失败（ProviderError/ProviderUnavailable 或 UNRESOLVED）→ `pytest.skip(真实原因)`，绝不伪装成代码失败或静默通过。

## 12. architecture tests

`test_the_entity_layer_stays_inside_its_boundaries` 现覆盖 4 个文件（entity.py、mention_resolution.py 模型、entity_registry.py、mention_resolution.py service）：禁止 `crea.agent / crea.llm / crea.sources / crea.providers / rdkit`。mention resolver 因此在结构上无法用 LLM 制造身份。既有 services 层禁 rdkit、core 层禁 provider 的参数化测试对新文件自动生效。

## 13. 已知限制

- 名称识别依赖调用方提供的 known_names 与编号/外部标识模式；不是化学 NLP，召回率刻意低（§45 precision 优先）。
- 括号上下文（`ParentheticalContext`）只读 mention 紧邻的括号定义；scheme/figure/table/SI 的定义需经 `StaticMentionContext` 表或未来的 Document→Evidence 管道提供。
- `NOT_FOUND` 与 `UNAVAILABLE` 在 mention 层合并为 UNRESOLVED（源不可达）与 NOT_FOUND（源答"无此条目"）；identity 层语义未改。
- 大小写折叠回落只对 NAME 生效且仅在 NOT_FOUND 时尝试；不改写用户拼写。
- 未实现 Reaction Extraction（3.8.3 范围）。

## 14. 是否存在 blocking issue

**无。** 过程中的三处修正均为增量小改（新代码内）：(a) 名称查找误用小写 normalized_text 导致大小写不匹配——改为按原文拼写查找、NOT_FOUND 时回落折叠写法；(b) `_ambiguous` 把 entity_id 误塞进 name 字段；(c) 静态禁令检查误命中 docstring 中的禁令示例——改为 AST 检查。3.8.1 的设计与语义零改动。

## 15. 下一阶段建议

3.8.3 Reaction Extraction：`Paper → ChemicalMention → ChemicalEntity` 已就绪，可将反应式中的 reactants/reagents/products 表示为 mention→entity 引用；建议沿用同一原则——无法确立的实体保持 UNRESOLVED/AMBIGUOUS，宁缺毋滥。

---

## §44 验收对照

- **A 普通名称 → Entity**：PASS（`test_A_a_plain_name_becomes_the_entity_the_provider_established`）
- **B 同义词 → 同一 CHEM-ID**：PASS（`test_B_a_synonym_of_an_established_identity_is_the_same_entity`、集成 `test_a_paper_passage_walks_from_text_to_entities`）
- **C compound 7 不被错误全球化**：PASS（默认 UNRESOLVED；上下文解析后仍不进 entity.names；paper 作用域不串）
- **D 多候选严格 AMBIGUOUS**：PASS（3 候选全部保留、无一被选、registry 空、无 candidates[0]）
- **E 无结构不建 Name-only Entity**：PASS（NOT_FOUND/UNRESOLVED，registry 空）
- **F 用户 PDF provenance 保留**：PASS（locator/source/source_id/evidence_refs/Provenance 全保留，未解析引用列出不丢）
- **G LLM 无法制造身份**：PASS（mention 层无任何 LLM import（架构测试+AST），无 `hash(mention.text)`、无候选定位选择）
- **H 3.8.1 行为保持**：PASS（3.8.1 全部测试未改且通过；identity key 仍为 `crea-identity-key-v1`）

## 测试结果

见随附 `pytest -q` 输出。

## 最终结论

**Phase 3.8.2 PASS**

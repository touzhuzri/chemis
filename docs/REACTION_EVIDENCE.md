# Reaction Evidence Model（Stage 3.4）

## 目标

为 `ReactionTemplate` 建立正式的 Evidence Model，使系统能表达：

```text
ReactionTemplate → Evidence → Source → Evidence Type → 具体支持内容
```

即从「CREA curated 了一条反应模板」升级为「CREA 记录这条模板由什么来源、
什么证据类型支持」。本阶段**不是**完整 Literature System，只建立
`Reaction ↔ Evidence` 的稳定领域模型与接口。

## Evidence ≠ Provenance（§四）

- **Provenance** 回答「这个对象/字段来自哪里？」（`source = CURATED`）。
- **ReactionEvidence** 回答「这个来源具体支持 ReactionTemplate 的哪一部分？」
  （`evidence_type = TRANSFORMATION_SUPPORT`，`support_scope = "primary
  alcohol → aldehyde"`）。

两者不是同一概念，Provenance **不**代替 Evidence。

## 模型（`core/models/reaction_evidence.py`）

```text
ReactionEvidence
├── evidence_id            CREA 内部记录 ID（≠ DOI / 文献标准 ID）
├── reaction_template_id   1 个 template : N 条 evidence
├── evidence_type          ReactionEvidenceType（见下）
├── source                 EvidenceSourceReference（source_type/source_id/
│                          title/url/locator/retrieved_at）
├── support_scope          "支持什么"（自由文本，暂不做复杂 ontology）
├── status                 EvidenceStatus（复用 Stage 3.1，见下）
├── note                   允许记录说明，但不把推测当证据
└── provenance             复用现有 Provenance
```

**有意的抽象层**：它比 `core/models/evidence.py::Evidence`（文献引文模型，要求
raw_quote + 真实 source_id）更抽象。一条 ReactionEvidence 可以有
`source_type = CURATED`、无 DOI、无论文，完全合法（§7/§15）。literature 层字段
（`AbstractSource`/`FulltextPermission`/`OAStatus`）**不**复制进来（§23）。

## EvidenceType（§七，描述「支持什么」）

```text
CURATED_KNOWLEDGE, TRANSFORMATION_SUPPORT, SUBSTRATE_SCOPE_SUPPORT,
PRODUCT_SUPPORT, CONDITION_SUPPORT, SELECTIVITY_SUPPORT,
MECHANISM_SUPPORT, LIMITATION_SUPPORT, CHARACTERIZATION_SUPPORT
```

一篇论文可以同时支持 `CONDITION_SUPPORT` 与 `SELECTIVITY_SUPPORT`——**literature
不是 EvidenceType**。

## EvidenceSourceType（§八/§十五）

```text
CURATED, LITERATURE, PROVIDER, USER_PROVIDED, EXPERIMENTAL, UNKNOWN
```

## EvidenceStatus（§十，复用 Stage 3.1）

`EvidenceStatus` 原地扩展（additive）+`PROVIDER_DERIVED`：
`UNKNOWN / CURATED / LITERATURE_SUPPORTED / USER_PROVIDED / PROVIDER_DERIVED`。
**未增加 `VERIFIED`**：没有验证工作流就不声称验证。

## CURATED 不是 Literature（§九）

46 条 seed 全部是 CURATED。**CURATED 不等于** LITERATURE_SUPPORTED；curated
knowledge 不自动提升为 literature evidence。

## 干净的做法（§二十六）：派生的 curated 自证据

不为 46 条各存一条重复记录。curated template 本身就是它的知识来源——服务在
读取时从模板自身派生一条 `CURATED_KNOWLEDGE` 自证据（`curated_evidence_for`）：
`source_type=curated`、`status=curated`、`support_scope=transformation`、
`note="Curated chemistry knowledge."`。**派生记录不持久化**，所以库不会长出 46
条重复行；无 DOI、无论文，绝不伪造成 literature。

## 持久化（§二十一）

SQLite 表 **`reaction_template_evidence`**（注意：`reaction_evidence` 已被
Stage D 占用，那是 Reaction→文献 Evidence 的链接表，概念不同，故分表）：
`evidence_id` PRIMARY KEY、`reaction_template_id` + 索引、`evidence_json`、
`created_at`。复用 `KnowledgeBase` / `SQLiteStore`，未引入新数据库系统。

## 服务与 API（§十九/§二十）

`services/reaction_evidence.py::ReactionEvidenceService`：
`list_evidence` / `get_evidence` / `attach_evidence` / `validate_evidence`。

```text
GET  /api/reaction-templates/{template_id}/evidence
POST /api/reaction-templates/{template_id}/evidence
```

未做完整 CRUD（Stage 4 再扩展）；前端不经 service 无法直接写库。

## 与文献的边界（§十五/§二十二/§二十九）

当前无 Crossref/OpenAlex/Unpaywall、无 DOI 检索、无 PDF、无爬虫。ReactionEvidence
可引用未来 LiteratureRecord，但**不**写入 crossref/openalex/unpaywall 专属字段
（通用 `source_type`/`source_id`/`external_references` 表达）。无 AI（§二十八）。

## Reaction Explorer（§十八）

Reaction Detail 增加 Evidence 区：显示 evidence_type / support_scope / source /
status。无记录时显示「No evidence records.」——绝不显示「No literature found.」
（Evidence ≠ Literature）。

## 层级保持（§二十七）

```text
ReactionTemplate  →  知识声明（"这个反应模板是什么"）
ReactionEvidence  →  支持记录（"为什么可以这样描述"）
Literature        →  来源（"哪篇文献具体支持"，未来）
```

## 测试

`tests/reaction/test_reaction_evidence.py`（8）：curated 自证据、curated ≠
literature、attach 校验存储、重开持久化、未知 template 结构化错误、非法 evidence
拒绝、template_id 不匹配拒绝、API（GET/POST/404/400）。

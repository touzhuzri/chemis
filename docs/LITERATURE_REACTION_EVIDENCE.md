# Literature ↔ Reaction Evidence（Stage 4.4）

## 目标

建立：

```text
ReactionTemplate → ReactionEvidence → LiteratureRecord
```

以及反向查询 `LiteratureRecord → Related Reaction Evidence`。

只有**关系建模、持久化、API、UI、测试**——无 PDF 下载/解析、无 OCR、无 AI、
无新 Provider。

## 架构原则（§一/§二/§三）

- **ReactionTemplate 不保存 DOI / paper_id / literature_ids**。关系只能经由
  已有的 `ReactionEvidence` 建立。
- **`ReactionEvidence` 不复制文献元数据**（无 title/doi/journal/year 字段）：
  title 等始终从 `Paper` 读取。（链接时写入的 `source.title` 只是显示便利，
  DOI/journal/year 只在 Paper 上。）
- **扩展现有模型，不建平行模型**：复用 `EvidenceSourceReference`
  （`source_type = LITERATURE`，`source_id = paper_id`）。**没有**创建
  `LiteratureEvidence` / `PaperEvidence` / `ReactionLiteratureEvidence`。

## EvidenceLocator（§四）

**新增**类型化 `EvidenceLocator`（取代此前的 `locator: dict`）：`page / section /
figure / table / scheme / supporting_information`，**全部可选**。它是「证据位置」，
**不是**证据正文。

## 禁止保存原文（§五）

新增模型不含 `raw_quote` / `full_quote` / `extracted_text` / `pdf_text` /
`ocr_text`（测试钉住）。不抓正文、不解析 PDF、不 OCR。

## 语义：Literature-linked ≠ VERIFIED（§六）

复用既有 `EvidenceStatus`（未新增状态）。`source_type == LITERATURE` **不**自动
升级为 VERIFIED/CONFIRMED/PROVEN。未引入 `AI_VERIFIED` 等。

## 仅本地关联（§七/§八）

`ReactionEvidenceService.link_literature(...)` 只做：

```text
paper_id 或 doi → normalize_doi → 本地 Literature Store 查找 → 建立 Evidence
```

本地不存在 → `LITERATURE_NOT_FOUND`。**绝不**为建立 Evidence 而调用
Crossref/OpenAlex/Unpaywall（架构测试 `test_literature_evidence_linking_is_local_not_a_provider`
钉住：`services/reaction_evidence.py` 不得 import provider/source/agent/llm/httpx）。
非 DOI 形式 → `INVALID_DOI`（`normalize_doi` 是语法级，故服务层额外校验 `10.` 形式）。

## 分层

```text
Literature Provider → Literature Record → Evidence Linking（本地）
```

## 持久化（§九）

复用 `reaction_template_evidence` 表（**未**建平行表）；**additive** 增
`source_type` / `source_id` 两列 + `(source_type, source_id)` 索引（guarded ALTER，
兼容既有库）。`source_id` 索引支撑反向查询。

## 去重（§十）

去重键 = `(reaction_template_id, source_type, source_id, evidence_type,
support_scope)`。**locator 不作为主键**（Figure 2 / Scheme 3 是同一证据）。重复
写入返回既有记录（`is_new=False`），**不**静默制造重复。

## Provenance（§十七）

Literature provenance 与 Evidence provenance **独立**：链接写入的是
`provenance.tool = "reaction-evidence-linker"`（记录**链接动作**），**不**复制
Paper 的 provenance。

## FulltextPermission 独立（§十八）

`MAY_RETRIEVE` **不**意味着某文献支持某模板；`UNKNOWN` 也**不**阻止建立
Literature Evidence。无任何自动逻辑把 permission 与 evidence 绑定。

## 禁止自动关联（§十九）

只实现 **Explicit Evidence Linking**：用户/受控流程明确指定
`Reaction + Paper + EvidenceType + SupportScope`。无 keyword→template 猜测、无
AI 自动建立、无 DOI→自动判断支持哪个反应。

## API

```text
POST /api/reaction-templates/{template_id}/evidence/literature
     {paper_id|doi, evidence_type, support_scope, locator?, status?, note?}   → {evidence, is_new}
GET  /api/literature/{literature_id}/reaction-evidence                       → {evidence, count}
```

错误：`LITERATURE_NOT_FOUND` / `TEMPLATE_NOT_FOUND` → 404；`INVALID_DOI` /
`INVALID_EVIDENCE` / `INVALID_LOCATOR` → 400。复用 `ReactionEvidenceError`，未新建
异常层。

## UI

- **Reaction Detail** → Evidence 区：列出证据（文献证据链接到文献详情），并提供
  「Link Literature Evidence」表单（DOI + type + scope，仅本地）。
- **Literature Detail**（`#/literature/{paperId}`）→ **Related Reaction Evidence**
  反向列表。

## 测试

`tests/reaction/test_literature_evidence.py`（15）：正常建立、DOI 解析、反向查询、
未知 DOI→`LITERATURE_NOT_FOUND`、非法 DOI→`INVALID_DOI`、未知模板→
`TEMPLATE_NOT_FOUND`、去重（不同 locator 同一证据）、不同 scope 视为不同证据、
一 paper 多模板 / 一模板多 paper、curated 不受影响、locator 可选且可序列化、
无 raw quote 字段、provenance 独立、API（link 200 / 404）。

架构测试 +1（无 provider coupling）。

## 未做（§二十二/§二十三 明确排除）

PDF 下载/解析、OCR、全文抽取、AI 文献分析/反应匹配、反应路线搜索、反应图、
路线规划、最佳反应推荐、批量文献抓取、新的 Crossref/OpenAlex/Unpaywall 功能。

## 验收（§二十三）

```text
ReactionTemplate → ReactionEvidence → Paper        稳定持久化 ✓
Paper → Related Reaction Evidence                  反向查询 ✓
DOI 经本地 Literature Record 建立关系 ✓
不存在的 DOI → LITERATURE_NOT_FOUND ✓
Evidence Linking 无网络请求 ✓（架构测试）
同一 Evidence 不重复写入 ✓
一篇 Paper 支持多个 ReactionTemplate ✓ / 一个模板多篇 Paper ✓
Curated Evidence 不受破坏 ✓
Literature provenance 与 Evidence provenance 独立 ✓
FulltextPermission 与 Evidence 独立 ✓
Locator 可选、可序列化 ✓ · 不保存 raw quote ✓ · 不涉及 PDF/OCR/AI ✓
```

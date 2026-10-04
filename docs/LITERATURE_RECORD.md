# Literature Record（Stage 4.1）

## 目标

建立 CREA 正式的 Literature Record 领域模型，把「论文/文献」从一个未来
provider 返回的 JSON，变成 CREA 内部稳定、可引用、可追踪的对象。

## 复用，不建第二套（§二/§三）

CREA **已有** `core/models/literature.py::Paper`——它就是 literature record：
identity / 书目元数据 / identifiers / sources / access / provenance 齐备。本阶段
**原地扩展** `Paper`，**不**新建 `LiteratureRecord` 类（避免第二套模型）。

## 身份分离（§四/§五/§六）

- `paper_id`：CREA **内部记录身份**，不是 DOI，不是国际文献 ID（§五）。
- `identifiers.doi`：DOI，**可选**。
- 两者分离：`paper.doi != paper.paper_id`。

## DOI normalization（§六/§七）

复用 `core/ids.py::normalize_doi`（trim、去 `https://doi.org/` 与 `doi:` 前缀、
去尾随标点、去 query/fragment、lowercase）。三种写法归一：

```text
10.1000/xyz  ·  doi:10.1000/xyz  ·  https://doi.org/10.1000/xyz  →  10.1000/xyz
```

存储层统一：`save_paper` 把 `doi` 列与 `identifiers_json` 都写成 canonical 形式
（不改调用方对象）；`find_paper_by_doi` 归一化查询后精确匹配。**不做网络验证**
——只做 syntactic normalization（§七）。

## 去重策略（§九/§二十六）

同一个 normalized DOI **只对应一条** LiteratureRecord：DB 层
`CREATE UNIQUE INDEX idx_papers_doi_unique ON papers(doi)`（NULL DOI 不限，SQLite
UNIQUE 允许多个 NULL），服务层 `LiteratureRecordService.create` 先查后写，返回
既有记录（`is_new=False`）。

**没有 DOI** 的工作仍然是一条记录（§八）；两条都无 DOI 的记录是两条——**不**发明
title+year+author 的「万能文献 ID」（§九）。

## 书目元数据（§十/§十一）

`title / authors(list[Author]) / year / journal / volume / issue / pages /
abstracts / language / publisher(type)` 全部可表达。`Author` 有
given/family/name/orcid/affiliation/sequence；**作者名字符串不是作者身份**，
ORCID 存在才记录，不自造 author ID（§十一）。

## External identifiers（§十二）

`Identifiers`（de-facto 的 `external_identifiers`）：`doi / openalex_id /
crossref_id / pmid / pmcid / mag_id / isbn`。**不**在 record 里硬编码
`crossref_record_json` / `openalex_json` / `unpaywall_json`；provider 原始事实留在
`upstream`（`UpstreamFacts`）。

## Source precedence（§十三）

复用既有 `AbstractSource` + `ABSTRACT_PREFERENCE`（publisher > crossref >
openalex_reconstructed > user_provided > unknown）。它是「同字段多来源时谁优先」，
**不**表示「谁更真实」。

## OAStatus / FulltextPermission（§十五/§十六/§十七）

**复用**既有枚举，不重写、不改 policy：

- `OAStatus`：UNKNOWN/GOLD/GREEN/HYBRID/BRONZE/DIAMOND/CLOSED。
- `FulltextPermission`：UNKNOWN/MAY_RETRIEVE/OA_NO_DOWNLOAD/SUBSCRIPTION_ONLY/
  NOT_PERMITTED，保持 fail-closed——`MAY_RETRIEVE` 仅在明确适用的许可证据下成立，
  证据不足是 `UNKNOWN`。
- **access URL ≠ permission**：`AccessAssessment.fulltext_url` 与 `permission`
  是两个字段；有 PDF URL 不代表 `MAY_RETRIEVE`。
- 本阶段只承载模型，**不判断**（不猜 OA、不下载 PDF、不改 policy）。

## Literature type（§十八）

`LiteratureType`（journal_article / conference_paper / preprint / book_chapter /
book / dataset / review / other / unknown）是 `Paper.literature_type` 属性——对
`upstream.type`（源stated、原文保留）的**类型化视图**，不是推断。未映射的类型=
OTHER，缺失=UNKNOWN。

## abstract 与 fulltext 分离（§十四）

`abstracts: list[AbstractRecord]`（按来源类型标注，OpenAlex 倒排索引重建的标
`is_reconstructed`）；全文相关在 `access`。两者不混。

## 不含 reaction-specific 字段（§二十四）

`Paper` 不含 `reaction_template_id` / `reaction_evidence_id`。关系由未来的
association 表达——本阶段只让它**可被引用**（§二十三），Stage 4.4 才建立真正的
ReactionEvidence ↔ LiteratureRecord 关系。

## Persistence / serialization（§二十五/§二十六）

复用 `papers` 表（`_paper_from_row` 从 `identifiers_json` 重建）。新增字段
（`crossref_id`、`language`）落在既有 JSON 列里，**无 schema 破坏**；仅新增一个
UNIQUE 索引。`model_dump(mode="json")` / `model_validate` 往返正常（datetime/enum/
嵌套 author/identifier 统一序列化）。

## 服务

`services/literature_record.py::LiteratureRecordService(kb)`：
`create`（DOI 去重）/ `get` / `find_by_doi` / `list` / `count`。

## 边界（§二十九）

无 Crossref/OpenAlex/Unpaywall HTTP、无 PDF retrieval、无 OCR、无 AI、无
Reaction/Route 新功能。本阶段从调用方已持有的元数据构造记录。

## 测试

`tests/literature/test_literature_record.py`（12）：DOI 三写法归一 + 标点/大小写、
同 DOI 不重复、无 DOI 仍可存且不强并、metadata/identifiers/access 往返、abstract
可空、permission 默认 UNKNOWN、literature_type 类型化视图、内部 ID ≠ DOI、
序列化往返、重开持久化、缺失记录报错。

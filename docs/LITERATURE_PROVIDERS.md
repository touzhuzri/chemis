# Literature Providers: Crossref + OpenAlex（Stage 4.2）

## 目标

让 CREA 从两个外部文献元数据源获取 LiteratureRecord：

```text
DOI / query → Provider → external metadata → LiteratureRecord
            → normalize / merge → field-level provenance → local persistence
```

只负责「发现并标准化文献元数据」，**不**判断全文权限（Stage 4.3）。

## 复用，不建第二套

项目**已有** `sources/crossref.py::CrossrefProvider`、`sources/openalex.py::
OpenAlexProvider`、`services/literature.py::LiteratureService`（provider federation +
partial-failure + DOI merge）、`services/literature_merge.py`（**字段级 provenance**：
`FieldResolution`/`FieldAlternative`/`MergePolicy.field_priority`）。本阶段**原地
扩展**，未重建。

## 本阶段新增

1. **配置（env，composition root）** —— `agent/live.py::build_literature_providers()`：
   - `CREA_CROSSREF_MAILTO` → Crossref polite pool（可选；无则匿名）。
   - `CREA_OPENALEX_API_KEY` / `CREA_OPENALEX_MAILTO` → OpenAlex（可选）。
   - key / email **不进源码、不进日志、不写进记录**（`redact_url` 已遮蔽 URL 中的
     `api_key`/`mailto`）。
2. **有界重试（service 层）** —— `services/literature_retry.py::RetryPolicy` +
   `call_with_retry`。默认 `max_retries=0`（**不改动既有 provider 契约**）；
   composition root 启用 `max_retries=2`。只重试 transient（`RateLimited`/
   `UpstreamError`），**不**重试 `NOT_FOUND`/`INVALID_REQUEST`（那是答案，不是故障）。
3. **API** —— `GET /api/literature/search?q=`、`GET /api/literature/doi/{doi}`、
   `GET /api/literature/{literature_id}`。
4. **UI** —— `#/literature` 最小 Literature Search 页（元数据 only）。

## 分层（§四十二 边界）

```text
Provider(sources/) → LiteratureService(services/) → LiteratureApplication(app/)
                                                    → API(api/) → UI(web/)
```

Provider **不** retry / 不 sleep（架构测试 `test_crossref_provider.py::
test_no_assumed_rate_is_hardcoded` 钉住了这一点）；重试是 **caller（service）** 的
决策。API 层不直接 fetch provider URL（provider 由 composition root 注入）。

## Federation / partial failure（§二十五/§二十六）

`LiteratureService` 已实现：一个 provider 失败（如 OpenAlex 超时）→
`PARTIAL_SUCCESS` + provider warning，Crossref 结果照常返回；全部失败 →
`ALL_PROVIDERS_FAILED`（**≠ `NO_RESULTS`**）。同一 normalized DOI 归并为**一条**
记录，`sources = [crossref, openalex]`。

## 身份 / 去重 / 字段级 provenance（§十八/§十九/§十七/§二十三）

- DOI normalization（复用 `core/ids.py::normalize_doi`）：`https://doi.org/…` 与
  `doi:…` 与裸 DOI 归一到同一 identity。
- 同 DOI → 同一 LiteratureRecord；无可靠身份时**不**按 title 强行合并。
- 字段冲突按既有 `MergePolicy.field_priority` / `ABSTRACT_PREFERENCE` 解决，
  保留每个来源的候选值（field-level provenance）。

## 权限边界（§三十六，不改）

Provider **不**设置 `FulltextPermission`。Crossref `license[]`、OpenAlex
`is_oa=true` 都只是 metadata，**不**改 `MAY_RETRIEVE`。权限判断属 Stage 4.3。
不下载 PDF、不接 Unpaywall-for-4.2、不做 bulk harvest。

## Offline（§二十七）

`--offline`：provider factory 返回 `{}` → 每次调用报
`PROVIDERS_UNAVAILABLE_OFFLINE`(503)。Molecule Explorer / Reaction Explorer /
本地库全部照常（测试钉住）。

## 错误模型（§二十四）

复用既有 `ProviderError` 分类：`RateLimited` / `ResourceNotFound` /
`UpstreamError` / `InvalidRequest` / `AuthenticationOrAuthorizationError` /
`InvalidUpstreamResponse`。`ProviderError` 增可选 `retry_after`（供将来的
transport 携带 `Retry-After`；当前 provider 不设置，重试用指数退避）。

## 测试

`tests/literature/test_literature_providers.py`（11）：provider 配置不泄露、同 DOI
归并、DOI lookup 归并、单 provider 失败不整体失败、全失败 ≠ 空结果、有界重试
恢复/有界/不重试答案、service 遵守 retry policy、permission 不被凭空设为
MAY_RETRIEVE、offline API 503。全部 `FakeTransport`，零网络。

> 真实网络测试：既有 `tests/live/` 下的 live suite（`CREA_LIVE_TESTS=1`）可选运行，
> 永不属于默认 `pytest`。

## 未做（§四十二 明确排除）

不下载 PDF、不判断 FulltextPermission、不接 Unpaywall（4.2 范围）、不接
ReactionEvidence、不接 AI、不做 bulk harvesting。

# Fulltext Access Policy（Stage 4.3）

## 目标

把文献记录 + Crossref license + OpenAlex OA + Unpaywall 聚合为**一个**严格的、
fail-closed 的 `FulltextPermission`：

```text
LiteratureRecord + Crossref license + OpenAlex OA + Unpaywall DOI metadata
    → Access Evidence → Access Policy → FulltextPermission
```

## 六个**不**同的概念（§三）

严格区分，绝不混同：

1. Free-to-read 2. Open Access 3. Fulltext URL 4. License
5. Permission to retrieve 6. Permission to text-mine

**URL ≠ permission。** Crossref 明确说明 full-text URL 的存在不能保证访问。

## 冻结的权限政策（§二，不改语义）

`core/policy/access.py::infer_permission`，三态：

- **MAY_RETRIEVE** —— 只有「明确、当前适用、足够具体、可支持程序化获取的许可/
  权利证据」时成立（即 OA 状态在 `_OA_WITH_LICENCE` **且** license 在
  `PERMISSIVE_SPDX` 白名单）。
- **UNKNOWN** —— fail-closed 默认：有 URL 无许可、OA=true 无许可、有 landing
  page、license URL 存在但适用范围不明、来源冲突……全部 UNKNOWN。
- **NOT_PERMITTED** —— 只有**明确**当前不允许的证据（OA=CLOSED）。

**绝不** UNKNOWN → NOT_PERMITTED，也**绝不** UNKNOWN → MAY_RETRIEVE。

## 复用，不建第二套

项目**已有**（Phase 2/3）：`core/policy/access.py`（`PERMISSIVE_SPDX` 白名单、
`license_is_permissive`、`infer_permission`、`may_retrieve`、`gate_retrieval`）、
`sources/unpaywall.py::UnpaywallProvider`（DOI-only、typed errors、`license=null`
诚实处理）、`services/access_reconciliation.py`（`reconcile`，`POLICY_VERSION =
access-v1`，`RESTRICTED_CONTENT_VERSIONS` = tdm/am/similar-check，source priority）。
本阶段**原地使用**，未重建 `AccessPolicyV2`。

## 本阶段新增（additive）

1. `AccessEvidence` 模型 + `AccessAssessment.policy_version/reasons/access_evidence`
   —— 让「为什么是 MAY_RETRIEVE」可回答（来源 + retrieved_at），**无置信分数、
   无概率**（§十三）。
2. `infer_permission` 填充 `policy_version`（`access-policy-v1`）、人类可读
   `reasons`、结构化 `access_evidence`。**判定结果不变**（fail-closed 保持）。
3. Unpaywall 接入文献路径：`agent/live.py::build_unpaywall_provider()`（读
   `CREA_UNPAYWALL_EMAIL`，不硬编码）+ `LiteratureApplication.access(doi)`。
4. API：`GET /api/literature/doi/{doi}/access`。
5. UI：Literature Detail 的 **Access** 区。

## Unpaywall（§四/§五/§六）

- 只做 DOI lookup：`GET /v2/{doi}?email=...`。**禁止**已退役的 `/v2/search`
  （`UnpaywallProvider.search` 主动拒绝，返回 `ProviderUnavailable`）。
- email 配置走 `CREA_UNPAYWALL_EMAIL`（composition root）；无 email → `missing_key`
  （正常降级，permission 保持 UNKNOWN，不崩溃）。
- OA location 记录为 fact，**不**抓取（无 `url_for_pdf` fetch）。

## License 语义（§九/§十六/§十七）

- **不自行定义开放许可证白名单**：复用既有 `PERMISSIVE_SPDX`（CC0 / CC-BY /
  CC-BY-SA 家族；未声明版本时按家族处理，因为 provider 常常只给家族名）。
- 一个 DOI 可有多条 license（VoR/AM/TDM）；**不**取「第一条」。
- `license_url` 存在 ≠ MAY_RETRIEVE；无法确认内容/版本/用途则 UNKNOWN（§十七）。
  本阶段**不**爬取 license 页面、**不**用 LLM 猜许可证含义。

## Version-aware（§十八）

`AccessAssessment.oa_version` 记录目标版本；TDM/AM 等窄授权（
`RESTRICTED_CONTENT_VERSIONS`）不继承为 VoR 的许可。

## 冲突（§二十）

Crossref license=unknown + OpenAlex is_oa=true + Unpaywall license=null →
**UNKNOWN**。来源之间冲突 → UNKNOWN，除非有明确 source precedence + 更强证据。

## Offline（§三十一）

`--offline`：无 Unpaywall 网络访问；Access 评估用已有 metadata，permission 保持
UNKNOWN。整个软件不因 Unpaywall 不可用而启动失败。

## 测试

`tests/literature/test_fulltext_access.py`（12）：

- Rule A/B：permissive license + OA → MAY_RETRIEVE（带 reasons/evidence/policy_version）。
- Rule C **no false permission**：PDF URL alone → UNKNOWN；OA status alone → UNKNOWN；
  BRONZE+landing page → UNKNOWN。
- Rule D：CLOSED → NOT_PERMITTED；UNKNOWN 绝不因缺 URL 变成 NOT_PERMITTED。
- Unpaywall：green + license=null → UNKNOWN；无 email 优雅降级；无 search endpoint。
- API：offline → 503。

既有 `test_access_reconciliation.py` / `test_unpaywall_provider.py` /
`test_evidence_end_to_end.py` 全部继续通过（零回归，UNKNOWN 未被大量转成
MAY_RETRIEVE）。

## 未做（§三十三/§三十六 明确排除）

不下载 PDF、不实现 PDF parser、不 OCR、不接 LLM 判断许可证、不接 ReactionEvidence
linking、不做 bulk harvesting。

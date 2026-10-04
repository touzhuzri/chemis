# PubChem Exact Structure Provider（Stage 2.3）

## 目标

把 CREA 已有的标准化化学身份（Standard InChIKey）与 PubChem 的标准化记录
可靠对应：InChIKey → PubChem exact lookup → CID → 基本性质 → 身份验证 →
provenance →（可选）显式导入本地库。

## 查询策略与身份验证（§四/§五）

1. CREA 生成 Standard InChIKey（`normalize`，唯一 RDKit 归属地）。
2. PubChem `compound/inchikey/{key}/property/.../JSON` 精确查询。
3. 读取返回的 InChIKey，与 CREA 的 Standard InChIKey 验证。
4. 一致 → `identity_match = true`；不一致 → 保留双方身份 + CID + URL，
   **不自动导入、不偷偷修正**，返回 `identity_match = false`。

PubChem CID 只是 `external_identifiers`（`{pubchem: {cid}}`），不是 chemical
identity，不是 `molecule_id`（§六）。

## Provider 分层（§十五）

```text
PubChemExactProvider（providers/pubchem_exact.py，身份验证）
   ↓
PubChemSource（sources/pubchem.py，PUG REST + 错误映射 + 限速/重试）
   ↓
HttpxTransport（sources/http.py，唯一网络归属地）
```

`app/pubchem.py` 不 import transport；provider 由 composition root
（`agent/live.py::build_pubchem_exact_provider`）构建后注入。

## 请求频率与错误（§十三/§十四）

- `min_request_interval = 0.2s`（≤5 req/s 下限，不保证恰好达到）；`max_retries=2`
  + 指数退避，仅重试 transient（429/5xx）；404/400 是答案，永不重试。
- 错误结构化：`PROVIDER_NOT_FOUND`(404) / `PROVIDER_RATE_LIMITED`(429) /
  `PROVIDER_UNAVAILABLE`(503) / `PROVIDER_INVALID_RESPONSE`(502) /
  `PROVIDER_IDENTITY_MISMATCH`(409)。不返回 traceback。

## 查询与导入分离（§九/§十）

Lookup 只查询；只有显式「Import to Library」才导入。导入时重新验证身份、
按 Standard InChIKey 去重、写 `source_type=PUBCHEM` + CID + 外部标识符，
不覆盖本地结构事实。

## License / Data Rights（§十二）

**Provider lookup preserves source provenance; licensing status is not
inferred from PubChem presence alone.** PubChem 数据来自大量贡献者，具体数据
源可能有不同许可与复用条件。CREA 记录 PubChem source / CID / URL，不做 bulk
redistribution、不做大规模复制，不宣称所有字段具有统一许可证。

## Offline（§二十二）

`--offline` 下：provider 为 None，lookup 返回 `PROVIDER_UNAVAILABLE`(503)；
本地库 / Molecule Explorer / Similarity / ReactionMatcher / 3D 全部正常。

## 测试

`tests/providers/test_pubchem_exact.py`（9）：身份匹配、身份不匹配不纠正、
not-found、rate-limited、unavailable、bounded retry（503→成功）、lookup 与
import 分离 + 去重、mismatch 拒绝导入、offline API 返回 503。全部用
`FakeTransport`，零网络依赖。

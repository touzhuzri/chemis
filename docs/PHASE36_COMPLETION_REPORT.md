# Phase 3.6 Completion Report

## Phase 3.6 Status

**Complete.** Stopped at the phase boundary; Phase 3.7 not started.

## Baseline

Recorded before any Phase 3.6 change:

```
1002 passed, 48 skipped
```

## New tests

121 tests in the six files §20 names, plus 3 appended to the PubChem provider
file for §21/§22.

| File | Tests | Covers |
|---|---|---|
| `tests/unit/test_identity_models.py` | 15 | §1–§2 the model grammar: the seven statuses, `candidate ≠ resolved`, `StructureInput.kind` by name or value, `ImageProvenance` carries no bytes, `require_resolved` refuses |
| `tests/unit/test_pubchem_provider.py` | 23 | §5–§7 the PUG REST adapter: routing, form bodies vs path segments, `NotFound` → `[]` vs every other error raised, ListKey follow, synonym enrichment, §21 provenance, §22 offline-by-default |
| `tests/unit/test_structure_resolution.py` | 22 | §3–§4 deterministic parse, `INVALID` for a bad SMILES/InChI, InChIKey is looked up and never reverse-derived, `NOT_FOUND` ≠ `UNAVAILABLE`, the image path never reaches `RESOLVED` |
| `tests/unit/test_identity_policy.py` | 15 | §8 no LLM in the identity layer, §9 the vitamin C family keeps every named field, §10 synonym ≠ identity proof, §11 the four facets on real stereo/isotope/protomer pairs |
| `tests/unit/test_decimer_worker_protocol.py` | 28 | §12–§16 the worker envelope, `request_id` echo, structured error, typed crash/timeout, image path rules, sha256 verification, §14's static ban on `eval`/`exec`/dynamic import/shell |
| `tests/unit/test_agent_identity_resolution.py` | 18 | §17–§19 `resolve_chemical_identity`, the `AMBIGUOUS` gate on `retrosynthesize`, the user-selection escape, the static ban on `candidates[0]` |

All six files named in §20 exist.

## Total tests

```
collected offline: 1178
```

## Passed

```
pytest -q                     1129 passed
```

## Failed

```
pytest -q                     0
```

**No CODE FAILURE.**

## Skipped

```
49 skipped
```

48 pre-existing + 1 new: `test_live_pubchem_returns_phenylacetylene`, gated on
`CREA_LIVE_TESTS=1` exactly as §22 requires. It is skipped, not counted as a
result.

One pre-existing architecture test was **sharpened**, not weakened:
`tests/architecture/test_layering.py::test_molfromsmiles_is_confined_to_the_chemical_core`
was a source-text substring match and fired on a docstring that *mentioned*
`Chem.MolFromSmiles`. It now inspects the syntax tree for the attribute access.
A guard that fires on prose is a guard people learn to work around.

## Identity model

`src/crea/core/models/identity.py`. `StructureInput` is what the user gave;
`ChemicalIdentityResolution` is what an inquiry established. The rule the
module encodes is `CANDIDATE` is not `RESOLVED`, and every other file in the
phase is downstream of it. Seven statuses: `INPUT / CANDIDATE / RESOLVED /
AMBIGUOUS / INVALID / NOT_FOUND / UNAVAILABLE`. No `confidence`, no
`probability`, no `score`.

## Deterministic structure resolution

`src/crea/services/identity.py`. SMILES and InChI go to RDKit and either parse
or become `INVALID`. Nothing "corrects" a user's SMILES. InChIKey is a lookup,
never reverse-derived. The four identity facets —
`same_connectivity / same_stereo / same_isotope / same_tautomer` — are measured
by comparing standard InChI layers, so a reader can point at the `/t` that
disagreed. Acetic acid and acetate agree on connectivity and differ only in
`/p`, which is `same_tautomer = False` and not `EXACT_STRUCTURE`.

## PubChem identity evidence

`src/crea/sources/pubchem.py` + `src/crea/providers/pubchem.py`, through the
Phase 2 `CompoundProvider`/`Transport` seam. No bespoke PubChem client.

The absence/failure boundary is enforced in exactly one place:
`ResourceNotFound` becomes `[]` (→ `NOT_FOUND`); every other `ProviderError`
propagates (→ `UNAVAILABLE`). Tested by looping over 500/503/429.

## Image recognition

`workers/chemistry/decimer_worker.py` + `src/crea/providers/decimer.py`. The
worker is a separate program in a separate runtime speaking the same
`crea-engine/1` envelope as the AiZynthFinder worker, so the guarantees
(`request_id`, structured error, typed timeout, typed crash) are written down
once.

DECIMER is **not** installed. `DecimerImageReader.status()` reports
`Availability.MISSING_KEY` when no isolated runtime exists, and
`resolve_by_image` reports `UNAVAILABLE` with `NO_RECOGNIZER`. The image path
never reaches `RESOLVED` in any case.

## Licences

`docs/PHASE36_DECIMER_LICENSES.md` records four separate facts: software
licence (MIT, © 2022 Otto Brinkhaus — code only), model licence (**not
asserted**), weights/training-data provenance (**not asserted**), runtime
requirements (TensorFlow 2.15.0, 8–16 GB RAM, own environment). The
`docs/LICENSES.md` EXCLUDED row for `DECIMER-Image-to-SMILES` is not upgraded.

## Agent tool

`resolve_chemical_identity` in `src/crea/agent/toolsets.py`, with the gate held
as session state in `src/crea/agent/workspace.py::IdentityState`. `AMBIGUOUS`
blocks `retrosynthesize`; the only unblock is `selected_candidate_index`
together with `user_selection_statement` quoting the user. The agent message is
generated from the status, so §17's sentence is the one that gets said.

## Git

```
git check-ignore -v .env
  .gitignore:3:.env	.env
```

`.env` (with live keys), `runtime/`, `runtime/aizynthfinder-env/`,
`runtime/decimer-env/`, `.venv/`, and `*.h5 / *.pt / *.ckpt / *.onnx /
*.safetensors` are ignored. Fixture JSON and `.meta.json` are tracked.

**The repository has no commits** (`master` has no history). Nothing was
committed. `git status --porcelain` shows only untracked source directories.

## Secrets protection

A scan of every `.py`/`.md`/`.toml`/`.json`/`.txt` found no key material. The
two hits for an `api_key`-shaped pattern are the env-var *name*
`CREA_LLM_API_KEY` and the test sentinel `"sk-do-not-leak-me"` in
`test_openalex_provider.py`, which exists to prove redaction works.
`crea.env.example` carries empty values only.

## Known limitations

- **PubChem's name index is exact.** Most common names resolve to one preferred
  CID, so the multi-candidate case cannot be provoked live on demand. The
  ambiguity *rule* is ours, not PubChem's, and is exercised against the real
  vitamin C family (three genuine CID entries differing in protonation and salt
  form) and the captured formula `C2H6O` fixture (76 distinct structures).
- **InChI layer comparison can under-claim.** When isotopes differ, standard
  InChI may rewrite the stereo layers, so such a pair reports
  `same_stereo = False`. This errs toward the weaker claim and never toward a
  false `EXACT_STRUCTURE`. Documented in `measure_facets`.
- **The recogniser success path is not exercised end to end.** It cannot be,
  offline, without installing a TensorFlow runtime the phase forbids installing.
  The worker's failure paths are tested against the real script; the adapter's
  success path is tested against a fake client.
- **The user-selection escape is prompt-enforceable only.** A model that writes
  `user_selection_statement` itself is indistinguishable from one relaying a
  real choice. What is enforced is that the words end up in the record, so the
  fabrication is auditable rather than invisible.
- **`resolve_chemical_identity` is not wired to a live PubChem provider in
  `agent/live.py`.** It degrades to `UNAVAILABLE` for names and still resolves
  SMILES/InChI. Wiring it is a small change and was left out to avoid touching
  the live stack mid-phase.

---

## 1. name → structure 是否由 LLM 完成？

**不是，完全不是。**

`name → structure` 的唯一路径是 `IdentityResolutionService.resolve_by_name`
→ `CompoundProvider.lookup` → 化学数据库返回记录。这条链上没有任何 LLM 参与，
并且由静态测试 `test_identity_models`/`test_identity_policy` 守住：identity 相关
文件不得 import `openai` / `anthropic` / `crea.llm` / `crea.agent`。

SMILES / InChI 同样不经过 LLM：它们走 RDKit 的确定性解析。用户写错了就是
`INVALID`，没有"模型帮我改对了"这条路径——那会是把臆造的结构伪装成用户的输入。

数据库查不到就返回 `NOT_FOUND`；数据库连不上就返回 `UNAVAILABLE`。两者都不
会产生结构。

## 2. ambiguous identity 如何处理？

**输出全部候选，绝不自动选择。**

`resolve_chemical_identity` 返回 `status="ambiguous"`、完整的 `candidates`
列表（`input_name, candidate_name, synonyms, canonical_smiles, inchi,
inchikey, source` 全部保留）、`selected=None`、`chemistry_allowed=False`，
以及一句生成自 status 的话：**"发现 3 个候选结构，需要进一步确认。"**

"我猜你说的是 XXX" 这句话被写进了 agent prompt 的禁止项，并且有一条测试断言
工具输出的消息里不出现任何候选名称。

同时，`retrosynthesize` 在 identity 未解决时直接拒绝运行（`status="blocked"`），
所以即使 agent 忘了问用户，化学计算也不会在一个候选上偷偷跑起来。

唯一的解开方式是**用户选择**：调用方必须同时传 `selected_candidate_index` 和
`user_selection_statement`（用户原话的引述）。只传 index 会被拒绝
（`SELECTION_WITHOUT_USER_STATEMENT`）；两者齐全时才标记为
`RESOLVED` + `USER_SELECTED`，并把用户的话写进 `notes` 留痕。

## 3. stereo / connectivity 如何区分？

**四个独立 facet，各自 `bool | None`，从不合并。**

`same_connectivity` / `same_stereo` / `same_isotope` / `same_tautomer`。

- `False` = 比较做过，答案是否
- `True` = 比较做过，答案是是
- `None` = 根本没比较过

三者不可混同。`None` 塌缩成 `False` 会把"没人查过"报告成"查过且不同"。

`identity_basis` 是从 facet **推导**出来的最强论断，不是并列的另一个字段：
facet 打架时取较弱的那个说法。相同骨架、不同立体化学 → `SAME_CONNECTIVITY`，
绝不是 `EXACT_STRUCTURE`。

比较本身用的是标准 InChI 的分层对比（`/c` 连接层，`/t /m /s /b` 立体层，
`/i` 同位素层，`/q /p` 电荷与质子层），所以审计者可以指着"是 `/t` 不一样"这样
的结论。乙酸和乙酸根只差 `/p-1`，因此 `same_connectivity=True` 但
`same_tautomer=False`——这个 case 是被专门写成测试的。

## 4. PubChem identity evidence 与 literature evidence 是否严格分离？

**是。**

PubChem 返回的记录被标记为 `IdentityBasis.PROVIDER_IDENTITY`——这是**数据库的
断言**，不是我们做过的比较。`EXACT_STRUCTURE` 只留给确定性解析。两者在
`test_identity_policy` 里被断言为不同的枚举值。

PubChem 有某个结构 ≠ 该结构有文献实验支持。这条规则在 §23 明确要求，并且在
代码里体现为：identity 层产出的是 `ChemicalIdentityCandidate`，完全不进入
Phase 3.5 的 evidence ladder；`Evidence` / `Claim` / `RouteEvidenceReport` 没有
任何改动，identity 也无法写入它们。

synonym 命中被单独记录为 `matched_by=SYNONYM` + `matched_synonym=...`，明确
不是身份证明（§10）。

## 5. DECIMER 输出为什么不能直接视为 resolved？

**因为 OCSR 是识别，不是验证。**

模型输出的是"我认为这串字符是图里的结构"。这中间有两层未知：

1. 这串字符**是不是**合法结构 —— 由 RDKit 回答，不是模型；
2. 它**是不是用户想表达的那个化合物** —— 这里没有任何人能回答。

所以 `RecognitionStage` 是独立的一组阶段：
`IMAGE_RECOGNIZED_CANDIDATE`（模型原始输出）→ 解析成功 → `STRUCTURE_CANDIDATE`
→ 解析失败 → `UNPARSED`。`resolve_by_image` 的出口**永远**是 `CANDIDATE` 或
`INVALID` 或 `UNAVAILABLE`，代码里有一条规则
`image_is_never_resolved` 写在 `rules_applied` 中，也有测试断言它。

worker 自己也不做清洗：`raw_smiles` 原样返回，不做 repair、不做 validation，
否则模型实际输出会被包装成一个看起来更像答案的东西。

## 6. 主 CREA RDKit 是否仍为 2026.3.6？

**是。** `pyproject.toml:14` 仍是 `"rdkit==2026.3.6"`，未被改动。

Phase 3.6 没有为任何新 provider 降级 RDKit。DECIMER 走的是完全独立的
runtime（`runtime/decimer-env/`），主环境里没有安装 TensorFlow、DECIMER 或
它们的任何依赖。`docs/PHASE36_DECIMER_LICENSES.md` 的 runtime 一节专门记录了
这一点：DECIMER.ai 平台自带的 RDKit 是 2024，与我们的 pin 冲突，这正是必须
隔离的原因。

## 7. 是否引入了新的必须联网依赖？

**没有。**

`pytest -q` 完全离线跑通 1129 个测试。新增的联网能力只有一个：
`PubChemProvider` 通过 Phase 2 已有的 `Transport` 抽象走 PUG REST——这是
**可选**的，不配置就返回 `UNAVAILABLE`，不阻塞任何确定性路径。

DECIMER worker 在识别时**不联网**，只在安装 runtime 时需要网络，而安装是
另一件、受 licence 门控的事，本阶段没有做，也不需要做。

唯一的 live 测试 `test_live_pubchem_returns_phenylacetylene` 被
`CREA_LIVE_TESTS=1` 门控，默认 skip。

## 8. 是否修改了 Phase 3.5 的 evidence ladder？

**没有。** `SupportLevel` 的六级、`StepStatus` 的推导规则、
`RouteEvidenceReport` 的结构、`docs/PHASE35_ROUTE_EVIDENCE.md` 全部原样。

identity 层产出的是 `ChemicalIdentityCandidate`，evidence 层产出的是
`Evidence`/`Claim`，两者是不同的模型、不同的服务、不同的工具，没有任何一条
路径把 identity 变成 evidence rung。

`test_identity_policy.py::test_no_identity_model_carries_a_confidence_or_a_probability`
专门断言 identity 模型没有长出 `confidence`/`score` 这类字段——Phase 3.5 的
evidence ladder 也没被塞进任何"确信度"列。§23 的
`CHEMICAL_IDENTITY_EVIDENCE` 与 `literature experimental evidence` 的区分
保持原样。

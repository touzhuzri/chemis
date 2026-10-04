# Stage B-1 Report — Semantic Spine

> **Stage B-1: COMPLETE**
> 基线：1445 passed, 50 skipped, 0 failed
> 最终：**1478 passed, 50 skipped, 0 failed**（+33，旧测试零回归）

一句话：CREA 4.0 的第一根 Semantic Spine 建成——任意化学输入（SMILES /
InChI / molblock / 化学名称 / 标识符）经统一 Resolver 到达**同一个
ChemicalEntity**，带表示集、身份证据等级与逐字段 provenance。四个验收
Case 全部实测通过（含真实 OPSIN 隔离 worker）。

---

## 1. 修改文件

| 文件 | 改动 |
|---|---|
| `core/models/identity.py` | +`IdentityEvidenceLevel`（EXACT/SUPPORTED/PROBABLE/AMBIGUOUS/UNRESOLVED，§21） |
| `core/models/entity.py` | +`ChemicalRepresentation`/`REPRESENTATION_KINDS`；ChemicalEntity +`representations` +`field_provenance`（ADDITIVE，调用方零破坏） |
| `chem/representations.py` | +`isomeric_smiles`/`molblock`/`molecule_from_molblock`（RDKit 仍只在 chem） |
| `engine/protocol.py` | +`EngineOp.NAME_TO_STRUCTURE` +`OpsinNameRequest`（同一 `crea-engine/1` 信封，§9） |
| `engine/client.py` | +`name_to_structure()` |
| `core/models/reaction_knowledge.py` | `key_material` 改用 `normalize_for_identity`（输出不变，key 契约有测试钉住） |
| `services/mention_resolution.py` / `services/reaction_builder.py` / `agent/toolsets.py` | 名称归一调用点收敛到 `core.naming`（行为不变） |
| `agent/toolsets.py` | `build_registry(semantic_resolver=)`；`_resolve_identity` 经 semantic resolver（§14），view 增 `identity_evidence`/`provider_results`；回退注册路径同样补全 representations |
| `agent/offline.py` / `agent/live.py` | 组合根装配 SemanticResolver（live 链：OPSIN → PubChem） |
| `docs/CREA_4_ARCHITECTURE.md` / `docs/ARCHITECTURE_AUDIT.md` | Stage B-1 落地设计与审计收口状态 |
| `docs/LICENSES.md`（前一轮） | OPSIN/RxnMapper/Open Babel/CIR 实证行 |

## 2. 新增文件

| 文件 | 内容 |
|---|---|
| `core/naming.py` | 名称归一化三具名用途（identity/search/display） |
| `semantic/__init__.py` | 层入口 |
| `semantic/representations.py` | 表示模型构建/解析，无效表示显式失败 |
| `semantic/identity.py` | `facets_for`（复用 measure_facets）/`same_entity_identity`/`identity_evidence_level`/`evidence_for_match` |
| `semantic/naming.py` | `NameLookupProvider` 协议 + `NameResolver`（调度/归一比较/歧义/证据/来源）+ `_materialize`（parse→normalize→比较） |
| `semantic/resolver.py` | `ChemicalInput`（显式分类）/`InputKind`/`ResolutionStatus`/`ProviderIdentityResult`/`ResolutionResult`/`SemanticResolver` |
| `providers/name_lookup.py` | `CompoundNameLookup`/`OpsinNameLookup`/`NCICIRNameLookup`（薄 adapter） |
| `providers/worker_opsin.py` | OPSIN worker 客户端（EngineClient，同一协议） |
| `workers/chemistry/opsin_worker.py` | 隔离 Java worker（实证 CLI：`uk.ac.cam.ch.wwmm.opsin.Cli -o smi`） |
| `sources/cir.py` | CIR 传输适配（opt-in） |
| `tests/semantic/test_semantic_spine.py` | Test 1-6 + 输入分类/无效表示 |
| `tests/identity/test_identity_evidence.py` | Test 7-10 + 证据语义 + 归一化锚定 |
| `runtime/opsin/*.jar` | OPSIN 2.9.0 运行时（Maven Central：cli+core+依赖；MIT） |
| `docs/STAGE_B1_REPORT.md` | 本文 |

## 3. 删除文件

无。（B-1 不删代码；废弃清单属 Stage E/F。）

## 4. 保留的旧代码

`chem/` 全部、IdentityService 及 `_lookup`/`_distinct_structures`/`_annotate`
歧义判定、`IdentityFacets`/`measure_facets`、ChemicalEntityRegistry、
`ChemicalIdentityResolution` 全模型、mention/evidence/reaction 全部语义、
ResearchWorkflow/ResearchJob 顶层结构、全部 API/前端。

## 5. 重构的旧代码

- 名称归一化：三处内联 `strip().lower()` → `core/naming` 具名函数（输出逐
  字节不变；`name:phenylacetylene` key 格式有回归测试）。
- `_resolve_identity`：identity 获取方式支持注入 semantic resolver；
  无注入时字节级保持旧行为（全部旧测试通过为证）。
- 审计误报澄清：PubChem "双份"实为 source/provider 分层——定 canonical
  入口 + 架构测试，而非合并。

## 6. 新 Semantic Layer 架构

见 `docs/CREA_4_ARCHITECTURE.md`「Stage B-1（已实施）」：分类→委托→收集→
注册；semantic 只 import core/chem/services；RDKit 只在 chem；无平行
identity 系统（比较全部走 IdentityFacets）。

## 7. Resolver provider chain

```text
NameResolver
├── OpsinNameLookup      → 隔离 Java worker（实证：ethynylbenzene → C(#C)c1ccccc1）
├── CompoundNameLookup   → CompoundProvider（canonical: providers.pubchem）
└── NCICIRNameLookup     → sources.cir（opt-in：ToS 未清）
```

调度→逐源收集（失败=诊断，链条继续）→`_materialize`（parse+normalize，
provider 拼写保留在 raw）→`_distinct_structures`（唯一去重规则）→
1 个=RESOLVED / 多个=AMBIGUOUS（绝不 candidates[0]）/ 0 个=UNRESOLVED。

## 8. Identity evidence semantics

| 等级 | 含义 | 实测 |
|---|---|---|
| EXACT | 结构记号输入 + 确定性解析 | `C#Cc1ccccc1` → CHEM-00000001 |
| SUPPORTED | 主名称/确定性标识经可靠来源 | `phenylacetylene`→链→同实体 |
| PROBABLE | 同义词路径或 facet 未定（`evidence_for_match` 只降不升） | 测试钉住 |
| AMBIGUOUS | 多相异结构并存 | vitamin c 双候选 → 不选 |
| UNRESOLVED | 无证据 | `zzznocnamezz` → 不猜 |

resolution status 与 evidence level 双维度并报（§17）。

## 9. PubChem consolidation

定案：`crea.providers.pubchem.PubChemProvider` = 唯一上层入口；
`crea.sources.pubchem.PubChemSource` = 其 PUG REST 传输层（被 provider 与
其单测引用）。架构测试 `test_pubchem_has_one_upper_entry` 禁止其余业务代码
import `crea.sources.pubchem`。未新增第三份。

## 10. Name normalization consolidation

`core/naming.py`：`normalize_for_identity`（reaction-key 材料，契约不变）/
`normalize_for_search`（匹配与查询）/`display_name`（展示，不判身份）。
§12 允许的"按不同问题命名"路线：不是粗暴合并，是三用途一入口。全部调用
点已换用；关键输出（`name:phenylacetylene`）有回归测试。

## 11. 测试结果

```text
pytest -q: 1478 passed, 50 skipped, 0 failed   （基线 1445 → +33）
```

- `tests/semantic/`（10）：Test 1-6（含真实 OPSIN worker 测试，runtime 缺失
  时 skip）、输入分类、无效表示显式失败、workflow→spine 接线验收
- `tests/identity/`（8）：Test 7-10、证据等级语义、归一化三用途锚定
- `tests/architecture/`（+3）：semantic 分层、PubChem 单入口、单一 worker 协议
- 其余为新模块自动分层/离线守卫；**旧 1445 个全部通过**（验收 #1）

验收对照（§20）：1✓ 2✓ 3✓ 4✓ 5✓ 6✓ 7✓ 8✓ 9✓ 10✓ 11✓（toolsets 注入
semantic resolver）12✓（`crea-engine/1` 未扩）13✓（API 零破坏）14✓
（未动 Workspace/Agent/Reaction Graph）。

## 12. 遗留问题

- **NCI CIR**：adapter 就绪但默认链未启用——服务条款待人工核读
  （LICENSES.md DEFERRED）。
- **OPSIN 运行时**：jars 置于 `runtime/opsin/`（Maven Central 组装，依赖
  共 12 个 jar）；缺 runtime 时链诚实降级（provider absent/UNAVAILABLE），
  但安装文档未成文（Stage C 补）。
- **facets 语义实证**：keto/enol 的标准 InChI `/h` 层在本实现下不同 →
  `same_tautomer=False`（宁欠勿过）；真正的 mobile-H 合并对测试可再补。
- **Image 输入**：分类与既有 `resolve_by_image` 已通，但 OCSR→entity 的
  表示补全未在 B-1 验证（Stage B-2）。
- **representation 去重**：同义 provider 重复表示按 (kind,value) 去重，
  未做跨 source 合并记录（来源多重性可在 B-2 加）。

## 13. Stage C 建议

1. **Chemistry Tool Layer**：`ChemistryTool` 统一接口；把
   resolve/parse/analyze/compare/search_pubchem/search_literature/
   retrosynthesize 包装为稳定工具面（现有 ToolRegistry 机制为底座，
   合一 ToolSpec/engine 双协议）。
2. **OPSIN/CIR 运行时文档化** + CIR ToS 清理后入默认链。
3. **name_structure**（反向命名）与 `search_reactions` 工具化。
4. 之后按既有阶段表推进 D（Reaction Graph）/E（Workspace）。

**到此停止。等待下一步指令。**

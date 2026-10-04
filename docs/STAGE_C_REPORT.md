# Stage C Report — Chemistry Tool Layer

> **Stage C: COMPLETE**
> 基线：1478 passed, 50 skipped, 0 failed
> 最终：**1499 passed, 50 skipped, 0 failed**（+21，旧测试零回归）

一句话：CREA 的全部既有能力（RDKit / SemanticResolver / OPSIN worker /
PubChem / Literature / Reaction 搜索 / AiZynthFinder）现在有一个**统一、
可发现、可验证、可审计的 Tool 边界**——一套 Contract、一种结构化
ToolResult、三种 execution mode、零新化学逻辑。

## 1. 新增文件

| 文件 | 内容 |
|---|---|
| `src/crea/tools/contract.py` | `ChemistryTool` 唯一契约 + `ExecutionMode` + `ToolErrorCode`/`ToolError` 归一词表 |
| `src/crea/tools/execution.py` | 三个 Execution Adapter（InProcess / IsolatedWorker / RemoteProvider） |
| `src/crea/tools/registry.py` | `ChemistryToolRegistry`（register/get/names/describe/invoke）+ `bridge_to` agent 桥 + `default_tool_registry` 装配 |
| `src/crea/tools/chemistry.py` | 八个代表性工具（薄 adapter，零化学重实现） |
| `tests/tools/test_tool_layer.py` | 14 个测试（契约/归一/模式/桥接/诚实缺席） |
| `docs/TOOL_LAYER.md` | 五层职责边界 + 契约 + 架构钉 |
| `docs/STAGE_C_REPORT.md` | 本文 |

## 2. 修改文件

| 文件 | 改动 |
|---|---|
| `core/models/research.py` | `ToolResult` ADDITIVE +`diagnostics` +`provenance`（成为唯一结果契约） |
| `tests/architecture/test_layering.py` | +4 钉（PubChem 入口含 tools、无旁路 identity、无第二 identity 词表、信封/SQL 隔离、RDKit 禁区含 tools/semantic） |
| `docs/CREA_4_ARCHITECTURE.md` / `docs/ARCHITECTURE_AUDIT.md` | Stage C 状态 |

## 3. 删除文件

无。旧 ToolSpec / agent ToolRegistry / worker 信封**全部保留**并划清职责
（§四：不简单删除重复概念，而是定义边界——Tool Contract → Execution
Adapter → Worker Envelope）。

## 4. 保留的旧代码

`agent/tools.py`（ToolSpec/ToolRegistry/execute 语义）、`crea-engine/1`
信封、全部 Service/Provider/Worker、SemanticResolver、Literature 政策、
ChemicalEntity/Identity。tools 只是上界，不触碰下层。

## 5. 重构的旧代码

无机械重构。仅 `ToolResult` 加法扩展两个字段（旧构造方零影响）。

## 6. Tool Contract

见 docs/TOOL_LAYER.md：name/description/version/input_schema/capabilities/
execution_modes/timeout_s/provenance_policy/invoke/execute/describe/
as_agent_spec。**Contract 唯一**；`ToolResult` 沿用 core 模型（扩展），
无第二结果模型。

## 7. Execution 模式与 worker 分离

`in_process` / `isolated_worker` / `remote_provider` 三模式齐备且声明可
发现。`IsolatedWorkerExecutor` 持有 `crea-engine/1`；测试断言 tool 结果
中**永不出现信封字样**（transport 不上浮）。REMOTE_PROVIDER 按
adapter/provider 契约测试（fixture transport），**未伪造远程执行**。

## 8. 八工具实测

| Tool | 实测 |
|---|---|
| resolve_chemical | SMILES→EXACT+CHEM-00000001；NAME→同实体；坏 SMILES→invalid、无实体 |
| name_structure | 真实 OPSIN worker：ethynylbenzene→resolved/supported；坏名→not_found；**CIR 明示 disabled（ToS 待核），未伪造** |
| analyze_structure | C8H6/102.136/rings/scaffold/fragments/fp 元数据；标注"计算结果，非文献证据" |
| compare_structures | 同分子→exact_structure/同一 identity；不同→basis 非 exact、similarity 恒 `screening_only: true` |
| search_chemical | PubChem（canonical 入口）候选带 source；无排序无选择 |
| search_literature | 未接线→unavailable 诚实；接 fixture 服务→DOI/OA status/fulltext permission 全保留 |
| search_reactions | 范围明示 local_reaction_knowledge + Stage D TODO；无过滤→REJECTED |
| retrosynthesize | 未接线→unavailable 诚实；接线后仅候选+引擎 provenance |

## 9. ToolResult / 错误归一

唯一契约：status(ok/error/unavailable/rejected) + data +
error_type(invalid_input/not_found/unavailable/ambiguous/timeout/
provider_failed/internal) + diagnostics + provenance。`invoke` 永不裸抛：
ExplodingTool 的 RuntimeError → `internal` 归一结果（测试钉住）。

## 10. 架构钉（§十七 六项全落）

① tools/agent 禁 sources.pubchem；② tools 禁绕过 SemanticResolver 自建
name→structure（含禁 import services.identity、禁构造 identity 对象）；
③ tools 禁第二 identity 词表；④ tools 禁 worker 信封/SQLite；⑤
tools/semantic 禁 rdkit；⑥ Tool→Service→Repository 无旁路。

## 11. 测试结果

```text
pytest -q: 1499 passed, 50 skipped, 0 failed   （基线 1478 → +21）
```

- `tests/tools/`（14）：registry 注册/发现/调用、契约唯一、错误归一、
  execution 模式、真 OPSIN 隔离 worker、resolve/name/compare 语义、
  literature 政策透传、reaction 范围诚实、agent 桥、describe 元数据
- `tests/architecture/`（+4）
- 其余为新模块自动守卫；**旧 1478 全部通过**

验收（§二十）：0 failed ✓；注册/发现/调用 ✓；Contract 唯一 ✓；ToolResult
结构化 ✓；错误/diagnostics 归一 ✓；execution mode 明确 ✓；worker transport
分离 ✓；resolve_chemical ✓；name_structure ✓；CIR unavailable 诚实 ✓。

## 12. 遗留问题

- `search_literature` 默认 registry 未接 LiteratureService（组合根按需
  接线；CLI/agent 路径已各自接线）。
- `search_reactions` 是本地知识搜索 adapter；跨文献反应检索与
  reaction_matching 收口留 **Stage D**（TODO 已如实标注在输出中）。
- timeout_s 目前是声明 + worker 请求级 timeout；进程内强杀超时未实现
  （协作式边界，留 Stage E 随 job 系统接入）。
- `name_structure` 的 CIR 后端就绪但 disabled（ToS）——启用条件与
  LICENSES.md 同步。

## 13. 下一步

按既有阶段表：**Stage D — Reaction Graph**（Reaction 一级化、
Entity↔Reaction↔Entity DAG、reaction_matching 收口、RxnMapper 接入）。
Stage C 到此停止，等待指令。

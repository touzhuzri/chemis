# CREA Chemistry Tool Layer

> CREA 4.0 Stage C。**Tool 是能力边界，不是业务层**：声明 schema、
> capability、execution mode、timeout、provenance 政策，然后调用既有
> Service / Provider / Worker / RDKit。实现化学逻辑的 tool 是 bug。

## 五层职责边界

```text
Agent
  ↓            只看到 Tool Contract（name/schema/metadata）
Tool           输入/输出 schema · 参数验证 · 错误归一 · provenance/diagnostics · timeout
  ↓
Service        业务语义（identity/mention/evidence/literature/workflow）
  ↓
Provider / Worker / RDKit    外部与计算能力（PubChem/OPSIN/DECIMER/AiZynthFinder/RDKit）
  ↓
Repository / external system 持久化与外部事实（SQLite / HTTP）
```

| 层 | 负责 | 不负责 |
|---|---|---|
| **Tool** | 声明面（schema/metadata/timeout）、参数验证、错误归一（ToolErrorCode）、结构化 ToolResult、诊断与来源透传 | 化学计算、SQL、HTTP、进程管理 |
| **Service** | 业务语义与政策（歧义判定、fail-closed 访问、证据等级） | 传输细节 |
| **Provider** | 一个外部源的问答与错误映射（absence≠failure） | 身份判定（§10：只是 External Identity Provider） |
| **Worker** | 隔离运行时（`crea-engine/1` 信封；Java/独立 venv） | 向上暴露信封 |
| **Repository** | 持久化与确定性查询 | 任何化学判断 |

## Tool Contract（唯一）

`crea/tools/contract.py::ChemistryTool`：

```text
ChemistryTool
├── name / description / version
├── input_schema        （pydantic arguments_model）
├── capabilities        （能力标签，可发现）
├── execution_modes     （in_process | isolated_worker | remote_provider）
├── timeout_s
├── provenance_policy
├── invoke(**args) -> ToolResult   （验证→执行→归一，永不裸抛）
└── execute(args)                  （子类：调既有能力）
```

**结果契约唯一**：沿用 `core.models.research.ToolResult`（ADDITIVE 扩展
`diagnostics`/`provenance`）——status/data/error_type/error/duration/
diagnostics/provenance。错误归一词表 `ToolErrorCode`：
`invalid_input→REJECTED`、`unavailable→UNAVAILABLE`、`not_found`/
`ambiguous` 是**答案**（status OK、data 说明）、`timeout`/
`provider_failed`/`internal`→ERROR。原始异常永不跨边界。

## Execution 分离（§四）

```text
Tool Contract → Execution Adapter（crea/tools/execution.py）→ Worker Envelope
```

- `InProcessExecutor`：确定性库调用
- `IsolatedWorkerExecutor`：`crea-engine/1` 子进程（OPSIN/retrosynthesis/
  DECIMER）——信封只属于 transport，Agent 永不理解它
- `RemoteProviderExecutor`：provider 协议（absence=答案、failure=错误）

`as_agent_spec()` 桥接进既有 agent `ToolRegistry`（一套契约、一座桥，
无第二协议）。

## 八个代表性工具（§七-§十）

| Tool | 调用 | mode | 备注 |
|---|---|---|---|
| `resolve_chemical` | SemanticResolver | in_process | 统一身份入口：SMILES/InChI/InChIKey/CID/NAME/molblock → entity + 证据等级 + 来源 |
| `name_structure` | NameResolver 链 | in_process + isolated_worker(OPSIN) + remote(PubChem) | 候选带来源；**CIR 默认 disabled（ToS 待核）且明示** |
| `analyze_structure` | crea.chem（RDKit） | in_process | formula/MW/rings/HBD/HBA/TPSA/logP/scaffold/fragments/fp 元数据；标注"计算结果，非文献证据" |
| `compare_structures` | IdentityFacets + screen_similarity + compare_scaffolds | in_process | **identity ≠ similarity** 四轴分离，similarity 恒为 screening_only |
| `search_chemical` | providers.pubchem（唯一入口） | remote_provider | name/identifier/smiles/inchikey；无排序无选择 |
| `search_literature` | LiteratureService | remote_provider | metadata≠evidence≠permission 全保留（fail-closed 不弱化） |
| `search_reactions` | 本地 reaction knowledge 搜索 | in_process | 范围明示 `local_reaction_knowledge`；跨文献搜索留 Stage D（TODO 如实标注） |
| `retrosynthesize` | RetrosynthesisProvider（AiZynthFinder worker） | isolated_worker | 候选路线 + 引擎 provenance；无排序、无可行性宣称 |

## Registry

`crea/tools/registry.py::ChemistryToolRegistry`：register / get / names /
describe（发现面）/ invoke（dict 参数，防撞名）；`bridge_to(agent_registry)`
注册为 agent ToolSpec。后端缺席时工具仍可发现、调用返回 `unavailable` +
diagnostics——**绝不伪造成功**。

## 架构钉（tests/architecture）

1. tool/agent 层禁 `sources.pubchem`（只经 `providers.pubchem`）
2. tools 禁绕过 SemanticResolver 自建 name→structure（禁构造 identity
   候选/决议、禁 import services.identity）
3. tools 禁第二套 identity 词表
4. tools 禁直接说 worker 信封（`crea.engine.protocol`）、禁 sqlite3
5. tools/semantic 禁 rdkit（chem 是唯一 RDKit 家）
6. Tool → Service → Repository，无旁路

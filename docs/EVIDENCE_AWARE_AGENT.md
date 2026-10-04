# Evidence-aware Agent（Stage 6.4）

## 目标

CREA 第一个真正的、有界的、只用只读工具的 Agent：

```text
User Question → Bounded Agent Loop → Deterministic Tools → Evidence → Grounded Answer
```

Agent 可以：理解问题、调用已有 CREA 工具、获取结构化事实、查询 Reaction /
Evidence / Literature、比较候选反应、探索候选路线、在工具结果上继续分析、发现
证据缺口、输出带来源的结构化答案。**Agent 不能创造化学事实。**

## 有界循环（§三）

```text
MAX_ITERATIONS = 6
MAX_TOOL_CALLS = 12
MAX_EXECUTION_SECONDS = 60
```

必须 explicit / configurable / tested。超过 → `StopReason.MAX_*` + `AgentStatus.LIMIT_REACHED`。
**无 `while True`。**

`StopReason`：`FINAL_ANSWER` / `MAX_ITERATIONS` / `MAX_TOOL_CALLS` / `TIMEOUT` /
`REPEATED_TOOL_CALL` / `PROVIDER_ERROR` / `INVALID_ACTION`。
`AgentStatus`：`COMPLETED` / `PARTIAL` / `LIMIT_REACHED` / `FAILED`。

## Tool 是唯一事实入口（§四/§五/§二十二）

Agent **不**直接访问数据库 / HTTP / Python / 文件；所有外部事实经
`ToolRegistry`。工具**只读**（`ToolPermission.READ_ONLY`），经 `ToolSpec.permission`
标注（additive 扩展既有 `agent/tools.py::ToolSpec`，**未**新建 registry）。

回归测试钉住：注册表中**无** `create/update/delete/save/write/insert/sql/shell/
python/http/fetch/download` 工具；每个工具 `permission == "read_only"` 且
`mutates_evidence == False`。

## 工具集（只读）

| Tool | 后端 |
|---|---|
| `inspect_molecule(smiles)` | `crea.chem`（normalize / 官能团 / 骨架，只读计算） |
| `search_transformations(substrate, target?)` | `TransformationSearchService`（5.1） |
| `get_reaction_template(id)` | `reaction_library.get_template` |
| `compare_reactions(ids, substrate?, target?)` | `ReactionComparisonService`（5.2） |
| `reaction_evidence(template_id)` | `ReactionEvidenceService`（3.4/4.4） |
| `literature_for_paper(paper_id)` | `ReactionEvidenceService.evidence_for_literature` |
| `search_literature(query, limit≤10)` | `LiteratureService`（**非** provider；无则 `unavailable`） |
| `route_search(start, target, max_steps≤3)` | `RouteExplorerService`（5.4） |

- **工具输入 Schema Validate**（§十七）：`ToolRegistry.execute` 用 `arguments_model`
  校验；失败 → `rejected`，绝不 `eval/exec`。
- **工具输出结构化**（§十八）：pydantic/typed dict，`{status, ...}`。
- **Literature ≠ Evidence**（§三十五/§三十六）：`search_literature` 明说「metadata is
  discovery, not evidence」；只有 `reaction_evidence` 显示来源支持什么。
- **无 Fulltext/PDF Tool**（§二十三/§五十一/§五十二）：`MAY_RETRIEVE` 只作状态。

## Answer Grounding（§二十九/§三十）

`AgentAnswer.claims[]` 每条 `AgentClaim` 引用 `reaction_ids / evidence_ids /
literature_ids / route_ids`。grounding 校验时**只保留本轮 run 实际收集到的 id**
（`gathered`，来自 ToolResult 递归抽取）；`claim_type == fact` 且无任何引用 →
`grounded=False`，run 状态降为 `PARTIAL`。引用不存在的 id 被剥离。

## Hypothesis（§三十一/§三十二/§三十四）

`ClaimType`：`FACT` / `INTERPRETATION` / `HYPOTHESIS`。非 FACT 的 claim
`not_experimentally_verified = True`。**不**创造 `strong/weak evidence`、
`high/low confidence`（除非未来有正式 evidence weighting）。

## Context Size Guard（§四十七/§四十八/§四十九/§五十）

`MAX_LITERATURE_RESULTS = 10`（`search_literature` 强制 `≤10`）；工具结果回填模型时
`MAX_TOOL_RESULT_CHARS = 4000`；`_bounded` 对长列表截断但**保留 id**（`*_truncated`
计数）。**不**无限翻页、**不**批量抓全文。

## Tool Trace（§十六/§四十/§四十一）

`tool_calls[]` 记录 `tool_call_id / tool_name / arguments / status / output_summary /
output_ids`——可审计、可复现、可解释。**不**保存 chain-of-thought / 原始 prompt。

## 失败处理（§六十一/§六十二）

模型失败 → `PROVIDER_ERROR` / `FAILED` + warning；工具失败 →
`status=failed`、`warning`，尽可能继续，最终 `PARTIAL`。**确定性工具不受影响**
（offline API 仍 `health=ok`）。

## 只读、瞬态、无新表（§八/§九/§十/§八十四）

不写数据库、不建 `agent_sessions` / `agent_runs` / `tool_calls` 表。`EvidenceAgentResult`
**transient**。用户说「加入 Reaction Library」→ 只能回答「Agent 没有写入能力」。

## API（§三十七）

```text
POST /api/ai/agent {question, molecule_smiles?, molecule_id?}
→ {"answer": {run_id, status, stop_reason, answer:{summary, claims, limitations, sources},
              tool_calls, iterations, gathered, warnings, model_metadata}}
```

无 provider（offline）→ 503 `AGENT_PROVIDER_UNAVAILABLE`。

## UI（§四十二/§四十三/§四十四）

Molecule Explorer 内「Evidence-aware Agent」：问题输入 → 答案区显示 **summary /
claims（badge 标 claim_type + grounded）/ limitations / sources（可点进 Reaction /
Literature Detail）/ Agent activity（tool trace）/ warnings**。数据不足时说明
knowledge boundary，而非「AI has no idea」。

## 禁止（§六十四）

不说「一定能成功 / 最佳路线 / 该论文证明了你的底物 / 预计产率 / 肯定发生」。
结果模型不含 `confidence` / `success_probability` / `yield` / `best_route` /
`recommended`（测试钉住）。

## 测试

`tests/ai/test_evidence_agent.py`（13）：单工具后 grounded 答案、幻觉 fact id 不
grounded→PARTIAL、hypothesis 标记、非法参数 rejected、max_tool_calls / max_iterations
/ repeated_tool_call 停止、provider 失败结构化、全部工具只读 + 无写工具、literature
discovery 语义、工具输出有界、无预测字段、API offline。

架构测试 +1：`evidence_agent.py` 只 import 工具 registry + core + llm（不 import
service/chem/store/provider/httpx）；`evidence_tools.py` 不 import provider/store；
无 `eval/exec/subprocess/os.system`。

## 不越界（§八十四/§八十五/§停止）

不写数据库、不建 AI/Agent 表、不做 PDF/OCR、不无界爬取、不执行任意代码。Stage 7
才开始 ResearchSession / ResearchWorkflow / ResearchReport。

**Stage 6.4 completed.** 唯一目标（User Question → Bounded Tool-Using Agent →
Evidence → Grounded Answer）达成——**AI 可以推理，但不能成为化学事实的来源。**

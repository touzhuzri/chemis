# Stage E Report — Chemistry Agent MVP

> **Stage E: COMPLETE**
> 基线：1511 passed, 50 skipped, 0 failed
> 最终：**1524 passed, 50 skipped, 0 failed**（+13，旧测试零回归）

一句话：CREA 从「拥有一组化学 Tool」推进到「**用户一句话 → Agent 经
ToolRegistry 自主调用真实化学能力 → 带着 evidence / provenance 重组回答**」。
到线即停——没有 Workspace、没有 Conversation、没有长期记忆、没有多 Agent。

```text
                User
                  │
                  ↓
         ┌────────────────┐
         │ ChemistryAgent │   orchestration, 零化学
         └───────┬────────┘
                 │ ToolRegistryLike (注入的抽象)
                 ↓
          ChemistryToolRegistry  ── describe() 单一事实来源
                 │
   ┌─────────────┼──────────────────┐
   ↓             ↓                  ↓
resolve_chemical  analyze_structure  compare_structures
   │             │                  │
search_reactions  search_literature  ...
   │
   ↓
existing chemistry layer (service / provider / repository / RDKit)
```

## 1. pytest

```text
1524 passed, 50 skipped, 0 failed
```

新增：`tests/agent/test_chemistry_agent.py`（10）+ `tests/architecture/`
（+2）。

## 2. Agent 架构（§4/§5/§20）

`src/crea/agent/chemistry_agent.py::ChemistryAgent`，职责只有一件事：

```text
User Message → LLM/Decision → Tool Selection → ToolRegistry → ToolResult
             → 继续调用 Tool 或生成最终回答
```

- 唯一入口 `agent.run(user_message) -> AgentResult`。
- **Agent 不持有任何化学**：RDKit、SemanticResolver、PubChem provider、
  Crossref/OpenAlex/Unpaywall、ReactionRepository、SQLite、chemistry
  service 全部不 import——能力经注入的 `ToolRegistryLike`（协议）进入。
  架构测试 `test_the_chemistry_agent_depends_on_the_tool_registry_abstraction_only`
  钉住这一点（禁 `rdkit/crea.chem/crea.semantic/crea.providers/
  crea.sources/crea.services/crea.repositories/crea.tools/…`）。
- 依赖抽象（§12）：`ToolRegistryLike` 协议只有 `describe()`/`invoke()` 两法，
  由 `ChemistryToolRegistry` 已实现；Agent 不 import 具体工具类。

## 3. Tool Calling 流程（§6/§7）

- Tool schema 来自 `registry.describe()`，**单一事实来源**，不手工维护第二份
  列表（`name + description + arguments(JSON Schema)`，另有
  capabilities/execution_modes/provenance_policy）。
- 中性结构化协议复用既有 `AgentAction`（`action=tool_call|final|clarify` +
  `tool` + `arguments`）——不是第二套协议。原生 tool calling 由
  `crea.llm.openai_compatible` 翻译（`action_from_payload`）；不支持原生
  calling 的模型走文本 JSON（脚本式 provider）。
- Tool 结果以中性线程消息 `tool_result(<name>) = <ToolResult JSON>` 回填，
  `to_openai_messages` 已识别该前缀并翻译为原生 `tool` 消息。

## 4. Agent Loop（§8）

有限循环 + 硬上限：

```python
MAX_ITERATIONS = 8   # 硬顶
while iteration < MAX_ITERATIONS:
    response = LLM(system, thread, tools=registry.describe())
    if response is 纯文本:  → 最终回答（answered）
    if action is FINAL:     → 最终回答（answered）
    if action is CLARIFY:   → 追问（answered，answer=question）
    if action is TOOL_CALL: → registry.invoke(...) → append → continue
# 到顶 → status="limit_reached"（不重试风暴，带走已产出）
```

本阶段无 planner、无复杂框架。单轮一个工具调用（与 OpenAI 提供方原生
tool_calls 的单次取首一致）。

## 5. Evidence / Provenance 传递（§11）

`AgentResult`（ADDITIVE 进 `core/models/research.py`）复用唯一结果词汇：

```text
AgentResult
├── answer          str
├── tool_calls      list[ToolResult]        ← 唯一 ToolResult 契约，原样保留
├── evidence        list[dict]              ← 工具实产（id/载荷，标注 tool）
├── provenance      list[dict]              ← 每调用 provenance，标注 tool
├── diagnostics     list[str]               ← 工具 diagnostics + 状态诊断
├── iterations      int
└── status          "answered"|"limit_reached"|"model_failed"
```

无 `Evidence2/Provenance2/ToolResult2`（架构测试 `test_the_chemistry_agent_
reuses_the_one_result_vocabulary` 钉住）。evidence 从 `ToolResult.evidence_ids`
与 `search_reactions` 的 `data.results[*].evidence` 提升，provenance 从
`ToolResult.provenance` 提升——都不是模型的口述。

## 6. 不确定性处理（§10）

Agent 是诚实中继，**绝不把 AMBIGUOUS/UNAVAILABLE/TIMEOUT 强行转成确定答案**：

- `resolve_chemical` → `data.status="ambiguous"`：候选列表随行，Agent 原样
  传递，**无任何 `candidates[0]` 代码路径**（测试钉住：仅 resolve 被调用，
  answer 明示歧义）。
- `search_reactions` → `UNAVAILABLE`：`diagnostics` 记
  `search_reactions: unavailable (...)`；**与「空结果」区分**——空结果是
  `OK + "no known reaction matches"`，不可用是 `UNAVAILABLE + error`，二者
  在 `AgentResult.diagnostics` 中永不混淆（`test_unavailable_is_distinguished_
  from_empty`）。
- 语义由 system prompt 固定：「无法查询」≠「没有」；「没有找到」≠「无法查询」。

## 7. 测试（§16，deterministic，无真实 LLM）

`tests/agent/test_chemistry_agent.py`，`ScriptedLLM`（脚本条目可为静态
`AgentAction` 或 `(LLMRequest)->AgentAction` 回调，使第 2 步能从第 1 步真实
结果推导）：

| # | 行为 | 覆盖 |
|---|---|---|
| 1 | resolve → final | resolve_chemical(SMILES) → 回答 |
| 2 | resolve → analyze | 真 RDKit analyze_structure（C8H6） |
| 3 | resolve → search_reactions | 真 ReactionRegistry，product role |
| 4 | resolve(A)+resolve(B) → compare | compare_structures 真实四轴 |
| 5 | AMBIGUOUS 不猜 | vitamin c 双候选 → status=ambiguous，仅 resolve 被调 |
| 6 | UNAVAILABLE ≠ empty | 空结果 vs 未接线，diagnostics 可区分 |
| 7 | 必经 ToolRegistry | SpyRegistry 记录 invoke/describe |
| 8 | 不依赖底层 | 架构测试（见 §2） |
| 9 | evidence/provenance 保留 | EV-1 与 provenance 抵达 AgentResult |
| 10 | MAX_ITERATIONS 阻止死循环 | 10 步脚本 + cap=3 → limit_reached |

## 8. End-to-End 示例（§17）

`test_e2e_smoke_known_reactions_of_phenylacetylene`：**真化学工具，只有 LLM 是
脚本**。

```text
用户："苯乙炔有哪些已知反应？"
  ↓ resolve_chemical(NAME "phenylacetylene")   ← 真 SemanticResolver（PubChem 假源）
  ↓ 返回 CHEM-00000001
  ↓ search_reactions(product_entity_id=CHEM-00000001)  ← 真 ReactionRegistry
  ↓ 返回 RXN-E2E（A → 苯乙炔；match_role=product；evidence EV-1 随行）
  ↓ final
AgentResult { answer:"苯乙炔作为产物出现在反应 RXN-E2E 中…",
              evidence:[{evidence_id:EV-1}], provenance:[search_reactions…] }
```

第 2 步的 `product_entity_id` 由脚本回调从第 1 步真实结果 `data.entity.entity_id`
推导——不是硬编码、不是假 Reaction 数据。

## 9. 已知限制

- **无 Workspace/Conversation/长期记忆**（§22 明令留后）。
- **无 planner**：单轮单工具、`MAX_ITERATIONS=8` 硬顶；复杂多步规划（如
  先检索知识再决定研究）不属本阶段。
- **无 `/api/agent/chat`**（§18）：现有 composition root 围绕 research job +
  knowledge 构建，接入交互式 Agent 需引入 LLM provider/会话语义，属
  「非自然」的 API 改造；Agent service 本身已完成，endpoint 留后续。
- LLM 无原生 tool calling 时靠文本 JSON 协议（脚本式路径），已可用但不
  是多 tool 并行。
- evidence 提升只识别 `ToolResult.evidence_ids` 与
  `data.results[*].evidence` 两种形状（已覆盖 Stage C/D 工具）；未来工具
  新增证据出口时在此扩展。

## 10. 下一阶段边界（不执行）

- Stage F：Workspace + Conversation（ChemistryAgent 作为其核心 loop）。
- 长期记忆、多 Agent、高级 Planning、自动科研、知识图谱、递归搜索——
  全部留后。

**Stage E 到此停止，等待下一步指令。不自行进入 Stage F。**

胜利线（§22）：用户的一句话，被 CREA Agent 理解，经 ToolRegistry 调用真实
化学能力，并把带 evidence/provenance 的结果重组为回答——已达成。

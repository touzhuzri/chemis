# Route Reasoning（Stage 6.3）

## 目标

对 5.4 找到的**确定性**候选路线做一次有界的 AI 解释：

```text
Start Molecule + Target Molecule
    → RouteExplorerService（5.4，确定性路线）
    → AIContextService（6.1，事实）
    → LLM（6.2 抽象）
    → Grounding validation
    → RouteReasoningResult（transient）
```

AI 可以：理解路线结构、逐步解释、比较候选路线、识别信息缺口、结合
evidence/literature 给有依据的分析、总结已知差异。AI **不能**：凭空创建 reaction /
intermediate / route / DOI / 文献 / 条件 / 产率。

## 调用顺序（§五）

```text
Request → RouteExplorerService → RouteExplorerResult → AIContextService → LLM
        → Grounding → RouteReasoningResult
```

**不**允许 `Request → LLM → RouteExplorer`。AI 只处理真实存在的路线。

## 单次有界调用（§五十二/§五十三）

6.3 是 **single bounded reasoning call**：不循环、不调用工具、不自动搜文献、不自
扩展 context、不 autonomous planning。`User → RouteExplorer → Context → LLM →
Reasoning`。Agent loop 属 6.4。

## 输入（§四）

```text
RouteReasoningRequest { start_molecule, target_molecule, max_steps, query, max_routes }
```

## 输出（§十/§十七/§四十七）

```text
RouteReasoningResult
├── reasoning_id / context_version / start_smiles / target_smiles
├── route_summaries   （route_id / status / step_count / blocked_reason —— 事实，来自 explorer）
├── observations      [RouteObservation]
├── comparisons       [str]     （AI 解释）
├── gaps              [str]
├── unsupported       [str]     （无法 ground 的 observation，进 warnings 不展示）
├── limitations / warnings
└── model_metadata    （provider/model/temperature/request_id —— AI 执行元信息）
```

`RouteObservation`：`route_id / status / summary / step_notes[{step_index,
reaction_template_id, note}] / gaps / grounding_status`。

**不含** `best_route` / `recommended_route` / `yield` / `confidence` /
`success_probability` / `answer`（测试钉住）。

## Grounding（§三十一/§三十二）

- observation 的 `route_id` 必须是 explorer 实际返回的路线 → 否则进 `unsupported`
  + warning。
- step note 的 (`step_index`, `reaction_template_id`) 必须是该路线的**真实一步** →
  否则被剥离，observation 降为 `PARTIALLY_GROUNDED`。
- route 的 `status` **只**取自 explorer（`complete`/`partial`/`blocked`），模型不能
  升级（partial ≠ complete，blocked ≠ route）。

## 不发明中间体（§三十二）

Prompt 与 grounding 都保证：AI 只能描述确定性的 step；不得输出
`A → R1 → X → R2 → B` 若 X 不在路线中。

## Evidence / Literature（§十八/§十九/§二十）

保持 evidence 的 `evidence_id / evidence_type / support_scope / status / locator /
literature_id`（尤其 `support_scope`）；不复述成更强结论；不扩展 Context 之外的
Paper。

## 失败处理（§三十九/§四十）

复用 AI 错误词汇：`AI_OUTPUT_INVALID`（≤1 次修复重试）/ `AI_PROVIDER_UNAVAILABLE`
/ `AI_TIMEOUT` / `AI_RATE_LIMITED`（`RouteReasoningError`）。**确定性 Route Explorer 不受
影响**（offline API 仍 `health=ok`）。

## 只读、瞬态（§四十七/§四十八）

AI **不**写回 `RouteExplorerResult`，**不**改 RouteStatus/steps/id。`RouteReasoning`
**transient**，不新增 `route_reasoning` / `ai_route_analysis` 表。

## 生产现状（§四十六）

46 条模板 `reaction_smarts` 全空 → 多数情况下 `routes=[]`、`partial_routes`/`blocked_paths`
非空。AI 必须正确处理「No deterministic route found.」，**不**因为没有路线就自己
生成路线。

## API（§三十七）

```text
POST /api/ai/route-reasoning {start_molecule, target_molecule, max_steps, query}
→ {"reasoning": {...}}
```

返回结构化 reasoning（非裸 answer）。offline / 无 key → 结构化错误。

## UI（§四十一）

Route Explorer 区内新增「Route Reasoning」：输入推理问题 → 观察卡片（route status
来自 explorer + AI summary + gaps + step notes 可点进 Reaction Detail）。明确区分
事实与 AI 解释。

## 测试

`tests/ai/test_route_reasoning.py`（10）：完整路线解释、多路线、partial 不升级为
complete、no routes 不造路线、route_id 幻觉被拒、step 幻觉被剥离、malformed→
`AI_OUTPUT_INVALID`、provider 不可用、model metadata + 无 ranking 字段、API offline。

架构测试：`services/route_reasoning*.py` 不 import vendor SDK / transport / provider /
agent / sqlite（并入 6.2 的架构钉）。

## 不越界（§五十一/§五十三/§停止）

不做 Evidence-aware Agent（6.4）、agent loop、tool calling、autonomous research、
自动文献检索、self-directed context expansion、autonomous planning（即便用户说「找
最好路线」也只能分析现有路线）。

**Stage 6.3 completed.** 唯一目标（Deterministic Routes → Evidence-aware AI Route
Reasoning）达成——**路线本身仍来自 CREA 的确定性化学层。**

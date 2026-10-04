# Modification Analysis（Stage 6.2）

## 目标

CREA 第一个真正的 AI 辅助能力：

```text
User Modification Intent → AI Context → AI Interpretation
    → Grounded Chemistry Candidates
```

不是 AI retrosynthesis、不是 route planning、不是 reaction prediction、不是
automatic product generation。它回答的是「这个分子想实现某种结构修改，可以考虑
哪些**已知**化学变化？」

## 职责划分（§一）

- **确定性化学层**：molecule identity / ReactionTemplate / SMARTS matching /
  transformation / ReactionEvidence / literature / graph / route。
- **AI**：理解修改意图、解释结构变化、提出候选方向、把候选映射到已有
  ReactionTemplate、组织已有事实。
- **AI 不做**：创造新 ReactionTemplate / reagent / condition / product /
  literature / yield。

## 唯一事实来源：AI Context（§四）

```text
ModificationRequest → AIContextService → AI Context → LLM
```

`ModificationAnalysisService` **不**直接查 SQLite、不直接查 ReactionTemplate、
不自己拼 facts——一切经 6.1 的 AI Context。

## LLM 抽象（§五/§六/§七/§四十三/§四十四）

**复用**项目既有 `crea.llm` 抽象（`LLMProvider.complete(LLMRequest)->LLMResponse`），
**不**新建第二套，也**不**绑定厂商。业务层不 import `openai`/`anthropic`/`ollama`/
`httpx`（架构测试钉住）。一个最小 fake provider
（`llm.fake.TextScriptedLLMProvider`）与既有 `OpenAICompatibleLLMProvider` 足够。

## Prompt（§八/§九/§三十六）

`services/modification_prompt.py` 与业务逻辑分离，明确边界：只能基于提供的
Context；不创造 Context 中不存在的事实；不编造 DOI/文献/试剂/条件/产率；
SMARTS match ≠ 实验成功；curated ≠ literature proof；literature linked ≠ 当前底物
被验证；fulltext permission ≠ evidence validity。**要求 JSON 输出**，不要求
chain-of-thought。

## 输出结构（§十/§十一/§三十四）

```text
ModificationAnalysis
├── analysis_id / context_version / input_molecule / input_molecule_id / user_query
├── candidates  [ModificationCandidate]
├── unsupported [str]        （无法 ground 的候选，只进警告）
├── limitations / warnings
└── model_metadata           {provider, model, temperature, request_id}（AI 执行元信息，非化学事实）
```

`ModificationCandidate`：`candidate_id / description / structural_change{from_feature,
to_feature, scope} / reaction_template_ids / match_status / evidence_ids /
literature_ids / evidence_summary / rationale / grounding_status`。

**不含** `yield` / `predicted_yield` / `confidence` / `success_probability` /
`new_reaction_template` / `answer`（测试钉住）。

## Grounding（§十二/§十三/§十四/§十五）

`ModificationGroundingService` 逐条校验 AI 的 claim 是否能在 Context 中解析：

- `reaction_template_id` 必须在 Context.reactions 中；
- `evidence_id` 必须在 Context.evidence 中；
- `literature_id` 必须在 Context.literature 中。

**不信任 LLM 的 ID**——一律重新对照 Context。结果：

- 全部解析 → `GROUNDED`
- 部分解析 → `PARTIALLY_GROUNDED`（只保留能解析的 id，其余进 warnings）
- 无任何 reaction 可解析 → `UNSUPPORTED`，**不作为化学候选展示**，进 `unsupported`
  + warnings。

**默认不向用户展示 ungrounded 候选。**

## 失败处理（§十九/§二十）

- malformed output：最多 1 次修复重试（`MAX_REPAIR_ATTEMPTS=2` 次模型调用），
  仍不合法 → `AI_OUTPUT_INVALID`。**无无限重试。**
- LLM 不可用 → `AI_PROVIDER_UNAVAILABLE` / `AI_TIMEOUT` / `AI_RATE_LIMITED` 结构化
  错误。**Molecule Explorer / Reaction Explorer / Literature / Evidence / Graph /
  Route Explorer 全部不受影响**（测试钉住 health 仍 ok）。

## 只读、瞬态（§二十一/§二十二/§四十七）

AI 分析默认 **read-only**：不创建/修改任何 ReactionTemplate / Paper / Evidence /
Molecule。`ModificationAnalysis` **transient**，**不**建 `ai_analyses` /
`modification_analysis` / `chat_history` 表。

## 不越界（§三十二/§三十三/§四十八/§四十九）

不预测产率、不创造条件（Context 无则明说）、不升级 Evidence 状态、不做 Route
Reasoning（`A→R1→B→R2→C`，属 6.3）、不做 Evidence-aware Agent（agent loop / tool
calling，属 6.4）。

## API（§三十七）

```text
POST /api/ai/modification-analysis
     {"molecule_smiles": "CCO", "query": "把羟基转化成氯", "target_smiles": ""}
→ {"analysis": {context_version, candidates, unsupported, limitations, warnings, ...}}
```

返回结构化 analysis，**不**返回裸 `answer`。无 provider（offline）→ 结构化错误。

## UI（§三十八/§五十一）

Molecule Explorer 内「Modification Analysis」区：输入修改意图（可选 target）→ 候选
卡片，明确分区 **CREA facts**（ReactionTemplate + match status + evidence）与
**AI interpretation**（rationale），并列出未 ground 的候选与 warnings。用户不会
把 AI prose 当成数据库事实。

## 测试

`tests/ai/test_modification_analysis.py`（12）：基本分析、模型收到 AI Context、
grounded reaction、hallucinated reaction id 被拒、partially grounded 只留可解析 id、
hallucinated literature 被拒、候选只引用真实模板、malformed→`AI_OUTPUT_INVALID`、
fenced JSON 容错、provider 不可用→`AI_PROVIDER_UNAVAILABLE`、model metadata + 无
prediction 字段、API offline 结构化错误且确定性工具不受影响。

架构测试 +1：`services/modification_{analysis,grounding,prompt}.py` 不 import vendor
SDK / transport / provider / agent / sqlite。

## 未做（§五十/§停止条件）

不修改基础化学层（除非最小 additive 兼容）、不做 Route Reasoning（6.3）、不做
Evidence-aware Agent（6.4）、不持久化 AI 结果（留 Stage 7）。

**Stage 6.2 completed.** 唯一目标（User Modification Intent → AI Interpretation →
Grounded Chemistry Candidates）达成——**所有化学事实仍来自 CREA 自己的结构化知识层。**

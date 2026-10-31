# AI Research Intelligence (M2.0)

The loop where an LLM decides what to research next. One decision
centre, one reply protocol, one budget, one audit trail.

```text
Research Request
      ↓
Research Understanding      (M1.6 InputUnderstandingService)
      ↓
Research State Assembly     (StudyContext + this run's observations)
      ↓
LLM  →  ResearchDecision    (the closed six-move protocol)
      ↓
DecisionValidator           (registered tool? real schema? ids we hold?)
      ↓
ToolRegistry.execute        (policy, approval, facts, output limit)
      ↓
Observation                 (M1.2 — the machine's reading, unchanged)
      ↓
Scientific Interpretation   (the model's reading, kept beside it)
      ↓
StudyContext update         (facts from tools only; hypotheses stay hypotheses)
      ↓
Reflection / Replanning  →  next decision
      ↓
Evidence-grounded Report
```

## 1. What changed, and what did not

**Changed.** Before M2.0 three production loops each invented their own
reply shape and stop contract: `ResearchAgent` (`AgentAction` under
`prompt.SYSTEM`), `ChemistryAgent` (`AgentAction` under a different
prompt, prose treated as a final answer) and `EvidenceAgent` (bare JSON,
a third protocol). None of them consulted `StudyContext`, and none of
them could change direction because a tool returned something.

**Unchanged.** The tool registry, the M1.1 execution policy, the M1.2
observer, the M1.5 study context, the M1.6 intent, the M1.7 golden cases,
the LLM provider boundary. The intelligence *uses* all of them; it adds
no second vocabulary for a result, a status or a fact.

**Where the old loops stand.** `ChemistryAgent` remains the workspace
chat surface and `EvidenceAgent` the read-only evidence Q&A surface --
neither is a research planner. `ResearchAgent` is superseded as the
research decision centre and kept only for the offline demo stack and
the tests that pin it. The research decision centre is
`ResearchIntelligence`, and there is one of it.

## 2. The decision protocol

`crea.core.models.research_decision`. Six moves, one payload each,
`extra="forbid"` on every object:

| kind | payload | when |
|---|---|---|
| `continue_with_tool` | `tool_call` | run one registered tool |
| `update_hypothesis` | `hypothesis_update` | propose or move a hypothesis |
| `update_direction` | `direction_update` | abandon a line of inquiry |
| `ask_user` | `ask_user` | only a person can answer |
| `complete` | `completion` | the research may stop |
| `blocked` | `blocked` | no further move is possible |

A decision carrying two payloads is refused at construction: a model
cannot act and then claim it had already finished. A reply that does not
parse into this model is `UNPARSEABLE` and is never executed; it gets at
most `max_fix_attempts` repair requests, each repeating the model's own
refusal back to it.

Every turn produces a `DecisionRecord`: session, study, turn, model
provider and name, prompt version, schema version, decision type,
summary, selected tool, the validator's ruling and why, timestamp,
duration, token usage, warnings, the step the decision produced, and —
since M2.0.1 — the prompt-budget accounting (`context_budget`) the turn
was requested under. There is no field for the model's private
reasoning, and no hidden reasoning is stored.

## 3. What the model may and may not do

May: name a registered, canonical tool with arguments that pass that
tool's own schema; propose a hypothesis; change direction; ask a person;
stop; say it is blocked.

May not, and each is a gate rather than a request:

- **Call something unregistered.** The validator lists the available
  tools back to the model; the registry would refuse anyway.
- **Pass invalid arguments.** Checked against the tool's declared
  `arguments_model` before the call, and again by the registry.
- **Skip the execution policy.** Every call goes through
  `ToolRegistry.execute` — the same facts, network, model and approval
  guards the deterministic runtime uses. A model cannot grant its own
  approval; a persistent tool comes back `APPROVAL_REQUIRED`.
- **Cite an evidence id it was never shown.** The validator checks every
  cited id against the ids this run actually holds.
- **Turn a hypothesis into a fact.** A model's statement enters
  `StudyContext` as a hypothesis; `SUPPORTED` needs named supporting
  evidence and the study context is the only writer that can grant it.
  A fact is written only from what a tool declared (`sets_facts` /
  `fact_rule`), with `TOOL_VERIFIED`.
- **Repeat a fruitless call.** An identical tool+arguments signature that
  already ran is refused, with the reason handed back to the model. A
  *changed* argument set is a different call and is allowed.
- **Execute code.** There is no code field anywhere in the protocol, and
  an architecture test scans the loop for `eval`, `exec`, `compile`,
  `__import__`, `os.system`, `subprocess` and `importlib`.

## 4. The budget, enforced

`ResearchBudget` bounds decisions, tool calls, model calls, identical
calls, consecutive no-progress turns, parse-repair attempts, wall-clock
seconds, per-call timeouts and output tokens. Every one is charged on the
execution path and checked before the next turn — a budget that is only
configuration is not a budget.

When one runs out, the run stops with a `BlockedStatement` whose
`blocker` is `budget_exhausted`, keeps everything already collected, and
reports `partial` (or `failed` when nothing was produced). It never
reports the research as *complete*.

### The prompt-state budget (M2.0.1)

The state document the model reads is itself budgeted:
`STATE_BUDGET_CHARS` (24 000) is an **approximate character** bound, not
a token count, and the rendered document says so
(`context_budget.approximate`). The first real-model acceptance run
proved why this matters: with the production 44-tool catalog the full
schemas alone overflowed the budget, the old truncation loop drained
`observations` to zero, and the model never saw a single tool result.
The rule now is enforced by `render_state_document`:

> A tool result the model must reason about outranks the catalog of
> tools it might call next, which outranks old decision history.

Concretely, when the whole state does not fit: the newest observations
are kept first (their data may shrink through a preview ladder, always
flagged `data_truncated_for_prompt`); the tool catalog compresses along
a ladder — `full` (complete schemas) → `focused` (full schemas for the
goal-relevant tools, compact entries for the rest) → `compact` (name,
purpose, parameter names/types, required facts, approval flag) →
`names` — and **never drops a tool's name**, so the validator can still
teach any missing schema detail field by field; older decision history
and signatures fill what is left. Every trim is declared in
`context_budget` (per-section character usage, catalog mode, omitted
counts, reasons) and in `state_truncated`; a trimmed state never
pretends to be whole, and the original `Observation` objects are never
modified by rendering.

## 5. Model configuration

No vendor is named anywhere above the provider boundary.

```python
from crea.llm import LLMConfig, OpenAICompatibleLLMProvider

llm = OpenAICompatibleLLMProvider(LLMConfig.from_env())
# CREA_LLM_API_KEY  (required; no default, no fallback)
# CREA_LLM_BASE_URL (default: the OpenAI-compatible endpoint)
# CREA_LLM_MODEL    (default: a name, not a recommendation)
```

`LLMConfig.from_env()` raises `ProviderUnavailable` when the key is
missing. `ResearchIntelligence` given no provider does **not** fall back
to a deterministic pipeline: it stops with `MODEL_UNAVAILABLE`, an empty
trajectory, and a record saying so. A run that quietly became a fixed
pipeline and then reported an "AI research" result is the one outcome
this project exists to prevent.

For tests, `TextScriptedLLMProvider` replays raw text turns and
`FakeLLMProvider` replays `AgentAction`s. The default suite needs no key
and no network.

## 6. The ToolRegistry

The intelligence receives an already-built registry. It never builds
one, and it never reaches around one.

- Offline (`build_offline_intelligence_stack`): fixture literature over a
  `FixtureTransport` that cannot open a socket, fake retrosynthesis, and
  `build_agent_tools` with `allow_model_calls=False` and persistent
  writes requiring approval.
- Live (`build_live_intelligence_stack`): the real OpenAlex/Crossref
  providers, PubChem for names, AiZynthFinder for routes, and
  `allow_network=True` — persistent writes still need approval.

What the model is shown about each tool is what the tool declares: name,
description, argument schema, required session facts, whether a human
must approve, and its effects. Aliases are hidden — a model names the
canonical tool. Under budget pressure the catalog compresses (see §4);
the full parameter schema of any tool is one rejected call away, because
the validator's issues name missing or invalid fields precisely.

## 7. Study context and the trace

The loop reads the study's facts, hypotheses, evidence references,
decisions, open issues and session summaries at the start of every run,
and re-seeds the registry's session facts from the tool-verified ones —
so a prerequisite established in an earlier session is not re-planned.

It writes, and only what the sources already established:

- a `TOOL_VERIFIED` fact per declared fact of a successful tool call;
- an open issue for `NEEDS_INPUT` / `UNAVAILABLE` / `ERROR` observations;
- a `StudyDecision` per accepted decision;
- a hypothesis, always created `proposed`;
- a `StudySessionSummary` when the run ends (not when it pauses).

A user's answer on resume becomes a `USER_STATED` fact — the user's own
material, never `TOOL_VERIFIED`.

The run also produces a `ResearchTrace`-shaped account through
`build_plan_from_decisions`: the plan this run actually followed,
reconstructed from the tools it chose and why. `AiResearchRuntime` maps
a run onto the M1.4 `RuntimeResult` vocabulary so the M1.7
`run_blind_case` can drive this loop while keeping its own two-door
isolation (`get_agent_view` in, `save_trace` out).

### The persisted audit trail (M2.0.1)

Study context remembers what the study *knows*; the audit trail
remembers what one run *did*. While the run executes, every turn is
written to `ai_research_turns` (the `DecisionRecord`, the step, the
arguments, the raw `ToolResult`, the observation JSON and the
interpretation in separate columns) and the run's terminal summary to
`ai_research_runs` — both keyed by study first, via
`ResearchTraceService`. A write failure is a diagnostic on the run, never
a silent success and never a research failure.

`get`, `decisions`, `report` and `live_result` therefore answer in a
second process that merely opens the same database: the trail is
re-read, the `DecisionRecord` order restored, the report re-rendered.
What a restart still loses is the ability to *continue a pause* —
`answer` and `cancel` need the live intelligence instance and say so.
Reading an audit trail is not resuming a task, and nothing here claims
otherwise. The rows are scrubbed on the way in (§87/§88): no credential
reaches them, and no GoldenCase reference material can.

## 8. Product entry

**CLI**

```bash
crea research "帮我研究这个天然产物的可能合成路线" \
    --ai --provider openai-compatible --db data/crea.db
```

`--ai` needs a real model and says so plainly without one. It prints the
rendered report, and a paused run prints its session id and the answer
route. `--study <id>` continues an existing study.

**HTTP**

```text
POST   /api/research/intelligence              {goal, study_id?, approvals?, document_ids?}
GET    /api/research/intelligence/{session}
GET    /api/research/intelligence/{session}/decisions
GET    /api/research/intelligence/{session}/report
POST   /api/research/intelligence/{session}/answer     {answers, approvals?}
POST   /api/research/intelligence/{session}/cancel
```

The run is synchronous: an AI research loop is a bounded number of model
and tool calls, and the caller sees the whole result. A caller that
wants a background job puts this behind its own worker, exactly as
`ResearchWorkflow` sits behind `JobManager`.

**Python**

```python
from crea.app.research_intelligence import ResearchIntelligenceApplication

app = ResearchIntelligenceApplication(
    db_path=Path("data/crea.db"),
    llm_factory=lambda: OpenAICompatibleLLMProvider(LLMConfig.from_env()),
    stack_factory=lambda document_ids=None: build_live_intelligence_stack(),
)
result = app.start("比较几种可能的合成策略，优先考虑绿色化学")
if result["awaiting"]:
    result = app.answer(result["session_id"], answers={"selection": "路线 B"})
print(app.report(result["session_id"]))
```

The application layer receives the LLM and the registry as injected
factories. An architecture test pins that it names neither.

## 9. Safety boundary — what this is and is not

**Is (tested):**

- One decision loop. The model proposes; the registry executes; nothing
  else does.
- Facts come from tools, with the tool's own declaration attached. A
  model's statement is a hypothesis, always.
- Evidence ids are references the validator checks; the model cannot
  mint one.
- Every turn and every tool call is recorded with its validator ruling,
  its timing and its cost, and the run renders a report that keeps
  findings, hypotheses, open questions and limitations apart.
- Pauses are pauses: an unanswered question is never recorded as a user
  answer.

**Is not (honest limits):**

- Not an OS sandbox. Python code holding the registry can call it
  directly; the guarantee is that the decision loop does not, and that
  the only execution path inside it is `ToolRegistry.execute`.
- Not an authentication layer. `study_id` and an approval are declared,
  not authenticated. An authenticated deployment is future work.
- Not a claim about scientific correctness. A well-formed decision that
  rests on a wrong hypothesis is still wrong; the report says what the
  evidence supports and what it does not, and a reader does the rest.
- `cancel()` is cooperative. A synchronous tool already running finishes
  and is observed honestly. The loop never claims it interrupted a call.

## 10. Not implemented (later stages)

- Streaming progress over HTTP; the run is synchronous today.
- Resuming a pause across a process restart. Since M2.0.1 the **audit
  trail** (every `DecisionRecord`, tool result, observation,
  interpretation and the run summary) survives a restart and is readable
  and reportable from any process holding the database; what does not
  survive is the ability to *answer* that pause, which lives in the
  process that started it.
- Multi-user identity, roles, or an authenticated `actor`.
- Token-accurate prompt budgeting — the state document is bounded by
  characters, and since M2.0.1 that bound is priority-allocated and
  fully accounted (`context_budget`), but it is still characters, not
  tokenizer counts.
- Any change to the chemistry, the literature providers or the document
  pipeline.

See `docs/M2_CAPABILITY_BASELINE.md` for what the Golden Cases actually
demonstrated, and what they did not.

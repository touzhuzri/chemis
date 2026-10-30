# M2.0 AI capability baseline

What the M2.0 Golden Cases actually demonstrated, and what they did not.

## Read this first

Every case below ran against the project's **scripted** provider
(`TextScriptedLLMProvider`), which replays a fixed list of replies. That
proves the *loop* — that a decision the model returns changes what
happens next, that the gates hold, that the budget is real, that an
interpretation never overwrites a tool result.

It does **not** prove that any real model will produce those decisions.
The scientific quality of the choices, the chemical soundness of the
hypotheses, and whether a live model picks the right tool on a question
nobody scripted are **unmeasured**. They are the thing M2.1 should
measure, against real credentials, case by case.

The baseline therefore has two columns everywhere: *what the loop does*
(verified) and *what a real model does* (not measured).

## The cases

| # | Case | Loop behaviour (verified) | Real model |
|---|---|---|---|
| A | Normal research flow | The model's `continue_with_tool` reaches the registry unchanged; the tool runs once; the run completes with the tool's own evidence ids and the model's interpretation recorded beside the observer's kind. | not measured |
| B | Ambiguous identity | The state names the unsettled entity. Asking the user pauses with the question and the candidates. Trying `retrosynthesize` anyway is refused by the registry's `identity_resolved` precondition — the tool never runs and no structure appears. Name-level literature search is allowed and the completion declares `insufficient`. | not measured |
| C | Tool returns nothing | `NO_RESULT` is read as a result, not a failure. An identical tool+arguments re-call is refused by the validator with the reason handed back; a *changed* query is a new call and runs. | not measured |
| D | Tool returns part | `PARTIAL` survives to the result and to the rendered report; the missing branch stays in `limitations`; the run is never dressed up as a whole answer. | not measured |
| E | Direction changes | `update_direction` is recorded between the result that forced it and the new action it led to, with its reason on the audit trail. | not measured |
| F | Evidence insufficient | Candidates stay candidates; `evidence_ids` stays empty when the tool returned none; `evidence_sufficiency=insufficient`, the unresolved questions and the limitations all reach the report. An evidence id the model invented is refused before a hypothesis can cite it. `SUPPORTED` with no evidence is refused at the validator; the study context refuses it again if asked directly. | not measured |
| G | Budget exhausted | `max_tool_calls` stops the run at exactly that many calls; `max_decisions` and a spent wall clock do the same. The run reports `partial`, keeps the evidence it holds, and never reports itself as complete. | not measured |

## Capability checklist against the M2.0 acceptance criteria

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | An open-ended question's next action is chosen by the LLM | **Verified with a scripted provider** | Case A; the tool called is the one the decision named, and no deterministic plan is consulted |
| 2 | The AI changes strategy on tool results | **Verified with a scripted provider** | Case E; the direction change is ordered between the result and the new action |
| 3 | The AI picks between tools rather than following a pipeline | **Verified with a scripted provider** | Case A/B/E: `search_literature` / `resolve_chemical_identity` / `retrosynthesize` are chosen per decision; no fixed plan exists |
| 4 | The AI proposes and maintains checkable hypotheses | **Verified with a scripted provider** | Case F; a hypothesis enters the study context as `proposed`, and `SUPPORTED` is refused without named evidence |
| 5 | The AI identifies evidence gaps and uncertainty | **Verified with a scripted provider** | Case D/F; `evidence_sufficiency`, `unresolved_questions` and `limitations` all reach the report |
| 6 | The AI asks the user when it should | **Verified with a scripted provider** | Case B; the pause carries the question and the options, and an unanswered question is never recorded as an answer |
| 7 | The AI keeps researching and stops inside a budget | **Verified** | Case G; every bound is charged on the execution path |
| 8 | Research can be saved, resumed and audited | **Partly verified** | Facts, hypotheses, decisions, issues and session summaries persist through `StudyContext`; every turn is a `DecisionRecord`. Resuming works **within one process**; across a restart only the audit trail survives |
| 9 | The AI cannot bypass tool permissions or scientific truth | **Verified** | The validator and registry gates listed in `AI_RESEARCH_INTELLIGENCE.md` §3, each with a test |
| 10 | Golden Cases demonstrate the above | **Yes, with the caveat at the top** | Cases A–G in `tests/ai/test_research_intelligence.py` |

## What is genuinely unimplemented

1. **No measurement against a real model.** Nothing in the default suite
   holds an API key. The live path exists (`--provider
   openai-compatible`, `CREA_LLM_API_KEY`) but has not been exercised as
   an evaluation here, and no claim is made about its scientific quality.
2. **No cross-process pause resume.** A paused run can be answered by the
   process that started it. The decisions and the study context persist;
   the paused handle does not.
3. **No streaming progress.** A run is synchronous. The HTTP entry
   returns the whole result.
4. **No authenticated identity.** `study_id` and an approval are
   declared values, not authenticated ones.
5. **No token-accurate budgeting.** Output tokens are bounded by the
   provider's own parameter and reported when it answers; the prompt-side
   state document is bounded by characters, not by a tokenizer.
6. **No claim about chemical correctness.** The loop is honest about what
   it did and what it rests on. Whether a conclusion is *right* is the
   Golden Case evaluation layer's job (M1.7), not the loop's.

## How to measure the real thing next

1. Configure `CREA_LLM_API_KEY` (and `CREA_LLM_BASE_URL` /
   `CREA_LLM_MODEL` for a non-default endpoint).
2. Run each case as a `GoldenCase` through `run_blind_case` with
   `AiResearchRuntime` as the runtime factory — the sealed path already
   works and is pinned by a test.
3. Unseal, compare the trace against the expert material, and write
   `EvaluationResult`s under a rubric version.
4. Record the per-case outcomes here, replacing "not measured".

That is M2.1's job. This document will not pretend to have done it.

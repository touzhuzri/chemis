# Phase 3.5 Completion Report

## Phase 3.5 Status

**Complete.** Stopped at the phase boundary; Phase 3.6 not started.

## Baseline

Recorded at §0 before any Phase 3.5 change:

```
943 passed, 21 skipped
```

## New tests

51 tests added by Phase 3.5.

| File | Tests | Covers |
|---|---|---|
| `tests/unit/test_route_evidence.py` | 19 | Ladder A–D, the four rules, evidence integrity, step isolation, conflicts |
| `tests/unit/test_literature_reaction_linking.py` | 20 | Candidate construction, the discovery-only branch, exact/partial/transformation, provenance |
| `tests/unit/test_route_report_evidence.py` | 11 | PDF → report closure, report language, §12 chain |
| `tests/integration_offline/test_route_e2e.py` | 1 | §12's four tools in order through the real agent loop |

Two further tests were added to existing files so the vocabularies cannot be
merged later: `test_the_four_states_are_distinguishable` (the matcher's four
versus the evidence layer's two) and `test_a_metadata_only_paper_is_never_more_than_a_discovery`.

The two tests named in §13 exist:
`test_title_match_never_becomes_reaction_support`
(`tests/unit/test_literature_reaction_linking.py`) and
`test_missing_evidence_cannot_be_upgraded`
(`tests/unit/test_route_evidence.py`).

All 19 §13 scenarios are covered; the mapping is in
`docs/PHASE35_ROUTE_EVIDENCE.md`.

## Total tests

```
collected offline: 1050
```

## Passed

```
pytest -q                     1002 passed
CREA_LIVE_TESTS=1 pytest -q   1016 passed
```

## Failed

```
pytest -q                     0
CREA_LIVE_TESTS=1 pytest -q   8   -- all ENVIRONMENT/QUOTA FAILURE, see below
```

**No CODE FAILURE in either run.**

## Skipped

```
pytest -q                     48
CREA_LIVE_TESTS=1 pytest -q   26
```

Every skip is accounted for and none is a regression:

| Reason | Count | Kind |
|---|---|---|
| `test_scenario_bc_pending.py` — Phase 2 scenario placeholders | 21 | deliberate, pre-existing |
| `CREA_LIVE_TESTS=1` gate | 21 | deliberate (offline run only) |
| `CREA_AIZYNTHFINDER_LIVE=1` gate | 5 | deliberate (both runs) |
| AiZynthFinder worker env not importable | 1 | environment |
| network guard lifted while live testing | 1 | deliberate (live run only) |

The live-run failures, classified as §17 requires:

| Test | Symptom | Classification |
|---|---|---|
| `test_live_provider_returns_text` | `RateLimited: LLM endpoint returned 429: openrouter.ai` | **ENVIRONMENT/QUOTA** |
| `test_live_provider_can_emit_a_structured_tool_call` | same 429 | **ENVIRONMENT/QUOTA** |
| `test_live_agent_completes_a_one_step_task` | "the model made no move at all" | **ENVIRONMENT/QUOTA** — cascade |
| `test_live_extraction_produces_a_validated_record` | same 429 | **ENVIRONMENT/QUOTA** |
| `test_live_extraction_invents_nothing` | same 429 | **ENVIRONMENT/QUOTA** |
| `test_live_extraction_cites_real_evidence` | same 429 | **ENVIRONMENT/QUOTA** |
| `test_live_model_can_run_the_route_tools` | "used []" | **ENVIRONMENT/QUOTA** — cascade |
| `test_live_model_can_validate_a_step` | "assert 'retrosynthesize' in []" | **ENVIRONMENT/QUOTA** — cascade |

The three "cascade" failures are the same root cause observed downstream. A
direct diagnostic run of `test_route_live` returned:

```
STATUS: TaskStatus.FAILED
TOOLS USED: []
TASK ERRORS:
  - llm provider failed: RateLimited: the LLM endpoint returned 429: https://openrouter.ai/api/v1/chat/completions
```

The agent recorded the provider failure and made no tool call — which is the
correct behaviour, since inventing a route would be worse. The literature and
HTTP tests in the same run **passed** (15 of the non-LLM live tests), so the
network is fine and only the OpenRouter LLM quota is exhausted. Re-running
`CREA_LIVE_TESTS=1 pytest -q` after the quota resets should clear all eight.

## Reaction evidence

`src/crea/core/models/reaction_linking.py` defines the five-rung
`ReactionEvidenceLevel`, `ReactionMatchResult`, `ReactionRecordLink` and
`CandidateRouteStepEvidence`. `evidence_level` and `candidate_status` are
separate fields answering separate questions. No confidence, probability or
success-rate field exists on any of them.

## Route evidence

`src/crea/services/route_evidence.py` produces `RouteEvidenceReport` through
Steps A–D and enforces the four rules: nothing skips a rung, every cited
evidence id is resolved, a record attaches only on a measured basis, and user
provenance is never restated as general literature. `at_least()` deliberately
ranks the two experimental rungs as peers.

## Literature linking

`src/crea/services/literature_reactions.py` produces *candidates*, not
reactions. `structure_availability` is derived by a model validator rather than
set by convention, so a metadata-only record cannot be labelled `AVAILABLE`.
`DISCOVERY_ONLY` and `NO_MATCH` are kept distinct.

## Local PDF linking

`PDF → Document → Evidence → ReactionRecord → ReactionRecordLink → RouteStep →
RouteEvidenceReport → Chinese report` is a real path in
`tests/unit/test_route_report_evidence.py`: a generated PDF, the production
`PypdfDocumentParser`, the real document search, the real extractor, the real
registry. `Experimental / 80 °C / 12 h / 82% isolated yield` reaches
`USER_DOCUMENT_EXPERIMENTAL_SUPPORT` on step 1 and `RETROSYNTHESIS_CANDIDATE`
on step 2.

## Git

`git init` run in `F:\chemistry`. `.gitignore` written covering `.env`, `*.key`,
`*.pem`, `*.p8`, `*.p12`, `*.pfx`, `*.crt`, `runtime/`,
`runtime/aizynthfinder-env/`, `__pycache__/`, `.pytest_cache/`, `*.pyc`, plus
`*credentials*` / `*secret*` and the `.azf*` exploratory scratch.

## Secrets protection

Verified before reporting:

- `git check-ignore -v .env` → matched by `.gitignore:3:.env`
- `git ls-files --others --exclude-standard | grep -x .env` → **no match**
  (`.env` is not among the files git would add)
- content scan for `sk-…` / `api_key = <literal>` / `Bearer <literal>` across
  source and docs → **no matches**
- `crea.env.example` is the only key-named file visible to git, and every value
  in it is empty

**No commit was made** — the repo has no automatic-commit policy.

## Known limitations

1. The 8 live failures above are a quota condition, not a code condition. They
   have not been cleared by a second run because that would spend the same
   quota again without new information.
2. All 19 §13 scenarios are covered at the service and report level. The five
   §10 no-auto-upgrade cases are not each driven through a scripted
   `ResearchAgent` transcript; that would be scaffolding rather than coverage,
   since the promotion decision is made by `crea.services.route_evidence` and
   that is where the tests sit. Rationale in
   `docs/PHASE35_ROUTE_EVIDENCE.md`.
3. The report-language check (`tests/fakes/report_language.py`) is a per-line
   heuristic over Chinese and English, not a parser of either. It will catch a
   future edit that writes `推荐路线 A`; it is not a proof that the renderer
   cannot produce a verdict.
4. The `.gitignore` additionally ignores `.azf*` scratch left by exploratory
   AiZynthFinder runs. This is an ignore, not a deletion, and the line is
   commented — remove it if those files should be tracked.
5. `runtime/aizynthfinder-env/` cannot currently import `aizynthfinder`
   cleanly (`AttributeError: module 'aizynthfinder' has no attribute
   '__version__'`), so E2E B and the five AiZynthFinder live tests skip. This
   is a pre-existing environment condition, unchanged by this phase.

---

# The seven questions

## 1. 一个 AiZynthFinder candidate 如何进入 RouteEvidenceReport？

It enters at the bottom, and only at the bottom.

`retrosynthesize` writes a `CandidateRoute` into the `RouteWorkbench`.
`route_evidence_report` reads it back and, for each step, builds a
`CandidateRouteStepEvidence` with:

- `candidate_status` = the step's existing `status` — **read, never written**
- `evidence_level` = `RETROSYNTHESIS_CANDIDATE`
- `match_type` = `NO_MATCH`
- `evidence_refs` = `[]`
- `support_reason` naming that a tool proposed it and nothing has been consulted

It then looks for literature candidates and reaction records that name that
step. Finding none, it stops there. The route is reported in full — every step
appears in the report whether or not anything supports it — because a step that
disappears from the report is a step the reader cannot doubt.

There is no `evidence_level` on the *route*. Three steps at three different
rungs have no single honest label; an average would be a claim about the route
that no step made.

## 2. 什么情况下才能成为 literature-supported？

All of:

1. A literature candidate carries **reaction structures** (`reactants` /
   `products` / `reaction_smiles`), making it `AVAILABLE`.
2. `compare_reactions` measures `EXACT` — both sides matched. `PARTIAL`,
   `TRANSFORMATION` and a near miss all stay at `LITERATURE_DISCOVERY`.
3. The structures are backed by **evidence ids that resolve** in the
   `EvidenceRegistry`.

Only then does the step reach `LITERATURE_REACTION_SUPPORT`.

A structural match without citable evidence is a *finding about structures*, not
evidence about a reaction, and is held at discovery. A title, an abstract or a
DOI can raise a step to `LITERATURE_DISCOVERY` and no further, whatever they
contain — including a title naming both the reactants and the product.

**Never** literature-supported: any `NO_REACTION_STRUCTURE_AVAILABLE` record,
any `PARTIAL`/`TRANSFORMATION` match, any exact match whose evidence does not
resolve, and any paper whose only connection to the step is that our product
appears in its title.

## 3. 什么情况下才能成为 experimentally-supported？

All of:

1. A validated `ReactionRecord` exists — the extraction passed its checks and
   carries stated conditions with citations.
2. A `ReactionRecordLink` names that step and carries **structures that were
   actually read**. `build_record_link` runs `compare_reactions` itself and
   records the measurement; it does not accept a claimed basis.
3. The basis is `EXACT_STRUCTURE` on a *measurement*, not on a caller's word.
   A link declaring `EXACT_STRUCTURE` but carrying no structures on either side
   is refused by `_measured_exactly` — a comparison needs something to compare.
4. The record's fields cite evidence ids that **resolve**.

Then:

- evidence from a publisher-like source → `EXPERIMENTAL_REACTION_SUPPORT`
- any evidence from `user_provided`, including mixed provenance →
  `USER_DOCUMENT_EXPERIMENTAL_SUPPORT`

A record attached without an exact structure comparison is **reported but does
not promote**: it lands in `unlinked_records` with a warning naming the basis it
failed on. The procedure is real; that it is *this* procedure was not shown.

## 4. USER_PROVIDED PDF 如何保留 provenance？

Through `Evidence.source == "user_provided"` and
`Evidence.strength == USER_PROVIDED_EVIDENCE`, both set when the document is
ingested, and both carried unchanged through the chain.

`RouteEvidenceService` reads the *sources* of the resolved evidence, not the
record's own claims about itself. `evidence_is_user_provided` / the
`user_provided` membership check decide the rung, and `support_provenance` is
set to `"user_provided"` or `"literature"` as a **separate field** from
`evidence_level`.

The two experimental rungs are peers — `at_least()` ranks them by membership
rather than `>=`, so neither can climb over the other. They differ in provenance,
not in strength, and the report labels them separately
(`文献实验记录支持` vs `用户文档实验支持`).

**Mixed provenance takes the user rung.** One publisher passage and one local
passage cannot be reported as a published procedure, because part of it is not
one. Being mixed is a reason to claim less, not more.

The local path is testable end to end:
`tests/unit/test_route_report_evidence.py::test_a_procedure_read_from_a_local_pdf_reaches_the_user_rung`
runs a generated PDF through the production parser, the real document search and
the real extractor, and asserts the rung, the provenance and the surviving
conditions (80 °C / 12 h / 82%) on step 1 — and nothing on step 2.

## 5. metadata-only paper 为什么不能被写成 experimental evidence？

Because nothing in it says which structures were combined, under what
conditions, and with what outcome.

A metadata record is built by `candidates_from_papers`, which takes what the
provider gave: title, abstract, DOI, URL, date. A validator then *derives*
`structure_availability = NO_REACTION_STRUCTURE_AVAILABLE` — it is not a flag
anyone sets, so a caller cannot label a structureless record as having
structures. Matching against a step therefore short-circuits to
`DISCOVERY_ONLY` with a `match_basis` that says so in words.

The report renders this as:
`DISCOVERY_ONLY（记录无可比较的反应结构）`, with
`best_similarity = None` and `evidence_refs = []`.

The strongest case is the one in
`test_title_match_never_becomes_reaction_support`: a paper titled
"Synthesis of 1,4-diphenylbutadiyne by Glaser coupling" whose title names our
product, matched against a step producing exactly that. It stays at
`LITERATURE_DISCOVERY`. So does one naming the reactants too. So does one whose
*abstract* states "82% yield at 80 C for 12 h" — an abstract is not a scheme.

Treating any of these as experimental evidence would mean reading a reaction out
of a title. The test docstring says it is written to fail loudly if somebody
later teaches the layer to do that.

## 6. fake/missing evidence ID 如何处理？

Withheld support, and reported.

A cited id is resolved against the `EvidenceRegistry` before anything else
happens. An id that does not resolve goes to `unresolved_evidence_refs` on the
step and to `report.unresolved` as a note naming it. It is never counted as
support.

Unresolved evidence **costs the rung**, not just a footnote:

- a literature match with unresolved structures stays at `LITERATURE_DISCOVERY`
  even when the structures matched exactly
- a reaction record whose fields cite nothing resolvable promotes **nothing**,
  landing in `unlinked_records`
- a record link citing a mix of resolvable and unresolvable ids promotes
  nothing, because only the resolvable ones count and if none remain the link
  stands on nothing

The report prints the ids under
`未能解析的证据 ID`, so the reader sees the gap rather than a cleaned-up answer.

The registry itself refuses a collision: adding a different `Evidence` under an
existing id raises `ValueError`, so identity cannot drift silently.

Two named tests: `test_missing_evidence_cannot_be_upgraded` (which also passes a
completely fabricated id) and
`test_a_record_with_no_resolvable_evidence_promotes_nothing`. The report side is
covered by `test_unresolved_evidence_is_written_into_the_report`, which asserts
the id itself appears in the Chinese output.

The model cannot manufacture an id either: `ResearchAgent._verify_claims` drops
any claim citing evidence the task never produced.

## 7. route step 之间 evidence 是否隔离？

Yes, and the isolation is structural rather than promised.

A `ReactionRecordLink` names one `step_id`. `_apply_records` iterates the
links and skips every one whose `step_id` is not the step being built. A record
attached to step 2 cannot appear on step 1, whatever it contains.

A literature candidate is compared against each step's own reactants and
products, so its match is per-step by construction: an exact match on step 1 is
a `PARTIAL` on step 2 and that difference is recorded per step.

Each `CandidateRouteStepEvidence` carries its own `evidence_refs`,
`unresolved_evidence_refs`, `literature_support`, `experimental_support` and
`unlinked_records`. The `RouteEvidenceReport` also carries a route-level
`evidence_refs` union so a reader can see everything the chain used, but the
per-step lists are what the rung is computed from.

Two dedicated tests: `test_evidence_is_isolated_between_steps` (two steps with
different products, one record linked to step 2; step 1 stays at the bottom with
empty lists) and
`test_the_conditions_land_on_the_step_whose_structures_they_describe` (the same
isolation driven by a real PDF and a real record).

Also tested: `test_every_step_is_reported_even_when_evidence_exists_for_one_of_them`
— three steps, evidence for the middle one, all three reported, and the
unsupported two still at `RETROSYNTHESIS_CANDIDATE`.

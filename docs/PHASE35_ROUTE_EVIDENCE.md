# Phase 3.5 — Evidence-backed Route Intelligence

How a candidate route step earns the right to be described as literature-supported
or experimentally-supported, and what stops it from earning that right by
accident.

## The distinction everything rests on

Three statements that are often spoken as if they were one:

| Statement | What it asserts | What it does not assert |
|---|---|---|
| *candidate route* | a tool proposed this disconnection | that anyone has run it |
| *literature-supported step* | a work was found whose reaction **structures match this step**, and those structures are backed by citable text | that the work ran it, or that the conditions transfer |
| *experimentally-supported step* | a `ReactionRecord` with stated conditions matches this step exactly, and its fields cite evidence this task holds | that the conditions will work for you |

A route is a list of steps. It is not itself supported or unsupported: there is
no aggregate rung, and deliberately no `evidence_level` on `RouteEvidenceReport`.
An average of rungs would be a claim about the route that no step made.

## The five rungs

```
retrosynthesis_candidate                # a tool proposed it
  └─ literature_discovery               # a work was found; nothing reaction-level
       └─ literature_reaction_support   # structures match, and are citable
            ├─ experimental_reaction_support         # a ReactionRecord, published provenance
            └─ user_document_experimental_support    # a ReactionRecord, user-provided PDF
```

The last two are **peers**, not a ranking. `at_least()` treats them as equal by
membership rather than by `>=`, so neither can climb over the other. They differ
in *provenance*, which is why they have different names and why
`support_provenance` is a separate field from `evidence_level`. This project
cannot tell a publisher's PDF from a scan of somebody's notebook, so promoting
a user file into `GENERAL_LITERATURE_SUPPORT` would report a local file as the
published literature. Mixed provenance therefore takes the *user* rung: being
mixed is a reason to claim less, not more.

Two other values exist and are not rungs. They answer *why* there is no
support, and live in `ReactionMatchType` beside the matcher's own four:

| Value | Meaning | The matcher's |
|---|---|---|
| `EXACT` | both sides matched | ✓ |
| `PARTIAL` | one side matched | ✓ |
| `TRANSFORMATION` | related, nothing matched exactly | ✓ |
| `DISCOVERY` | the matcher ran, found nothing related | ✓ |
| `NO_MATCH` | structures existed; they did not fit | evidence layer |
| `DISCOVERY_ONLY` | there were **no structures to compare** | evidence layer |

Collapsing `DISCOVERY_ONLY` into `NO_MATCH` would tell a reader we looked at a
reaction and found it wrong, when we never had it to look at. This is the
central honesty of the phase: provider metadata is *usually* a discovery and
nothing more, because no provider hands back a parsed scheme.

## The four rules the service enforces

1. **Nothing skips a rung.** `_promote` only moves upward; a match can never be
   written as support without passing through the rung it actually earned.
2. **Every cited evidence id is resolved.** Unresolved ids land in
   `unresolved_evidence_refs` and `report.unresolved`, and they *cost* the rung
   rather than merely being reported: an exact match whose structures point at
   evidence nobody holds is held at `LITERATURE_DISCOVERY`.
3. **A record attaches only on a measured basis.** `build_record_link` runs
   `compare_reactions` itself. A link built by hand may name any `basis` it
   likes, including `EXACT_STRUCTURE`; `_measured_exactly` refuses a link that
   carries no structures on either side, because a comparison needs something
   to compare. Such a link is reported in `unlinked_records` and promotes
   nothing.
4. **User provenance is never restated as general literature.** See above.

## The chain

```
retrosynthesize
  └─ search_reaction_evidence    what a search turned up (usually DISCOVERY_ONLY)
       └─ validate_route_step    step status as the validator saw it
            └─ route_evidence_report   what the step may claim
```

`candidate_status` and `evidence_level` are two different questions and are two
different fields. `candidate_status` is read from `CandidateRoute.steps[i].status`
and is never written by this phase.

## Deliberately absent

No confidence, probability or success-rate field exists on any Phase 3.5 model,
and the `ReactionMatchResult` docstring says there will not be one. RDKit's
Tanimoto is `best_similarity`: a measurement named for what it is. It is not a
probability of anything.

## Future work / deferred

**Phase 3.5 forbids these outright.** They are listed so that "we didn't do it"
is distinguishable from "we didn't think of it", and so that a later phase can
reason about them without re-deriving the decision:

- atom mapping
- reaction center prediction
- reaction outcome prediction
- yield prediction
- route success probability
- route ranking
- route recommendation
- automatic commercial reagent purchasing
- broad web crawling
- paywall bypass
- unknown-license PDF / model downloading
- Web UI
- vector DB
- embedding
- OCR
- DECIMER
- ASKCOS
- name → SMILES conversion

If any becomes necessary later, it goes through a new phase and a new decision —
not through a quiet edit to this one.

**Not covered by the Phase 3.5 tests, and why:**

| Gap | Why it is a gap |
|---|---|
| The 19 §13 scenarios are each covered by a test, but not all five §10 no-auto-upgrade cases have a dedicated end-to-end run through the *agent loop*. Cases 1–5 are covered at the service and report level (`tests/unit/test_route_evidence.py`, `tests/unit/test_literature_reaction_linking.py`, `tests/unit/test_route_report_evidence.py`); driving each through `ResearchAgent` would need five scripted LLM transcripts, which is scaffolding rather than coverage. | Coverage is at the layer that makes the decision. |
| A second, independently-extracted `ReactionRecord` from a *different* PDF on the same step. | Two links to one step are tested; two records with *disagreeing* conditions are tested as a conflict. Two records from different files with agreeing conditions is the same path. |
| `route_evidence_report` with `record_links` produced by a real model rather than a test harness. | E2E A in `tests/integration_offline/test_route_e2e.py` drives the real `ToolRegistry`, real agent loop and real validation service, but with `FakeLLMProvider`. A model that fabricates a `basis` is exactly what `_measured_exactly` rejects, and that rejection is tested directly. |

## Where things live

| Concern | File |
|---|---|
| Ladder, link/match/evidence models | `src/crea/core/models/reaction_linking.py` |
| Literature candidates and matching | `src/crea/services/literature_reactions.py` |
| Steps A–D, the four rules | `src/crea/services/route_evidence.py` |
| `route_evidence_report` tool | `src/crea/agent/toolsets.py` |
| Chain collection into the report | `src/crea/agent/loop.py` |
| Agent discipline (rules 19–27) | `src/crea/agent/prompt.py` |
| Chinese route evidence section | `src/crea/report/research.py` |

# Golden Cases: blind evaluation infrastructure (M1.7.1 + M1.7.2)

A **golden case** is one standard research evaluation: CREA receives a
sealed input, runs blind, and only afterwards is the expert material
opened and a verdict written. This document covers the data contract
(M1.7.1) and the storage, access isolation, unseal audit, content
verification and blind runner (M1.7.2).

## 1. What a golden case is for

Two evaluation regimes, never mixed:

- `blind_reproduction` — the expert paper, SI, route and results are
  **hidden** until the run ends. Guards against the model reciting a
  paper it memorised.
- `open_literature_research` — the agent may read public literature;
  the evaluation judges retrieval, extraction, evidence quality and
  traceability.

Every case separates four objects:

```text
GoldenCase
├── BlindInput          ← what the agent sees (M1.6 ResearchIntent inside)
├── ReferenceMaterial[] ← expert material: metadata + locator + hash, sealed
├── ResearchTrace[]     ← what a run actually did (written after execution)
└── EvaluationResult[]  ← verdicts against a trace, under a rubric version
```

## 2. Three version layers

| Layer | Field | Meaning |
|---|---|---|
| Schema | `GOLDEN_CASE_SCHEMA_VERSION` / `GoldenCase.schema_version` | The shape of the models. Moves only when their meaning changes. |
| Case content | `GoldenCase.case_version` | Which content this case has. Every content edit bumps it; the `case_id` never moves. |
| Rubric | `EvaluationResult.evaluation_version` | Which rules a verdict was written under. Two rubrics can judge one trace. |

Traces and evaluations **freeze the case version they saw**: editing a
case never re-labels an old run, and an evaluation that does not judge
the exact version its trace ran is refused
(`EVALUATION_VERSION_MISMATCH`).

## 3. Creating and managing cases

```python
from pathlib import Path

from crea.core.models.golden_case import (
    BlindInput, GoldenCase, GoldenCaseStatus, GoldenCaseType,
    ReferenceMaterial, ReferenceMaterialKind, AnswerContentKind,
)
from crea.services.golden_case_store import GoldenCaseStore
from crea.services.knowledge_base import KnowledgeBase

kb = KnowledgeBase(Path("data/crea.db"))
kb.initialize()
store = GoldenCaseStore(kb)

case = GoldenCase.build(
    "阿司匹林绿色路线复现", GoldenCaseType.BLIND_REPRODUCTION,
    description="JOC 2019 的三步路线，隐藏 SI 与专家路线",
    status=GoldenCaseStatus.EXECUTABLE,
    blind_input=BlindInput(goal="寻找阿司匹林的绿色合成路线，优先近五年文献"),
    tags=["aspirin", "green"], domains=["药物合成"],
)
store.create(case)                       # duplicate case_id -> CASE_EXISTS

# Edit content: read, bump the version, write.
current = store.get(case.case_id)
store.update(current.model_copy(update={
    "case_version": "2",
    "blind_input": BlindInput(goal="寻找阿司匹林的绿色三步以内路线"),
}), expected_version=current.case_version)   # stale read -> CASE_VERSION_CONFLICT

store.set_status(case.case_id, GoldenCaseStatus.ARCHIVED)  # terminal
```

### Attach expert references (still sealed)

```python
reference = ReferenceMaterial.build(
    ReferenceMaterialKind.EXPERT_ROUTE,
    title="Expert route", doi="10.1000/example",
    content_locator="refs/aspirin-route.txt",   # relative to a configured root
    content_hash="sha256:2c26...",              # verify before trusting
    answer_content=[AnswerContentKind.EXPERT_ROUTE, AnswerContentKind.YIELDS],
)
store.save_reference(case.case_id, reference)
# reference.hidden is True by default; a metadata update cannot change it.
```

## 4. Agent view vs manager view

Two **different projections**, not one object with a hide-flag.

```python
agent_view = store.get_agent_view(case.case_id)
# GoldenCaseAgentView: case_id, case_type, case_version, schema_version,
#                      status, blind_input. Nothing else exists on it.
# No reference_ids, no DOI, no titles, no locator, no hash, no verdicts.

manager_view = store.get_manager_view(case.case_id)
# GoldenCaseManagerView: the case, reference metadata, traces, evaluations,
#                        unseal events. For evaluation tooling only.
```

The agent view is built by projection from the case document alone;
reference rows are never loaded into it. `extra="forbid"` closes the
constructor-smuggling hole. Reading the manager view **unseals
nothing**.

## 5. Running a case blind

```python
from crea.evaluation import RunEnvironment, run_blind_case
from crea.core.models.golden_case import TraceModelInfo, TraceToolVersion

outcome = run_blind_case(
    store, case.case_id,
    runtime_factory=lambda: CoreAgentRuntime(build_agent_tools(...)),
    environment=RunEnvironment(
        models=[TraceModelInfo(name="...", version="...", provider="...")],
        tools=[TraceToolVersion(tool="search_literature", version="1")],
        git_commit="5784ac5",
    ),
)
# outcome.trace is already saved through store.save_trace.
```

`run_blind_case` touches the store through exactly two doors:
`get_agent_view` in, `save_trace` out. The `BlindResearchRunner` object
itself holds no store and cannot name the unseal or content services.
The trace freezes `case_id`/`case_version` from the view and records
session id, plan, steps with observation kinds, re-planning events,
person approvals, evidence ids, warnings/failures and the declared
environment.

The ordinary `CoreAgentRuntime.run(...)` entry is unchanged; the runner
is a new caller, not a new runtime.

## 6. Explicit unseal

```python
from crea.core.models.golden_case_access import UnsealRequest
from crea.services.golden_case_unseal import GoldenCaseUnsealService

events = GoldenCaseUnsealService(store).unseal(UnsealRequest(
    case_id=case.case_id,
    reference_ids=["ref-..."],        # empty list = every reference
    actor="evaluator:张三",            # required
    reason="研究完成，进入专家对照评价阶段",  # required
))
# Each event: unsealed | already_unsealed | failed -- append-only,
# with case id, case version at attempt time, before/after hidden state.
```

Rules: the store's `save_reference` preserves `hidden` (metadata edits
neither unseal nor re-seal); `set_reference_sealed_state` is the
privileged writer and only the unseal service calls it; audit events
have no update and no delete API; a repeat unseal is a new
`already_unsealed` event, never a rewrite.

## 7. Reading reference content (hash-verified)

```python
from crea.services.reference_content import ReferenceContentReader

reader = ReferenceContentReader(store, allowed_roots=[Path("sealed-data")])
result = reader.read(case.case_id, reference.reference_id)
if result.status is ContentStatus.OK:
    bytes_verified = result.content          # only verified bytes come back
else:
    ...  # NOT_FOUND | UNREADABLE | HASH_MISSING | HASH_MISMATCH |
         # NOT_UNSEALED | NOT_ALLOWED -- each with content=None
```

Safety: only relative locators under the configured roots resolve;
absolute paths, drive letters, `..` (checked after resolution) and
every URL scheme are `NOT_ALLOWED` before any I/O — the reader never
touches the network, so a locator cannot become an SSRF. Error details
name the reference and status, never the resolved local path. Reading a
sealed reference returns `NOT_UNSEALED`: reading is not unsealing.
Missing or mismatched hashes never return content as "the reference".

## 8. Traces and evaluations

```python
from crea.core.models.golden_case import (
    EvaluationResult, EvaluationStatus, DimensionEvaluation,
    EvaluationDimension,
)

store.save_evaluation(EvaluationResult(
    evaluation_id="eval-1",
    case_id=case.case_id,
    case_version=outcome.trace.case_version,   # the version the trace ran
    trace_id=outcome.trace.trace_id,
    evaluation_version="rubric-1",
    evaluation_type=case.case_type,
    evaluator="expert:张三",
    status=EvaluationStatus.PARTIAL,
    dimensions=[DimensionEvaluation(
        dimension=EvaluationDimension.TARGET_IDENTITY,
        verdict=EvaluationStatus.PASS,
        evidence_ids=[reference.reference_id],   # cite what supports the call
    )],
    rationale="目标与断键正确；条件证据不足",
))
```

Five verdicts (`pass/partial/fail/not_evaluable/insufficient_evidence`)
across ten reserved dimensions; scores are optional and must carry a
`score_basis`. A verdict pinned to a version its trace did not run is
refused.

## 9. Security boundary — what this is and is not

**Is at the application layer (tested):**

- The agent path receives a `GoldenCaseAgentView` whose fields cannot
  carry a reference; serialization of it cannot leak what was never
  loaded.
- The runner names neither the unseal nor the content service (import
  boundary, pinned by architecture tests).
- Unsealing is explicit, named, reasoned, append-only audited; no other
  path flips `hidden`.
- Content reads are root-confined and hash-verified; unverified bytes
  are never returned as reference material.

**Is not (honest limits):**

- Not an OS sandbox or process isolation. Any Python code holding the
  `GoldenCaseStore` can call `get_manager_view` — the guarantee is that
  the agent-facing code path never does, and that opening a reference
  always leaves a named, reasoned audit event.
- No user-auth/RBAC layer; `actor` is a declared string, not an
  authenticated identity. An authenticated deployment is future work.
- Case prose (`name`, `description`, `tags`) is manager-side: the agent
  view omits it because case names can name the paper.

## 10. Not implemented (by design, later stages)

- Paper/SI PDF parsing, OCR, reaction extraction from references.
- The evaluation *process* (who judges, when, with which rubric UI).
- Authenticated access control and multi-user audit identity.
- Benchmark dashboards and cross-version capability reports.
- Any AI Research Intelligence: M1.7.2 is infrastructure for judging
  research, not the research brain itself (that is M2.0).

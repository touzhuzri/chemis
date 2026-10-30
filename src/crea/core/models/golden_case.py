"""M1.7.1 -- the golden-case data contract: what an evaluation may see, and
what it must never see.

A golden case is one standard research evaluation: CREA receives a
`BlindInput` (the visible half), runs, and produces a `ResearchTrace`; only
after the run are the `ReferenceMaterial`s unsealed and an
`EvaluationResult` written against them. This module fixes the five data
objects and the isolation boundary between them. It stores no case on disk,
performs no access control, and scores nothing -- those belong to M1.7.2+.

The boundary, as structure rather than convention:

* `GoldenCase` holds the metadata plus the *visible* half
  (`blind_input`) and, for the hidden half, **only ids**
  (`reference_ids`). A `GoldenCase.model_dump()` therefore cannot carry a
  reference's metadata, let alone a paper's body: the object is not there
  to serialize.
* `ReferenceMaterial` holds bibliographic metadata, a *locator* (where an
  authorized keeper stores the content) and an optional content hash. It
  has **no field that can hold body text** -- no `content`, no `body`, no
  `raw_quote`. Unsealing a reference hands over the stored artifact the
  locator names; nothing in this schema can smuggle it into an agent's
  context.
* `BlindInput` reuses M1.6's `ResearchIntent` / `ResearchConstraint` and
  the identity layer's `StructureInput`, and has no field that points at a
  reference. What the agent sees is exactly what a blind run may see.

Honest limitation, recorded here rather than papered over: pydantic models
are a contract, not an access control system. Any code that legitimately
holds a `ReferenceMaterial` can read its metadata. True isolation -- who
may dereference a locator, when the seal opens, what the runtime is handed
-- is M1.7.2's job (a case store with separate agent/manager views). What
this stage *can* guarantee, it guarantees by structure: the agent-facing
objects contain no path to reference content, and no field exists for that
content to live in.

Three version layers, deliberately never merged into one `version`:

* `GOLDEN_CASE_SCHEMA_VERSION` -- the shape of these models. Bumped when
  their meaning changes.
* `GoldenCase.case_version` -- one case's *content*. Editing a case bumps
  this and never the schema version.
* `EvaluationResult.evaluation_version` -- the *rules* a verdict was
  written under, so two evaluations of one trace under different rubrics
  are distinguishable.

Execution-recorded environment facts (model name/version/cutoff, tool
versions, data-source versions, CREA git commit) live on `ResearchTrace`
-- recorded once a run actually happens, never invented by a case.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator

from crea.core.ids import content_hash
from crea.core.models.identity import StructureInput
from crea.core.models.knowledge import ResearchPlan
from crea.core.models.research import ResearchStep, TaskStatus
from crea.core.models.research_intent import IntentSource, ResearchIntent
from crea.core.provenance import Provenance, utc_now_iso

#: Bumped when the *meaning* of these models changes. A case serialized
#: under one schema version is not silently interchangeable with another.
GOLDEN_CASE_SCHEMA_VERSION = "golden-case-v1"


class GoldenCaseType(str, Enum):
    """The two evaluation regimes, whose boundaries never merge.

    A blind reproduction hides the expert material until the run ends; an
    open-literature research judges retrieval, extraction and traceability
    against sources the agent was allowed to read. One rubric cannot serve
    both, which is why the type is on the case and on every verdict.
    """

    BLIND_REPRODUCTION = "blind_reproduction"
    OPEN_LITERATURE_RESEARCH = "open_literature_research"


class GoldenCaseStatus(str, Enum):
    """A case's lifecycle. DRAFT may still change; ARCHIVED is terminal."""

    DRAFT = "draft"
    READY = "ready"
    EXECUTABLE = "executable"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class ResourceScope(BaseModel):
    """The tools and sources a run of this case may touch, as the agent is
    told. Part of the visible half: a blind run must know its sandbox."""

    allowed_tools: list[str] = Field(default_factory=list)
    allowed_data_sources: list[str] = Field(default_factory=list)
    #: True when the case is scored offline: no network-backed tool may run.
    offline_only: bool = False
    note: str = ""


class BlindInput(BaseModel):
    """Everything a CREA run of this case may see -- and nothing else.

    The structured half reuses M1.6 rather than inventing a second
    understanding model: `intent` carries the entities (with identity
    states), the constraints (each with its `IntentSource`), and the goal
    as the user stated it. `target_structure` is the case's designated
    molecule in the identity layer's own vocabulary, recorded exactly as
    given.

    There is deliberately no field for a reference, an expert route, a
    paper's body, or an answer of any kind. A blind run sees this object;
    that is the whole of its world.
    """

    #: The research question, verbatim. A blind input with no question is
    #: not an input.
    goal: str
    #: The structured understanding, when one was produced or authored.
    intent: ResearchIntent | None = None
    #: The target molecule, as StructureInput (SMILES / InChI / name ...).
    target_structure: StructureInput | None = None
    #: What the run may use. Optional: a case may leave the sandbox to the
    #: runtime's own policy.
    resource_scope: ResourceScope | None = None
    #: Where this input came from, in M1.6's source vocabulary.
    source: IntentSource = IntentSource.USER_EXPLICIT
    #: The input's own content version (independent of the case's).
    input_version: str = "1"
    created_at: str = Field(default_factory=utc_now_iso)

    @field_validator("goal")
    @classmethod
    def _goal_is_not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError(
                "BlindInput.goal must be the research question; an empty "
                "string gives the agent nothing to research"
            )
        return value


class ReferenceMaterialKind(str, Enum):
    """What kind of expert material this is. A paper, its SI, the expert
    route read from them, and a recorded experimental result are four
    different things and never one 'attachment'."""

    PAPER = "paper"
    SUPPORTING_INFORMATION = "supporting_information"
    EXPERT_ROUTE = "expert_route"
    EXPERIMENTAL_RESULT = "experimental_result"
    OTHER = "other"


class ReferenceAccess(str, Enum):
    """Who may reach the artifact the locator names.

    UNKNOWN is a legitimate state: not knowing an artifact's access is
    exactly when unsealing must ask a person.
    """

    PUBLIC = "public"
    EVALUATION_MANAGER_ONLY = "evaluation_manager_only"
    AUTHORIZED_FOR_AGENT = "authorized_for_agent"
    INACCESSIBLE = "inaccessible"
    UNKNOWN = "unknown"


class AnswerContentKind(str, Enum):
    """The kinds of answer a reference may carry. Recorded on the material
    so the evaluation can see, before unsealing, what it is about to
    compare against -- without reading a word of the content."""

    EXPERT_ROUTE = "expert_route"
    EXPERIMENTAL_CONDITIONS = "experimental_conditions"
    YIELDS = "yields"
    REFERENCE_ANSWER = "reference_answer"


class ReferenceMaterial(BaseModel):
    """One piece of expert material: its identity, its provenance, a
    locator, a hash -- and never its body.

    The content itself lives wherever `content_locator` points, under a
    keeper M1.7.2 will define. This model can record *that* a reference
    exists, *what kind* of answer it contains, and *whether its hash
    matches* a given artifact -- all without one sentence of paper text
    being representable here. That is the isolation this stage can
    enforce, and it is structural, not a promise.
    """

    reference_id: str
    material_type: ReferenceMaterialKind
    title: str = ""
    doi: str = ""
    #: Any other upstream identifier (OpenAlex id, internal catalogue id).
    source_id: str = ""
    authors: list[str] = Field(default_factory=list)
    published_at: str = ""
    #: This material's own version, independent of the case's.
    material_version: str = "1"
    #: Where an authorized keeper finds the content: a path, an opaque key,
    #: a URI inside a sealed store. A pointer, never the pointed-at text.
    content_locator: str = ""
    #: Hash of the stored artifact, for integrity checks after unsealing.
    content_hash: str = ""
    provenance: Provenance | None = None
    #: Default hidden: a reference starts sealed; opening it is a decision.
    hidden: bool = True
    access: ReferenceAccess = ReferenceAccess.EVALUATION_MANAGER_ONLY
    #: What kinds of answer this material contains (metadata only).
    answer_content: list[AnswerContentKind] = Field(default_factory=list)
    note: str = ""

    @field_validator("reference_id")
    @classmethod
    def _id_is_not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError(
                "ReferenceMaterial.reference_id must name the material; "
                "an unidentifiable reference cannot be sealed or unsealed"
            )
        return value

    @classmethod
    def build(cls, material_type: ReferenceMaterialKind, *,
              title: str = "", doi: str = "", **kwargs) -> "ReferenceMaterial":
        """A stable reference id derived from what the material *is*.

        Re-filing the same paper for the same case is the same reference,
        not a second one -- the id hashes kind + (doi or title), so dedup
        is the default and double-counting is not.
        """
        key = f"{material_type.value}|{(doi or title).strip().lower()}"
        return cls(reference_id="ref-" + content_hash(key, 12),
                   material_type=material_type, title=title, doi=doi, **kwargs)


class TraceModelInfo(BaseModel):
    """The model a run used, as much of it as is knowable.

    `knowledge_cutoff` stays a string because providers state it
    inconsistently ("2024-06", a date, or not at all): recording the claim
    verbatim beats inventing a date type it cannot always fill.
    """

    name: str = ""
    version: str = ""
    provider: str = ""
    knowledge_cutoff: str = ""


class TraceToolVersion(BaseModel):
    """One tool that ran, and the version that ran it."""

    tool: str
    version: str = ""


class TraceDataSourceVersion(BaseModel):
    """One data source consulted, and the version/snapshot of it."""

    source: str
    version: str = ""


class TraceToolCall(BaseModel):
    """One executed step's tool call, summarised for the trace.

    A summary, not a second `ToolResult`: the full result belongs to the
    session's own records, and duplicating it here would create the second
    result vocabulary this project refuses. `observation_kind` carries the
    M1.2 six-value classification as data (the trace lives in `core`,
    below the agent, and so names kinds rather than importing the enum).
    """

    step_id: str = ""
    tool: str = ""
    #: An M1.2 ObservationKind value: success / no_result / needs_input /
    #: unavailable / partial / error.
    observation_kind: str = ""
    summary: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    at: str = Field(default_factory=utc_now_iso)


class TraceUserInput(BaseModel):
    """One approval or answer a person gave the paused run.

    `kind` is "approval" or "user_input"; `answer` is the person's own
    words, never a paraphrase the agent produced.
    """

    step_id: str = ""
    question: str = ""
    answer: str = ""
    #: "approval" | "user_input".
    kind: str = "user_input"
    at: str = Field(default_factory=utc_now_iso)


class TracePlanChange(BaseModel):
    """One re-planning event, with the kinds of change and why.

    `change_kinds` carry M1.3 `PlanChangeKind` values as data, and
    `plan_revision` is the plan's revision counter at that moment -- so
    "why did step 7 run at all" is answerable from the trace alone.
    """

    at: str = Field(default_factory=utc_now_iso)
    plan_revision: int = 0
    change_kinds: list[str] = Field(default_factory=list)
    reason: str = ""


class TraceProduct(BaseModel):
    """One final artifact the run produced, by reference.

    `ref` is the product's id in its own store (a report id, a route id,
    an evidence id) -- a pointer, never the artifact's content.
    """

    #: "report" | "route" | "evidence" | "entity" | "other" ...
    kind: str = "other"
    ref: str = ""
    label: str = ""


class ResearchTrace(BaseModel):
    """One run of one case: what it was given, what it did, what it ended
    with -- recorded so "why did it conclude that" stays answerable.

    The trace is assembled from the runtime's own products (M1.4
    `RuntimeResult.steps` are `ResearchStep`s; the plan is a
    `ResearchPlan`; observations are M1.2 kinds), never recomputed or
    re-narrated. This stage defines the shape; the collector is later
    work. A trace that kept only the final route and lost the tool calls,
    the pauses, and the re-planning would be the failure this model
    exists to prevent.
    """

    trace_id: str
    case_id: str
    #: The case version this run was given. Frozen here: a later case edit
    #: must not rewrite what this run actually saw.
    case_version: str
    #: The M1.5 research session the run belonged to.
    session_id: str = ""
    started_at: str = ""
    ended_at: str = ""
    #: The run's own outcome, in the shared TaskStatus vocabulary.
    status: TaskStatus = TaskStatus.PENDING
    #: Environment, recorded not assumed -- each list empty when unknown.
    models: list[TraceModelInfo] = Field(default_factory=list)
    tools: list[TraceToolVersion] = Field(default_factory=list)
    data_sources: list[TraceDataSourceVersion] = Field(default_factory=list)
    #: The CREA commit the run executed at.
    git_commit: str = ""
    #: The plan the run started from, and where re-planning left it.
    plan: ResearchPlan | None = None
    plan_revision: int = 0
    #: The execution trajectory, in the existing ResearchStep vocabulary.
    steps: list[ResearchStep] = Field(default_factory=list)
    #: Per-step tool calls (summarised), person inputs, re-planning events.
    tool_calls: list[TraceToolCall] = Field(default_factory=list)
    user_inputs: list[TraceUserInput] = Field(default_factory=list)
    plan_changes: list[TracePlanChange] = Field(default_factory=list)
    #: What the run produced, by reference.
    products: list[TraceProduct] = Field(default_factory=list)
    #: Evidence ids the tools actually returned, deduplicated, in order.
    evidence_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    #: Hard failures, kept separately: a warning is not a failure and must
    #: never be read as one.
    failures: list[str] = Field(default_factory=list)
    provenance: Provenance | None = None
    collected_at: str = Field(default_factory=utc_now_iso)

    @field_validator("trace_id", "case_id")
    @classmethod
    def _id_is_not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError(
                "ResearchTrace identifiers must be non-empty; a trace "
                "nobody can link to its case cannot be audited"
            )
        return value


class EvaluationDimension(str, Enum):
    """The ten reserved dimensions. Keys are stable; prose may grow around
    them, the values may not silently change meaning.
    """

    #: Did the run work on the right molecule?
    TARGET_IDENTITY = "target_identity"
    #: Structure and stereochemistry as output, vs the expert reference.
    STRUCTURE_STEREOCHEMISTRY = "structure_stereochemistry"
    #: Are the disconnections chemically reasonable?
    RETROSYNTHETIC_DISCONNECTION = "retrosynthetic_disconnection"
    #: Is the route as a whole chemically sound?
    ROUTE_CHEMISTRY = "route_chemistry"
    #: Reaction choices: appropriate types, sensible selectivity.
    REACTION_SELECTION = "reaction_selection"
    #: Do real conditions stand behind the proposed steps?
    CONDITION_SUPPORT = "condition_support"
    #: Quality of the literature evidence the run cited.
    LITERATURE_EVIDENCE = "literature_evidence"
    #: Are starting materials actually obtainable?
    MATERIAL_AVAILABILITY = "material_availability"
    #: Did the process itself run honestly: pauses, gaps, failures shown.
    PROCESS_INTEGRITY = "process_integrity"
    #: Can every conclusion be walked back to what produced it?
    TRACEABILITY = "traceability"


class EvaluationStatus(str, Enum):
    """The verdict vocabulary. Five states, and no forced number.

    A route that differs from the paper's is not automatically FAIL (the
    paper's route is not the only correct one); a route that matches is
    not automatically PASS. NOT_EVALUABLE is "this dimension could not be
    judged"; INSUFFICIENT_EVIDENCE is "judged, and the evidence does not
    decide it". Neither is a polite PASS.
    """

    PASS = "pass"
    PARTIAL = "partial"
    FAIL = "fail"
    NOT_EVALUABLE = "not_evaluable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class DimensionEvaluation(BaseModel):
    """One dimension's verdict, its evidence, and (optionally) a score.

    `score` is `None` by default and must stay that way unless the rubric
    genuinely produced a number *and* `score_basis` says where it came
    from. A number without a basis is exactly the fake precision this
    vocabulary exists to refuse.
    """

    dimension: EvaluationDimension
    verdict: EvaluationStatus = EvaluationStatus.NOT_EVALUABLE
    note: str = ""
    #: Records that support this verdict (Evidence ids, ReferenceMaterial
    #: ids after unsealing).
    evidence_ids: list[str] = Field(default_factory=list)
    score: float | None = None
    #: Why this score, if one exists. Required reading for any number.
    score_basis: str = ""


class EvaluationConfig(BaseModel):
    """The rubric a case asks to be judged under.

    Attached to the case (which dimensions, whether a score is wanted at
    all); `evaluation_version` on each result then records which revision
    of this config actually produced a given verdict.
    """

    evaluation_version: str = "1"
    dimensions: list[EvaluationDimension] = Field(
        default_factory=lambda: list(EvaluationDimension))
    #: False: verdicts only. True: a score is expected where the rubric
    #: can honestly produce one -- and a missing score must carry a basis
    #: explaining why it could not.
    require_score: bool = False
    #: Human-readable criteria, one sentence each.
    criteria: list[str] = Field(default_factory=list)
    note: str = ""


class CaseConstraints(BaseModel):
    """The evaluation policy a run must obey -- the manager's side, not
    the agent's.

    The agent-visible sandbox is `BlindInput.resource_scope`; this is what
    the *evaluation* enforces and records: which model policy applies and
    which environment facts a trace must carry before its verdict can be
    compared across CREA versions.
    """

    #: "record_only" (any model, but the trace must name it) |
    #: "fixed_model" | "any".
    model_policy: str = "record_only"
    #: Environment facts the trace must record for the verdict to count
    #: in cross-version comparisons.
    required_recordings: list[str] = Field(default_factory=lambda: [
        "model", "tool_versions", "data_source_versions", "git_commit",
        "research_time",
    ])
    note: str = ""


class EvaluationResult(BaseModel):
    """One verdict on one trace, under one rubric version.

    Dimensions, not a single number: the ten reserved dimensions each take
    their own verdict, evidence and (optionally) score. The overall
    `status` summarises them and may never contradict the evidence list it
    carries. Two results for one trace under different
    `evaluation_version`s are both legitimate records -- they answer
    different questions.
    """

    evaluation_id: str
    case_id: str
    #: The case version that was judged. Frozen with the trace.
    case_version: str
    trace_id: str
    #: The rubric revision this verdict was written under.
    evaluation_version: str = "1"
    #: The regime the verdict belongs to, matching the case's type.
    evaluation_type: GoldenCaseType
    evaluated_at: str = Field(default_factory=utc_now_iso)
    #: Who or what judged: "expert:<name>" | "rule:<id>" | a team name.
    evaluator: str = ""
    #: An unfilled evaluation decides nothing; it is not a quiet pass.
    status: EvaluationStatus = EvaluationStatus.NOT_EVALUABLE
    dimensions: list[DimensionEvaluation] = Field(default_factory=list)
    rationale: str = ""
    #: Optional overall score; None unless the rubric produced one.
    score: float | None = None
    #: Why this number, if it exists. Empty score + empty basis is the
    #: honest default.
    score_basis: str = ""
    #: Records supporting the verdict (Evidence ids, unsealed
    #: ReferenceMaterial ids).
    evidence_ids: list[str] = Field(default_factory=list)
    unresolved_issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)

    @field_validator("evaluation_id", "case_id", "trace_id")
    @classmethod
    def _id_is_not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError(
                "EvaluationResult identifiers must be non-empty; a "
                "verdict nobody can trace to its case and run is not "
                "auditable"
            )
        return value

    def verdict_for(self, dimension: EvaluationDimension) -> EvaluationStatus:
        """The verdict on one dimension, or NOT_EVALUABLE when the rubric
        did not address it -- an unaddressed dimension is never a pass."""
        for item in self.dimensions:
            if item.dimension is dimension:
                return item.verdict
        return EvaluationStatus.NOT_EVALUABLE


class GoldenCase(BaseModel):
    """One complete evaluation case: metadata, the visible half, and ids
    for the hidden half.

    `reference_ids` are ids, deliberately -- never `ReferenceMaterial`
    objects. The materials live with an evaluation keeper (M1.7.2 defines
    the store and its access control), so serializing this object cannot
    leak their metadata, let alone content. `blind_input` is embedded
    because it *is* the agent's world; it has no field that reaches a
    reference, so a blind run's context is exactly this object's visible
    half.

    `case_id` is the slot; `case_version` is the content. Editing a case
    keeps its id and bumps the version; `schema_version` moves only when
    these models' meaning changes.
    """

    case_id: str
    name: str
    description: str = ""
    case_type: GoldenCaseType
    #: The case's content version. Independent of schema and rubric.
    case_version: str = "1"
    #: Which shape of these models the case was written under.
    schema_version: str = GOLDEN_CASE_SCHEMA_VERSION
    status: GoldenCaseStatus = GoldenCaseStatus.DRAFT
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    tags: list[str] = Field(default_factory=list)
    #: Research fields / chemical classifications this case belongs to.
    domains: list[str] = Field(default_factory=list)
    #: The agent-visible half. None only while a draft has no input yet.
    blind_input: BlindInput | None = None
    #: Ids of the sealed materials. The objects themselves are never here.
    reference_ids: list[str] = Field(default_factory=list)
    evaluation_config: EvaluationConfig = Field(default_factory=EvaluationConfig)
    constraints: CaseConstraints = Field(default_factory=CaseConstraints)
    provenance: Provenance | None = None

    @field_validator("case_id", "name")
    @classmethod
    def _id_is_not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError(
                "GoldenCase.case_id and GoldenCase.name must be non-empty; "
                "an unnamed case cannot be found, versioned or audited"
            )
        return value

    @classmethod
    def build(cls, name: str, case_type: GoldenCaseType, **kwargs) -> "GoldenCase":
        """A case whose id is stable for its (type, name) slot.

        Content edits bump `case_version`; the id does not move, because
        the id answers "which case" and the version answers "which
        content". A store with its own id scheme may ignore this and
        assign one -- the model does not require the hash, only a caller
        who wants a reproducible fixture does.
        """
        key = f"{case_type.value}|{name.strip().lower()}"
        return cls(case_id="case-" + content_hash(key, 16),
                   name=name, case_type=case_type, **kwargs)


__all__ = [
    "GOLDEN_CASE_SCHEMA_VERSION",
    "AnswerContentKind",
    "BlindInput",
    "CaseConstraints",
    "DimensionEvaluation",
    "EvaluationConfig",
    "EvaluationDimension",
    "EvaluationResult",
    "EvaluationStatus",
    "GoldenCase",
    "GoldenCaseStatus",
    "GoldenCaseType",
    "ReferenceAccess",
    "ReferenceMaterial",
    "ReferenceMaterialKind",
    "ResearchTrace",
    "ResourceScope",
    "TraceDataSourceVersion",
    "TraceModelInfo",
    "TracePlanChange",
    "TraceProduct",
    "TraceToolCall",
    "TraceToolVersion",
    "TraceUserInput",
]

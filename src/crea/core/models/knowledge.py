"""Knowledge state, gaps, conflicts, and the deterministic research plan.

Phase 3.12. A research question is not answered by running tools until the
report looks full -- it is answered by knowing *what is already
established*, *what is missing*, and therefore *what this run should do*.
This module is the vocabulary for that:

* `KnowledgeState` -- `UNKNOWN` / `PARTIALLY_KNOWN` / `KNOWN`. A state,
  never a score: there is no percentage of knowledge, and "证据不足,
  置信度 42%" is not a sentence this project can produce. A facet is
  `KNOWN` because named objects stand behind it, `PARTIALLY_KNOWN` when
  some of what the facet needs exists and some does not, `UNKNOWN` when
  nothing does.
* `KnowledgeCoverage` / `KnowledgeGap` -- the state of each facet for one
  target, and the gaps that follow from it. Every gap carries a sentence a
  person can read: "已有反应结构信息，但尚未找到可验证的实验条件证据。"
* `FieldObservation` / `FieldConflict` -- what different papers said about
  one field, each value pointing at its own evidence. A conflict is two
  reports disagreeing (`CONFLICT`), which is not an invalid value
  (`INVALID` -- a proposal validation refused) and never a reason to
  average or pick a winner (§22-§24).
* `ResearchPlan` / `ResearchPlanStep` -- what to do next, derived
  *deterministically* from the gaps (§18). A plan is advice for the
  workflow's own steps and a record of why each runs; the LLM may propose
  additions, but execution stays with the ToolRegistry and the evidence
  chain (§6).

Known knowledge is not current knowledge (§15): `KNOWN` means CREA has
recorded sources and facts about this target -- with their `retrieved_at`
and provenance -- not that they are the latest or still correct. Nothing
here reads as "the truth"; everything reads as "established, and from
where".
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.provenance import utc_now_iso


class KnowledgeState(str, Enum):
    """How much of a facet is established. A state, not a score."""

    UNKNOWN = "unknown"
    PARTIALLY_KNOWN = "partially_known"
    KNOWN = "known"


class KnowledgeFacet(str, Enum):
    """The facets a research question about one compound is made of.

    They follow the knowledge chain itself: who the compound is (ENTITY),
    who reported what about it (PAPER), what transformations are known
    (REACTION), whether real experimental conditions stand behind them
    (EXPERIMENTAL_EVIDENCE), what synthesis candidates exist (ROUTE), and
    whether the route steps are linked to literature reactions
    (ROUTE_EVIDENCE).
    """

    ENTITY = "entity"
    PAPER = "paper"
    REACTION = "reaction"
    EXPERIMENTAL_EVIDENCE = "experimental_evidence"
    ROUTE = "route"
    ROUTE_EVIDENCE = "route_evidence"


class FacetKnowledge(BaseModel):
    """One facet's state, and what it rests on."""

    facet: KnowledgeFacet
    state: KnowledgeState = KnowledgeState.UNKNOWN
    #: Human-readable basis: "3 reactions, 1 experimentally supported".
    detail: str = ""
    #: The objects behind the state -- reaction ids, evidence ids, route ids.
    object_ids: list[str] = Field(default_factory=list)


class KnowledgeCoverage(BaseModel):
    """Everything known about one target's research facets, at one moment.

    Snapshot semantics: this is what the knowledge base held when the run
    looked, not a live view. Existing knowledge is a record of past
    sources (§15), and a coverage that says KNOWN is saying "CREA has
    established this from cited sources", never "this is current".
    """

    entity_id: str | None = None
    target: str = ""
    facets: list[FacetKnowledge] = Field(default_factory=list)
    checked_at: str = Field(default_factory=utc_now_iso)

    def state_of(self, facet: KnowledgeFacet) -> KnowledgeState:
        for item in self.facets:
            if item.facet is facet:
                return item.state
        return KnowledgeState.UNKNOWN

    def has_state(self, facet: KnowledgeFacet, state: KnowledgeState) -> bool:
        return self.state_of(facet) is state


class KnowledgeGap(BaseModel):
    """Something a complete answer would have that we do not.

    Gaps are facts about knowledge, not failures: "no experimental
    pressure recorded" is a legitimate research outcome to report. Every
    gap names its facet, says why it is a gap in one readable sentence,
    and points at the kind of work that would close it -- never at a
    fabricated value (§12/§16).
    """

    facet: KnowledgeFacet
    reason: str
    #: What would help: "search literature", "inspect local document", ...
    suggested_action: str = ""
    object_ids: list[str] = Field(default_factory=list)


class FieldObservation(BaseModel):
    """One field's value as one source reported it.

    Every observation carries its own provenance chain: which evidence,
    which paper, which locator (§24). `raw_value`/`raw_unit` stay exactly
    as written -- `80 °C` never becomes `353 K` here (§27).
    """

    field: str
    value: float | str | None = None
    raw_value: str = ""
    unit: str = ""
    raw_unit: str = ""
    evidence_id: str = ""
    paper_id: str = ""
    doi: str = ""
    source_id: str = ""
    reaction_id: str = ""
    record_id: str = ""


class FieldConflict(BaseModel):
    """Two sources reported different values for one field.

    `CONFLICT`, not `INVALID`: both observations survived validation --
    they simply disagree (§23). The conflict record keeps every value with
    its evidence so a reader can see both papers; nothing here averages,
    ranks or picks a winner.
    """

    field: str
    observations: list[FieldObservation] = Field(default_factory=list)
    kind: str = "conflict"
    note: str = ("两篇来源报告了不同的实验条件；每个值保留其证据与出处，"
                 "未做平均、未选择其中之一。")


class EvidenceSynthesis(BaseModel):
    """The recorded condition values for one body of reactions, side by side.

    An aggregation of what the records say -- never a new fact. Reading a
    value off this synthesis is reading it off a citation; there is no
    synthesis-level value that no source stated (§6).
    """

    entity_id: str | None = None
    reaction_ids: list[str] = Field(default_factory=list)
    observations: list[FieldObservation] = Field(default_factory=list)
    conflicts: list[FieldConflict] = Field(default_factory=list)
    generated_at: str = Field(default_factory=utc_now_iso)


class ResearchPlanAction(str, Enum):
    """What a plan step would have the run do. Tool-shaped, not magic."""

    ASSESS_KNOWLEDGE = "assess_knowledge"
    SEARCH_LITERATURE = "search_literature"
    INSPECT_DOCUMENT = "inspect_document"
    EXTRACT_REACTION_EVIDENCE = "extract_reaction_evidence"
    RETROSYNTHESIZE = "retrosynthesize"
    VALIDATE_ROUTES = "validate_routes"
    SYNTHESIZE_EVIDENCE = "synthesize_evidence"
    REPORT = "report"


class ResearchPlanStep(BaseModel):
    action: ResearchPlanAction
    #: Why this runs: usually the gap it addresses.
    reason: str = ""
    gap_facet: KnowledgeFacet | None = None
    #: The tool that implements it, when one exists.
    tool: str = ""


class ResearchPlan(BaseModel):
    """What this research run should do, and why.

    Derived deterministically from `KnowledgeCoverage` and its gaps (§18):
    no model invents the plan, and a plan step is a decision the workflow
    can audit -- "search literature because experimental evidence is
    UNKNOWN", not "search literature because that is what we always do".
    When the knowledge covers the question and the user did not ask for
    the latest literature, the plan says so and the search does not run
    (§39).
    """

    goal: str = ""
    coverage: KnowledgeCoverage | None = None
    gaps: list[KnowledgeGap] = Field(default_factory=list)
    steps: list[ResearchPlanStep] = Field(default_factory=list)
    #: Whether the user asked for the latest literature (§40). Detected
    #: deterministically from the question's own words, never assumed.
    wants_latest: bool = False

    def has(self, action: ResearchPlanAction) -> bool:
        return any(step.action is action for step in self.steps)


class KnowledgeReport(BaseModel):
    """What the report needs to say about knowledge (§100).

    `existing` is what the knowledge base already held when the run
    looked; `new` is what this run added. The kinds are stable keys
    ("entities", "reactions", "papers", "evidence", "routes",
    "reaction_records", "mentions"). A reader must be able to see both
    halves -- and the conflicts and gaps between them.
    """

    coverage: KnowledgeCoverage | None = None
    gaps: list[KnowledgeGap] = Field(default_factory=list)
    conflicts: list[FieldConflict] = Field(default_factory=list)
    existing: dict[str, list[str]] = Field(default_factory=dict)
    new: dict[str, list[str]] = Field(default_factory=dict)


__all__ = [
    "EvidenceSynthesis",
    "FacetKnowledge",
    "FieldConflict",
    "FieldObservation",
    "KnowledgeCoverage",
    "KnowledgeFacet",
    "KnowledgeGap",
    "KnowledgeReport",
    "KnowledgeState",
    "ResearchPlan",
    "ResearchPlanAction",
    "ResearchPlanStep",
]

"""M1.5 -- the study context: what one ResearchStudy knows, and how it knows it.

A study is the research boundary (§ Study as the Research Boundary). This
module is the vocabulary for what accumulates inside one study across
sessions: validated facts, AI hypotheses, evidence references, research
decisions, unresolved issues, and identity-candidate selections. Everything
is keyed by `study_id`; there is no global record here and no query that is
not scoped to one study.

The load-bearing separations, as models rather than conventions:

* `StudyFact` vs `StudyHypothesis`. A fact carries a `FactVerification` --
  tool-verified, user-stated, deterministically derived, or unverified.
  There is no verification value that means "the model said so": model text
  can only ever arrive as a hypothesis (`proposed`), and a hypothesis is
  never silently promoted (`SUPPORTED` requires named evidence).
* Evidence is *referenced*, never copied, and never converted. A reference
  to a literature `Evidence` stays a literature reference; nothing here
  constructs a `ReactionEvidence` or upgrades a search hit.
* `StudyDecision` is append-only: a later decision never overwrites an
  earlier one, and each keeps the observation kind and plan-change kinds
  that triggered it.
* `UnresolvedIssue` is queryable across sessions, so the next session can
  pick up exactly where the last one could not continue.
* `IdentitySelectionRecord` is the only path from a user's choice to
  `resolve_chemical_identity` arguments -- candidates are stored verbatim,
  the selection is the user's, and no selection is ever invented.

These models are new because the epistemic categories are new: the existing
`ResearchStudy` container (references/events/artifacts) has no place where
"a fact with a verification state" and "an AI hypothesis" can stay distinct.
Everything around them -- the study container, `Evidence`, `Provenance`,
`ChemicalEntity` -- is reused as-is.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.models.research_study import ResearchStudy
from crea.core.provenance import utc_now_iso


class FactVerification(str, Enum):
    """How a fact was established. There is no "model said so" value.

    `TOOL_VERIFIED` means a registered tool result established it under the
    M1.1 `sets_facts` / `fact_rule` declarations. `USER_STATED` is the user's
    own material. `DERIVED` is a deterministic computation. `UNVERIFIED` is
    recorded but must never be read as established.
    """

    TOOL_VERIFIED = "tool_verified"
    USER_STATED = "user_stated"
    DERIVED = "derived"
    UNVERIFIED = "unverified"


class StudyFact(BaseModel):
    """One established (or explicitly unestablished) fact inside one study.

    `payload` keeps the structured content the source gave; `evidence_ids`
    point at the `Evidence` records behind it; `provenance` is kept as the
    producing layer emitted it (the tool result's own provenance dict), so
    nothing is re-typed or re-worded on the way in.
    """

    fact_id: str
    study_id: str
    #: The session that established it, when one exists.
    session_id: str = ""
    #: What the fact says: "identity_resolved", "route_held", "structure" ...
    fact_type: str
    payload: dict = Field(default_factory=dict)
    summary: str = ""
    #: The tool (or "user" / "computation") that produced it.
    source: str = ""
    verification: FactVerification = FactVerification.UNVERIFIED
    provenance: dict | None = None
    #: The entity this fact is about, when it is about one.
    entity_id: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)


class HypothesisStatus(str, Enum):
    """Where a hypothesis stands. `proposed` never equals `supported`."""

    PROPOSED = "proposed"
    UNDER_REVIEW = "under_review"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    UNRESOLVED = "unresolved"


class StudyHypothesis(BaseModel):
    """An AI-proposed statement, kept as a hypothesis no matter how often it
    is repeated. Repetition is not evidence; only named evidence moves it."""

    hypothesis_id: str
    study_id: str
    session_id: str = ""
    content: str
    entity_id: str = ""
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    note: str = ""


class StudyEvidenceKind(str, Enum):
    """What kind of thing a referenced evidence record is.

    Stored beside the reference; the `Evidence`'s own `evidence_type` is
    never rewritten to match. A literature record referenced here is a
    literature reference forever.
    """

    LITERATURE = "literature"
    REACTION = "reaction"
    COMPUTED = "computed"
    USER_PROVIDED = "user_provided"
    OTHER = "other"


class StudyEvidenceRef(BaseModel):
    """A pointer at an existing `Evidence`, inside one study.

    References point; they never copy (§ the study container's own rule).
    The evidence object itself lives in the evidence store and keeps every
    field it was built with.
    """

    study_id: str
    evidence_id: str
    kind: StudyEvidenceKind
    session_id: str = ""
    note: str = ""
    added_at: str = Field(default_factory=utc_now_iso)


class StudyDecision(BaseModel):
    """One research decision, append-only.

    `observation_kind` is the M1.2 `ObservationKind` value that triggered it
    (empty for a start decision); `plan_change_kinds` are the M1.3
    `PlanChangeKind` values the decision carried. The decision text is a
    sentence a reader can check against those machine signals.
    """

    decision_id: str
    study_id: str
    session_id: str = ""
    decision: str
    reason: str = ""
    observation_kind: str = ""
    plan_change_kinds: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    hypothesis_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)


class IssueStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class UnresolvedIssue(BaseModel):
    """Something the research could not settle, kept for the next session.

    Identity ambiguities, missing experimental conditions, thin literature,
    unavailable tools, unvalidated routes and unanswered user questions all
    live here -- as issues with a status, never as silent gaps.
    """

    issue_id: str
    study_id: str
    session_id: str = ""
    #: "identity", "experimental_conditions", "literature", "tool_unavailable",
    #: "route", "user_question", "tool_error", "user_input" ...
    kind: str
    content: str
    status: IssueStatus = IssueStatus.OPEN
    entity_id: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    resolved_at: str = ""
    resolution: str = ""


class IdentityCandidate(BaseModel):
    """One candidate identity, kept exactly as the source described it.

    `kind`/`value` are filled only from declared fields (a candidate view's
    `canonical_smiles` or its name) -- never inferred. `raw` keeps the full
    candidate view so a reader can re-make the choice later.
    """

    kind: str = ""
    value: str = ""
    label: str = ""
    raw: dict = Field(default_factory=dict)


class IdentitySelectionRecord(BaseModel):
    """An ambiguity, its candidates, and (when made) the USER's selection.

    The only producer of `resolve_chemical_identity` selection arguments.
    A record without a selection stays a question; nothing here guesses an
    index, and `user_selection_statement` is the user's own words, required
    by the identity tool whenever an index is relayed.
    """

    record_id: str
    study_id: str
    session_id: str = ""
    question: str = ""
    candidates: list[IdentityCandidate] = Field(default_factory=list)
    selected_index: int | None = None
    #: Set only through the user-selection API; never "agent".
    selected_by: str = ""
    user_selection_statement: str = ""
    selected_at: str = ""
    created_at: str = Field(default_factory=utc_now_iso)

    def resolve_arguments(self) -> dict | None:
        """The exact `resolve_chemical_identity` arguments for this record.

        The selected candidate's declared kind/value plus the relay fields
        the tool requires. `None` while the question is open -- an unselected
        record produces no arguments at all.
        """
        if self.selected_index is None:
            return None
        if not (0 <= self.selected_index < len(self.candidates)):
            return None
        chosen = self.candidates[self.selected_index]
        if not chosen.kind or not chosen.value:
            return None
        return {
            "kind": chosen.kind,
            "value": chosen.value,
            "selected_candidate_index": self.selected_index,
            "user_selection_statement": self.user_selection_statement,
        }


class StudySessionSummary(BaseModel):
    """One finished (or paused) session's execution summary, for the next one.

    A bounded account of what ran and how it ended -- never the transcript
    itself, and never a replay of the previous session's messages.
    """

    session_id: str = ""
    status: str = ""
    outcome: str = ""
    started_at: str = Field(default_factory=utc_now_iso)
    ended_at: str = Field(default_factory=utc_now_iso)
    step_count: int = 0
    evidence_ids: list[str] = Field(default_factory=list)
    note: str = ""


class StudyContext(BaseModel):
    """Everything one study holds for its own continuation.

    Assembled by `StudyContextService.get_context`; every list is scoped to
    one `study_id`. There is no cross-study field, on purpose.
    """

    study: ResearchStudy
    #: The research goal (the study's question, plus any bound constraints).
    goal: str = ""
    constraints: list[str] = Field(default_factory=list)
    facts: list[StudyFact] = Field(default_factory=list)
    hypotheses: list[StudyHypothesis] = Field(default_factory=list)
    evidence_refs: list[StudyEvidenceRef] = Field(default_factory=list)
    decisions: list[StudyDecision] = Field(default_factory=list)
    #: Open issues by default; resolved ones only when asked for.
    issues: list[UnresolvedIssue] = Field(default_factory=list)
    identity_records: list[IdentitySelectionRecord] = Field(default_factory=list)
    #: Most recent first, bounded by the caller.
    session_summaries: list[StudySessionSummary] = Field(default_factory=list)
    loaded_at: str = Field(default_factory=utc_now_iso)


__all__ = [
    "FactVerification",
    "HypothesisStatus",
    "IdentityCandidate",
    "IdentitySelectionRecord",
    "IssueStatus",
    "StudyContext",
    "StudyDecision",
    "StudyEvidenceKind",
    "StudyEvidenceRef",
    "StudyFact",
    "StudyHypothesis",
    "StudySessionSummary",
    "UnresolvedIssue",
]

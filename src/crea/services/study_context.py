"""M1.5 -- the study context service: one study's memory, strictly scoped.

`StudyContextService` is the single write path for what a `ResearchStudy`
knows across sessions: tool-verified facts, AI hypotheses, evidence
references, research decisions, unresolved issues, and identity-candidate
selections. Agents and runtimes never assemble this from raw repositories --
they go through this service, because the boundaries (study isolation, fact
vs hypothesis, no silent promotion) are only real if exactly one place
enforces them.

The rules this service holds:

* **Study scope is absolute.** Every read and write names a `study_id`; a
  record of study A is unreachable while working on study B. There is no
  global search, by design (§ the blind-study isolation boundary).
* **Facts carry how they were established.** `FactVerification` has no
  "the model said so" value; `TOOL_VERIFIED` requires the tool that
  declared it. Model prose can only arrive as a hypothesis.
* **A hypothesis never promotes itself.** Every hypothesis starts
  `proposed`; moving it to `supported` requires named evidence ids. Saying
  the same thing twice changes nothing.
* **Evidence is referenced, never copied or converted.** A literature
  `Evidence` referenced here keeps its type forever; nothing in this module
  constructs a `ReactionEvidence` or upgrades a search hit.
* **Decisions are append-only.** `record_decision` never overwrites; the
  history is the record.
* **A selection belongs to the user.** `select_identity_candidate` requires
  the user's own words (`user_selection_statement`) because the identity
  tool does; the service never invents either.

Study lifecycle (create/status/references) stays with `ResearchStudyService`
-- this service attaches to a study that exists and never creates one.
"""

from __future__ import annotations

import time

from crea.core.errors import ResearchStudyError
from crea.core.ids import content_hash
from crea.core.models.research_study import ArtifactType
from crea.core.models.study_context import (
    FactVerification,
    HypothesisStatus,
    IdentityCandidate,
    IdentitySelectionRecord,
    IssueStatus,
    StudyContext,
    StudyDecision,
    StudyEvidenceKind,
    StudyEvidenceRef,
    StudyFact,
    StudyHypothesis,
    StudySessionSummary,
    UnresolvedIssue,
)
from crea.core.provenance import utc_now_iso


class StudyContextService:
    """Read and grow one study's context, through one door."""

    def __init__(self, kb) -> None:
        self._kb = kb

    # -- context assembly ---------------------------------------------------

    def open(self, study_id: str, *, goal: str = "",
             constraints: list[str] | None = None) -> StudyContext:
        """Bind (or re-bind) the context profile and return the context.

        The profile -- goal, constraints -- is a `STUDY_CONTEXT` artifact on
        the existing study container: a result that cannot be recomputed
        (§十七), not a new table. An empty `goal` falls back to the study's
        own question, which stays the canonical research goal (§二十七: the
        question is fixed at creation).
        """
        study = self._get_study(study_id)
        content = {
            "goal": (goal or "").strip() or study.question,
            "constraints": [c for c in (constraints or []) if c and c.strip()],
            "updated_at": utc_now_iso(),
        }
        self._kb.save_study_artifact(study_id, _artifact(
            study_id, ArtifactType.STUDY_CONTEXT, content,
            label="study context profile"))
        return self.get_context(study_id)

    def get_context(self, study_id: str, *, include_resolved_issues: bool = False,
                    decision_limit: int = 50,
                    session_limit: int = 5) -> StudyContext:
        """Everything this study holds, and nothing any other study holds."""
        study = self._get_study(study_id)
        profile = self._profile(study_id)
        issues = self._kb.list_unresolved_issues(study_id)
        if not include_resolved_issues:
            issues = [i for i in issues if i.status is IssueStatus.OPEN]
        decisions = self._kb.list_study_decisions(study_id)
        summaries = self._session_summaries(study_id)[:max(0, session_limit)]
        return StudyContext(
            study=study,
            goal=profile.get("goal") or study.question,
            constraints=list(profile.get("constraints") or []),
            facts=self._kb.list_study_facts(study_id),
            hypotheses=self._kb.list_study_hypotheses(study_id),
            evidence_refs=self._evidence_refs(study_id),
            decisions=decisions[-max(0, decision_limit):] if decision_limit
            else decisions,
            issues=issues,
            identity_records=self._kb.list_identity_records(study_id),
            session_summaries=summaries,
        )

    # -- facts --------------------------------------------------------------

    def add_fact(self, study_id: str, *, fact_type: str,
                 payload: dict | None = None, summary: str = "",
                 source: str = "", verification: FactVerification =
                 FactVerification.UNVERIFIED, provenance: dict | None = None,
                 entity_id: str = "", evidence_ids: list[str] | None = None,
                 session_id: str = "") -> StudyFact:
        """Record one fact, with how it was established.

        The default is `UNVERIFIED` on purpose: a caller that forgets the
        verification records an unestablished fact, never an established one.
        `TOOL_VERIFIED` names the tool that declared it (the M1.1
        `sets_facts` / `fact_rule` surface), so a fact can always be traced
        back to the call that produced it.
        """
        self._get_study(study_id)
        if not (fact_type or "").strip():
            raise ResearchStudyError(
                "INVALID_INPUT", "a fact needs a fact_type to be queryable")
        if verification is FactVerification.TOOL_VERIFIED and not source.strip():
            raise ResearchStudyError(
                "INVALID_INPUT",
                "a tool-verified fact must name the tool that verified it")
        fact = StudyFact(
            fact_id="fact-" + content_hash(
                f"{study_id}|{fact_type}|{session_id}|{utc_now_iso()}|"
                f"{time.time_ns()}", 16),
            study_id=study_id, session_id=session_id,
            fact_type=fact_type.strip(), payload=dict(payload or {}),
            summary=summary, source=source, verification=verification,
            provenance=dict(provenance) if provenance else None,
            entity_id=entity_id, evidence_ids=list(evidence_ids or []))
        return self._kb.save_study_fact(study_id, fact)

    def facts(self, study_id: str, *, fact_type: str = "",
              entity_id: str = "") -> list[StudyFact]:
        """Facts of one study, optionally narrowed by type or entity."""
        self._get_study(study_id)
        out = self._kb.list_study_facts(study_id)
        if fact_type:
            out = [f for f in out if f.fact_type == fact_type]
        if entity_id:
            out = [f for f in out if f.entity_id == entity_id]
        return out

    # -- hypotheses ---------------------------------------------------------

    def add_hypothesis(self, study_id: str, content: str, *, session_id: str = "",
                       entity_id: str = "", note: str = "") -> StudyHypothesis:
        """Record an AI-proposed statement -- always as `proposed`.

        There is no status parameter here: a new hypothesis is proposed no
        matter who vouches for it or how often it has been said.
        """
        self._get_study(study_id)
        text = (content or "").strip()
        if not text:
            raise ResearchStudyError(
                "INVALID_INPUT", "a hypothesis needs content")
        hypothesis = StudyHypothesis(
            hypothesis_id="hyp-" + content_hash(
                f"{study_id}|{session_id}|{text}|{time.time_ns()}", 16),
            study_id=study_id, session_id=session_id, content=text,
            entity_id=entity_id, note=note)
        return self._kb.save_study_hypothesis(study_id, hypothesis)

    def update_hypothesis(self, study_id: str, hypothesis_id: str,
                          status: HypothesisStatus, *, note: str = "",
                          supporting_evidence_ids: list[str] | None = None,
                          contradicting_evidence_ids: list[str] | None = None
                          ) -> StudyHypothesis:
        """Move a hypothesis along -- with evidence where evidence is owed.

        `SUPPORTED` requires at least one named supporting evidence id: a
        hypothesis does not become a fact-shaped thing because it stopped
        being questioned. `REFUTED` may rest on a user's judgement alone and
        says so in the note.
        """
        hypothesis = self._hypothesis(study_id, hypothesis_id)
        supporting = list(supporting_evidence_ids
                          if supporting_evidence_ids is not None
                          else hypothesis.supporting_evidence_ids)
        contradicting = list(contradicting_evidence_ids
                             if contradicting_evidence_ids is not None
                             else hypothesis.contradicting_evidence_ids)
        if (status is HypothesisStatus.SUPPORTED
                and not supporting and not hypothesis.supporting_evidence_ids):
            raise ResearchStudyError(
                "HYPOTHESIS_EVIDENCE_REQUIRED",
                "a hypothesis moves to 'supported' only with named supporting "
                "evidence ids; repetition and confidence are not evidence")
        updated = hypothesis.model_copy(update={
            "status": status,
            "supporting_evidence_ids": supporting,
            "contradicting_evidence_ids": contradicting,
            "updated_at": utc_now_iso(),
            "note": note if note else hypothesis.note,
        })
        return self._kb.save_study_hypothesis(study_id, updated)

    def hypotheses(self, study_id: str, *, status: HypothesisStatus | None = None,
                   entity_id: str = "") -> list[StudyHypothesis]:
        self._get_study(study_id)
        out = self._kb.list_study_hypotheses(study_id)
        if status is not None:
            out = [h for h in out if h.status is status]
        if entity_id:
            out = [h for h in out if h.entity_id == entity_id]
        return out

    # -- evidence references ------------------------------------------------

    def add_evidence_reference(self, study_id: str, evidence_id: str, *,
                               kind: StudyEvidenceKind,
                               session_id: str = "",
                               note: str = "") -> StudyEvidenceRef:
        """Point this study at an existing `Evidence`.

        The evidence must already exist (the session references objects and
        never creates them, §四十); the reference records what kind of thing
        it is *as the caller classified it* -- the `Evidence`'s own
        `evidence_type` is never rewritten, and no conversion between evidence
        kinds exists anywhere in this service.
        """
        self._get_study(study_id)
        try:
            self._kb.get_evidence(evidence_id)
        except Exception as exc:  # NotFound and nothing else is meaningful here
            raise ResearchStudyError(
                "EVIDENCE_NOT_FOUND",
                f"no evidence {evidence_id!r} in the knowledge base; a "
                f"context reference points at stored evidence and never "
                f"creates it") from exc
        ref = StudyEvidenceRef(
            study_id=study_id, evidence_id=evidence_id, kind=kind,
            session_id=session_id, note=note)
        refs = self._evidence_refs(study_id)
        if not any(r.evidence_id == evidence_id and r.kind is kind
                   for r in refs):
            self._kb.save_study_artifact(study_id, _artifact(
                study_id, ArtifactType.STUDY_EVIDENCE_REF,
                ref.model_dump(mode="json"), label="evidence reference"))
        return ref

    def evidence_refs(self, study_id: str,
                      kind: StudyEvidenceKind | None = None
                      ) -> list[StudyEvidenceRef]:
        self._get_study(study_id)
        refs = self._evidence_refs(study_id)
        if kind is not None:
            refs = [r for r in refs if r.kind is kind]
        return refs

    # -- decisions ----------------------------------------------------------

    def record_decision(self, study_id: str, decision: str, *,
                        reason: str = "", session_id: str = "",
                        observation_kind: str = "",
                        plan_change_kinds: list[str] | None = None,
                        entity_ids: list[str] | None = None,
                        evidence_ids: list[str] | None = None,
                        hypothesis_ids: list[str] | None = None
                        ) -> StudyDecision:
        """Append one research decision. Nothing here ever overwrites."""
        self._get_study(study_id)
        text = (decision or "").strip()
        if not text:
            raise ResearchStudyError(
                "INVALID_INPUT", "a decision needs its content")
        record = StudyDecision(
            decision_id="dec-" + content_hash(
                f"{study_id}|{session_id}|{text}|{time.time_ns()}", 16),
            study_id=study_id, session_id=session_id, decision=text,
            reason=reason, observation_kind=observation_kind,
            plan_change_kinds=list(plan_change_kinds or []),
            entity_ids=list(entity_ids or []),
            evidence_ids=list(evidence_ids or []),
            hypothesis_ids=list(hypothesis_ids or []))
        return self._kb.save_study_decision(study_id, record)

    def decisions(self, study_id: str) -> list[StudyDecision]:
        """Every decision of this study, oldest first -- the full history."""
        self._get_study(study_id)
        return self._kb.list_study_decisions(study_id)

    # -- unresolved issues --------------------------------------------------

    def record_unresolved_issue(self, study_id: str, content: str, *,
                                kind: str, session_id: str = "",
                                entity_id: str = "",
                                deduplicate: bool = True) -> UnresolvedIssue:
        """Record something the research could not settle.

        With `deduplicate` (the default), an open issue with the same kind
        and content is returned instead of a second copy -- a tool that was
        unavailable in three consecutive runs is one issue, not three.
        """
        self._get_study(study_id)
        text = (content or "").strip()
        if not text or not (kind or "").strip():
            raise ResearchStudyError(
                "INVALID_INPUT", "an issue needs a kind and content")
        if deduplicate:
            for existing in self._kb.list_unresolved_issues(study_id):
                if (existing.status is IssueStatus.OPEN
                        and existing.kind == kind
                        and existing.content == text):
                    return existing
        issue = UnresolvedIssue(
            issue_id="issue-" + content_hash(
                f"{study_id}|{kind}|{text}|{time.time_ns()}", 16),
            study_id=study_id, session_id=session_id, kind=kind, content=text,
            entity_id=entity_id)
        return self._kb.save_unresolved_issue(study_id, issue)

    def resolve_unresolved_issue(self, study_id: str, issue_id: str,
                                 resolution: str, *,
                                 session_id: str = "") -> UnresolvedIssue:
        """Close an issue with what settled it. The issue row is kept."""
        issue = self._issue(study_id, issue_id)
        if issue.status is IssueStatus.RESOLVED:
            return issue
        updated = issue.model_copy(update={
            "status": IssueStatus.RESOLVED,
            "resolved_at": utc_now_iso(),
            "resolution": (resolution or "").strip(),
            "session_id": session_id or issue.session_id,
        })
        return self._kb.update_unresolved_issue(study_id, updated)

    def open_issues(self, study_id: str) -> list[UnresolvedIssue]:
        """The issues the next session should pick up."""
        self._get_study(study_id)
        return [i for i in self._kb.list_unresolved_issues(study_id)
                if i.status is IssueStatus.OPEN]

    # -- identity candidates and user selection -----------------------------

    def save_identity_candidates(self, study_id: str, *,
                                 question: str,
                                 candidates: list[IdentityCandidate],
                                 session_id: str = "") -> IdentitySelectionRecord:
        """Store an ambiguity with its candidates, as a question.

        No selection is made here. The candidates keep whatever their source
        declared (`raw` holds the full view); `kind`/`value` are the
        structure or name the source itself stated, never inferred.
        """
        self._get_study(study_id)
        if not candidates:
            raise ResearchStudyError(
                "INVALID_INPUT", "an identity record needs candidates")
        record = IdentitySelectionRecord(
            record_id="idrec-" + content_hash(
                f"{study_id}|{session_id}|{question}|{time.time_ns()}", 16),
            study_id=study_id, session_id=session_id, question=question,
            candidates=list(candidates))
        return self._kb.save_identity_record(study_id, record)

    def select_identity_candidate(self, study_id: str, record_id: str,
                                  selected_index: int, *,
                                  user_selection_statement: str,
                                  session_id: str = "") -> IdentitySelectionRecord:
        """Record THE USER's choice among stored candidates.

        `user_selection_statement` is the user's own words, quoted -- the
        identity tool requires them whenever an index is relayed, because a
        selection the system invented is indistinguishable from one it
        relays unless the relay leaves a trace. The service never writes
        this string itself.
        """
        record = self._identity_record(study_id, record_id)
        if not (0 <= selected_index < len(record.candidates)):
            raise ResearchStudyError(
                "IDENTITY_INDEX_INVALID",
                f"candidate {selected_index} does not exist; the record has "
                f"{len(record.candidates)} candidates")
        statement = (user_selection_statement or "").strip()
        if not statement:
            raise ResearchStudyError(
                "IDENTITY_SELECTION_STATEMENT_REQUIRED",
                "a selection is only real if the user made it; record what "
                "they said (user_selection_statement) or leave the record open")
        updated = record.model_copy(update={
            "selected_index": selected_index,
            "selected_by": "user",
            "user_selection_statement": statement,
            "selected_at": utc_now_iso(),
            "session_id": session_id or record.session_id,
        })
        return self._kb.save_identity_record(study_id, updated)

    def identity_resolve_arguments(self, study_id: str,
                                   record_id: str) -> dict:
        """The exact `resolve_chemical_identity` arguments for the user's
        choice. Raises while the record is open: an unselected record
        produces no arguments, ever."""
        record = self._identity_record(study_id, record_id)
        arguments = record.resolve_arguments()
        if arguments is None:
            raise ResearchStudyError(
                "IDENTITY_SELECTION_REQUIRED",
                "this identity record has no user selection; candidates are "
                "a question, not an answer")
        return arguments

    def identity_records(self, study_id: str) -> list[IdentitySelectionRecord]:
        self._get_study(study_id)
        return self._kb.list_identity_records(study_id)

    # -- session summaries --------------------------------------------------

    def record_session_summary(self, study_id: str, summary: StudySessionSummary
                               ) -> StudySessionSummary:
        """Store one session's bounded execution summary.

        The next session reads this instead of replaying the last one: what
        ran, how it ended, which evidence came back -- never the messages.
        """
        self._get_study(study_id)
        self._kb.save_study_artifact(study_id, _artifact(
            study_id, ArtifactType.SESSION_SUMMARY,
            summary.model_dump(mode="json"),
            label=f"session {summary.session_id}"))
        return summary

    # -- helpers ------------------------------------------------------------

    def _get_study(self, study_id: str):
        study = self._kb.get_study(study_id)
        if study is None:
            raise ResearchStudyError(
                "STUDY_NOT_FOUND", f"no research session {study_id!r}")
        return study

    def _hypothesis(self, study_id: str, hypothesis_id: str) -> StudyHypothesis:
        for hypothesis in self._kb.list_study_hypotheses(study_id):
            if hypothesis.hypothesis_id == hypothesis_id:
                return hypothesis
        raise ResearchStudyError(
            "CONTEXT_RECORD_NOT_FOUND",
            f"no hypothesis {hypothesis_id!r} in study {study_id!r}")

    def _issue(self, study_id: str, issue_id: str) -> UnresolvedIssue:
        for issue in self._kb.list_unresolved_issues(study_id):
            if issue.issue_id == issue_id:
                return issue
        raise ResearchStudyError(
            "CONTEXT_RECORD_NOT_FOUND",
            f"no issue {issue_id!r} in study {study_id!r}")

    def _identity_record(self, study_id: str,
                         record_id: str) -> IdentitySelectionRecord:
        for record in self._kb.list_identity_records(study_id):
            if record.record_id == record_id:
                return record
        raise ResearchStudyError(
            "CONTEXT_RECORD_NOT_FOUND",
            f"no identity record {record_id!r} in study {study_id!r}")

    def _profile(self, study_id: str) -> dict:
        for artifact in self._kb.list_study_artifacts(study_id):
            if artifact.artifact_type is ArtifactType.STUDY_CONTEXT:
                content = artifact.content or {}
                if "goal" in content or "constraints" in content:
                    return content
        return {}

    def _evidence_refs(self, study_id: str) -> list[StudyEvidenceRef]:
        refs: list[StudyEvidenceRef] = []
        seen: set[tuple] = set()
        for artifact in self._kb.list_study_artifacts(study_id):
            if artifact.artifact_type is not ArtifactType.STUDY_EVIDENCE_REF:
                continue
            try:
                ref = StudyEvidenceRef(**(artifact.content or {}))
            except Exception:  # noqa: BLE001 - a foreign artifact is not a ref
                continue
            key = (ref.evidence_id, ref.kind.value)
            if key in seen:
                continue
            seen.add(key)
            refs.append(ref)
        return refs

    def _session_summaries(self, study_id: str) -> list[StudySessionSummary]:
        """Most recent session summaries first (bounded by the caller)."""
        found: list[StudySessionSummary] = []
        for artifact in self._kb.list_study_artifacts(study_id):
            if artifact.artifact_type is not ArtifactType.SESSION_SUMMARY:
                continue
            try:
                found.append(StudySessionSummary(**(artifact.content or {})))
            except Exception:  # noqa: BLE001 - a foreign artifact is not one
                continue
        found.sort(key=lambda s: s.ended_at, reverse=True)
        return found


def _artifact(study_id: str, artifact_type: ArtifactType, content: dict,
              label: str = ""):
    from crea.core.models.research_study import StudyArtifact

    return StudyArtifact(
        artifact_id="art-" + content_hash(
            f"{study_id}|{artifact_type.value}|{utc_now_iso()}|"
            f"{time.time_ns()}", 16),
        artifact_type=artifact_type, label=label, content=content)


__all__ = ["StudyContextService"]

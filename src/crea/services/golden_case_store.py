"""M1.7.2 -- GoldenCaseStore: the one write path for evaluation cases.

Case lifecycle, references, traces, evaluations and unseal history all
go through this service, because the rules that keep an evaluation
auditable are only real if exactly one place enforces them:

* **case_id is identity, and identity is not overwritten.** `create`
  refuses an existing case_id (`CASE_EXISTS`); updating requires the
  caller to have read the current version (`CASE_VERSION_CONFLICT`
  otherwise) and to bump `case_version` -- content edits and the version
  number move together, and `schema_version` never moves because a case
  was edited.
* **A trace freezes the version it ran under.** Saving a trace does not
  check the case's current version: history keeps the version it saw.
  Evaluations are stricter -- a verdict must name the case version its
  trace actually ran (`EVALUATION_VERSION_MISMATCH` otherwise), so a
  later case edit can never silently re-label an old verdict.
* **References belong to exactly one case.** Every reference row is
  keyed by (case_id, reference_id); there is no global reference pool
  and no cross-case query.
* **Unsealing changes only what unseal is allowed to change.** Ordinary
  reference updates preserve the stored `hidden` state -- the only path
  that flips it is the unseal service, which writes an audit event.
* **The agent path never sees a reference.** `get_agent_view` returns a
  `GoldenCaseAgentView` built by projection; the reference rows are
  never loaded into it. `get_manager_view` is the deliberate, separate
  door for evaluation tooling.

What this service does NOT do: content retrieval (that is
`reference_content`, with its own path-safety rules), unsealing (that is
`golden_case_unseal`, so every event is audited), and running research
(that is the blind runner). Thin doors, one rule each.
"""

from __future__ import annotations

import time

from crea.core.errors import GoldenCaseError
from crea.core.ids import content_hash
from crea.core.models.golden_case import (
    GOLDEN_CASE_SCHEMA_VERSION,
    EvaluationResult,
    GoldenCase,
    GoldenCaseStatus,
    GoldenCaseType,
    ReferenceMaterial,
    ResearchTrace,
)
from crea.core.models.golden_case_access import (
    GoldenCaseAgentView,
    GoldenCaseManagerView,
    UnsealEvent,
)
from crea.core.provenance import utc_now_iso
from crea.repositories.errors import Duplicate, NotFound, RepositoryError


class GoldenCaseStore:
    """Persistent evaluation cases, behind one door."""

    def __init__(self, kb) -> None:
        self._kb = kb

    # -- cases --------------------------------------------------------------

    def create(self, case: GoldenCase) -> GoldenCase:
        """Store a new case. An existing case_id is an error, never a
        silent overwrite: two different cases must not share a slot."""
        if case.schema_version != GOLDEN_CASE_SCHEMA_VERSION:
            raise GoldenCaseError(
                "SCHEMA_MISMATCH",
                f"case {case.case_id!r} declares schema "
                f"{case.schema_version!r}; this build speaks "
                f"{GOLDEN_CASE_SCHEMA_VERSION!r}")
        try:
            self._kb.save_golden_case(case)
        except Duplicate as exc:
            raise GoldenCaseError(
                "CASE_EXISTS",
                f"golden case {case.case_id!r} already exists; create "
                f"refuses to overwrite it") from exc
        return case

    def get(self, case_id: str) -> GoldenCase:
        try:
            case = self._kb.get_golden_case(case_id)
        except RepositoryError as exc:
            raise GoldenCaseError(
                "CASE_RECORD_CORRUPT",
                f"golden case {case_id!r} is stored but not "
                f"reconstructable: {exc}") from exc
        if case is None:
            raise GoldenCaseError("CASE_NOT_FOUND",
                                  f"no golden case {case_id!r}")
        return case

    def update(self, case: GoldenCase, *,
               expected_version: str | None = None) -> GoldenCase:
        """Update a case's content, with explicit version handling.

        The caller must state the version it read (`expected_version`,
        when given): if the stored case has moved on, the update is
        refused rather than clobbering someone else's edit. A content
        update must bump `case_version` -- editing content under the
        same version number would make "version 3" mean two things.
        `updated_at` is stamped by the service, not trusted from the
        caller.
        """
        current = self.get(case.case_id)
        if expected_version is not None \
                and current.case_version != expected_version:
            raise GoldenCaseError(
                "CASE_VERSION_CONFLICT",
                f"golden case {case.case_id!r} is at version "
                f"{current.case_version!r}, not the expected "
                f"{expected_version!r}; re-read before updating")
        if case.case_version == current.case_version:
            raise GoldenCaseError(
                "CASE_VERSION_CONFLICT",
                f"content update must bump case_version "
                f"(still {case.case_version!r}); a version number may "
                f"not mean two different contents")
        if case.schema_version != current.schema_version:
            raise GoldenCaseError(
                "SCHEMA_MISMATCH",
                f"schema_version may not change through an update "
                f"({current.schema_version!r} -> {case.schema_version!r}); "
                f"a schema change is a migration, not an edit")
        stamped = case.model_copy(update={"updated_at": utc_now_iso()})
        try:
            self._kb.update_golden_case(stamped)
        except NotFound as exc:
            raise GoldenCaseError("CASE_NOT_FOUND",
                                  f"no golden case {case.case_id!r}") from exc
        return stamped

    def set_status(self, case_id: str, status: GoldenCaseStatus) -> GoldenCase:
        """Move a case through its lifecycle without touching content.

        Status is not content: archiving a case does not bump
        `case_version`, because nothing the run would see has changed.
        ARCHIVED is terminal -- a finished archive stays finished.
        """
        case = self.get(case_id)
        if case.status is GoldenCaseStatus.ARCHIVED:
            raise GoldenCaseError(
                "CASE_ARCHIVED",
                f"golden case {case_id!r} is archived; archived cases "
                f"are terminal")
        stamped = case.model_copy(update={"status": status,
                                          "updated_at": utc_now_iso()})
        self._kb.update_golden_case(stamped)
        return stamped

    def list_cases(self, *, case_type: GoldenCaseType | None = None,
                   status: GoldenCaseStatus | None = None
                   ) -> list[GoldenCase]:
        return self._kb.list_golden_cases(case_type=case_type, status=status)

    def get_version(self, case_id: str) -> str:
        version = self._kb.get_golden_case_version(case_id)
        if version is None:
            raise GoldenCaseError("CASE_NOT_FOUND",
                                  f"no golden case {case_id!r}")
        return version

    # -- references ---------------------------------------------------------

    def save_reference(self, case_id: str,
                       material: ReferenceMaterial) -> ReferenceMaterial:
        """Attach (or update) one reference of one case.

        An update preserves the stored `hidden` state: metadata edits
        never unseal, and re-sealing a sealed reference through the back
        door is equally impossible. The one privileged writer of the
        hidden flag is `_set_reference_sealed_state`, whose only caller
        is the unseal service -- and that path always writes an audit
        event beside the change.
        """
        self.get(case_id)  # the case must exist
        stored = self._kb.get_golden_case_reference(case_id,
                                                    material.reference_id)
        if stored is not None and stored.hidden != material.hidden:
            material = material.model_copy(update={"hidden": stored.hidden})
        return self._kb.save_golden_case_reference(case_id, material)

    def set_reference_sealed_state(self, case_id: str,
                                   material: ReferenceMaterial
                                   ) -> ReferenceMaterial:
        """Write a reference with a changed `hidden` flag.

        The unseal service's write path, and its alone: `save_reference`
        will not carry a hidden change, so nothing else can open (or
        re-seal) a reference by editing metadata. Every caller of this
        method must write the matching `UnsealEvent` -- the audit trail
        is the point of having a privileged path at all.
        """
        self.get(case_id)  # the case must exist
        return self._kb.save_golden_case_reference(case_id, material)

    def save_unseal_event(self, event: UnsealEvent) -> UnsealEvent:
        """Append one audit event. Persistence only -- recording who
        opened what never opens anything itself."""
        return self._kb.save_unseal_event(event)

    def get_reference(self, case_id: str, reference_id: str
                      ) -> ReferenceMaterial:
        material = self._kb.get_golden_case_reference(case_id, reference_id)
        if material is None:
            raise GoldenCaseError(
                "REFERENCE_NOT_FOUND",
                f"case {case_id!r} has no reference {reference_id!r}")
        return material

    def list_references(self, case_id: str) -> list[ReferenceMaterial]:
        self.get(case_id)
        return self._kb.list_golden_case_references(case_id)

    # -- traces and evaluations ---------------------------------------------

    def save_trace(self, trace: ResearchTrace) -> ResearchTrace:
        """Store a run's trace. The trace keeps the case_version it ran
        under -- a later case edit never rewrites history."""
        self.get(trace.case_id)  # the case must exist
        return self._kb.save_golden_case_trace(trace)

    def get_trace(self, trace_id: str) -> ResearchTrace:
        trace = self._kb.get_golden_case_trace(trace_id)
        if trace is None:
            raise GoldenCaseError("TRACE_NOT_FOUND",
                                  f"no trace {trace_id!r}")
        return trace

    def list_traces(self, case_id: str) -> list[ResearchTrace]:
        self.get(case_id)
        return self._kb.list_golden_case_traces(case_id)

    def save_evaluation(self, result: EvaluationResult) -> EvaluationResult:
        """Store a verdict, after checking it judges what it says.

        The named trace must exist and belong to the named case, and the
        evaluation must judge the exact case_version that trace ran
        under. A verdict pinned to a different version cannot say
        anything honest about that run, so it is refused.
        """
        self.get(result.case_id)
        trace = self.get_trace(result.trace_id)
        if trace.case_id != result.case_id:
            raise GoldenCaseError(
                "TRACE_CASE_MISMATCH",
                f"trace {result.trace_id!r} belongs to case "
                f"{trace.case_id!r}, not {result.case_id!r}")
        if trace.case_version != result.case_version:
            raise GoldenCaseError(
                "EVALUATION_VERSION_MISMATCH",
                f"trace {result.trace_id!r} ran case version "
                f"{trace.case_version!r}; an evaluation of that run "
                f"must judge version {trace.case_version!r}, not "
                f"{result.case_version!r}")
        return self._kb.save_golden_case_evaluation(result)

    def get_evaluation(self, evaluation_id: str) -> EvaluationResult:
        result = self._kb.get_golden_case_evaluation(evaluation_id)
        if result is None:
            raise GoldenCaseError("EVALUATION_NOT_FOUND",
                                  f"no evaluation {evaluation_id!r}")
        return result

    def list_evaluations(self, case_id: str) -> list[EvaluationResult]:
        self.get(case_id)
        return self._kb.list_golden_case_evaluations(case_id)

    # -- unseal history (read side; writing is the unseal service) ---------

    def list_unseal_events(self, case_id: str, *,
                           reference_id: str | None = None
                           ) -> list[UnsealEvent]:
        self.get(case_id)
        return self._kb.list_unseal_events(case_id, reference_id=reference_id)

    # -- the two views ------------------------------------------------------

    def get_agent_view(self, case_id: str) -> GoldenCaseAgentView:
        """The blind-run projection. References are never loaded: the
        view is built from the case document alone, so there is no
        window in which a reference could leak into it."""
        case = self.get(case_id)
        if case.blind_input is None:
            raise GoldenCaseError(
                "CASE_INPUT_MISSING",
                f"golden case {case_id!r} has no BlindInput; a case "
                f"cannot be run blind without its visible half")
        return GoldenCaseAgentView(
            case_id=case.case_id,
            case_type=case.case_type,
            case_version=case.case_version,
            schema_version=case.schema_version,
            status=case.status,
            blind_input=case.blind_input,
        )

    def get_manager_view(self, case_id: str) -> GoldenCaseManagerView:
        """The full picture for evaluation tooling: case, reference
        metadata, traces, evaluations, unseal history. Reading this
        unseals nothing -- `hidden` is a field of what comes back, not
        an action taken."""
        case = self.get(case_id)
        return GoldenCaseManagerView(
            case=case,
            references=self._kb.list_golden_case_references(case_id),
            traces=self._kb.list_golden_case_traces(case_id),
            evaluations=self._kb.list_golden_case_evaluations(case_id),
            unseal_events=self._kb.list_unseal_events(case_id),
        )

    # -- id helpers ---------------------------------------------------------

    @staticmethod
    def new_case_id(key: str) -> str:
        """A deterministic case id for a (type, name) slot -- the same
        helper `GoldenCase.build` uses, exposed so a caller can compute
        the id before constructing the case."""
        return "case-" + content_hash(key, 16)

    @staticmethod
    def new_event_id(case_id: str, reference_id: str, at: str) -> str:
        """A distinct id per unseal attempt: same case, same reference,
        different attempt -> different event. The nanosecond clock is the
        discriminator (`utc_now_iso` only resolves seconds, and two
        attempts inside one second must still be two records), so
        re-unsealing is a new event rather than a rewrite."""
        return "unseal-" + content_hash(
            f"{case_id}|{reference_id}|{at}|{time.time_ns()}", 12)


__all__ = ["GoldenCaseStore"]

"""M1.7.2 -- explicit unseal: the only path that opens a reference.

Blind evaluation means the agent finishes before the expert material is
opened. That makes unsealing a deliberate, named, audited act:

* A request names the case, the references (or all of them), **a person**
  and **a reason** -- both required by the model, so an audit trail that
  says nothing cannot be constructed.
* Every attempt is an append-only `UnsealEvent`: unsealed, already
  unsealed, or failed -- with case id, case version at attempt time,
  reference id, before/after hidden state and failure diagnostics.
  Nothing rewrites history; a repeat unseal is a new event, not an edit.
* Reading the manager view unseals nothing. Saving an evaluation
  unseals nothing. The store's `save_reference` preserves `hidden`.
  This module is the only caller of the store's privileged
  `set_reference_sealed_state`, and every state change it makes is
  written beside its audit event.

The trust boundary, stated plainly: whoever can call this service can
open references. There is no user-auth layer in this project; the
guarantee is that they must leave a name and a reason behind, and that
no *other* path opens anything. Process-level access control belongs to
a future authenticated deployment, not to this infrastructure.
"""

from __future__ import annotations

from crea.core.errors import GoldenCaseError
from crea.core.models.golden_case import GoldenCase, ReferenceMaterial
from crea.core.models.golden_case_access import (
    UnsealEvent,
    UnsealOutcome,
    UnsealRequest,
)
from crea.core.provenance import utc_now_iso
from crea.services.golden_case_store import GoldenCaseStore


class GoldenCaseUnsealService:
    """Open references on request, and leave a trail."""

    def __init__(self, store: GoldenCaseStore) -> None:
        self._store = store

    def unseal(self, request: UnsealRequest) -> list[UnsealEvent]:
        """Unseal the requested references; return one event per attempt.

        The request is validated before anything opens: the case must
        exist and every named reference must belong to it, or a
        REFERENCE_NOT_FOUND is raised -- with a FAILED event already
        appended for each missing id, so even a typo leaves history. A
        repeat unseal of an open reference is legal and recorded as
        ALREADY_UNSEALED: loud, never silent, never a rewrite.
        """
        case = self._store.get(request.case_id)
        targets = self._resolve_targets(case, request)

        events: list[UnsealEvent] = []
        for material in targets:
            event = self._unseal_one(case, material, request)
            self._store.save_unseal_event(event)
            events.append(event)
        return events

    def unseal_all(self, case_id: str, *, actor: str, reason: str
                   ) -> list[UnsealEvent]:
        """Convenience: unseal every reference of a case, named and
        reasoned exactly like any other request."""
        return self.unseal(UnsealRequest(case_id=case_id, actor=actor,
                                         reason=reason))

    # -- internals ----------------------------------------------------------

    def _resolve_targets(self, case: GoldenCase,
                         request: UnsealRequest) -> list[ReferenceMaterial]:
        """The references a request names, each verified to exist.

        An empty list means every reference of the case -- still an
        explicit choice (the request says "no filter"), and still
        audited per reference.
        """
        if not request.reference_ids:
            references = self._store.list_references(case.case_id)
            if not references:
                raise GoldenCaseError(
                    "REFERENCE_NOT_FOUND",
                    f"golden case {case.case_id!r} has no references "
                    f"to unseal")
            return references
        out: list[ReferenceMaterial] = []
        missing: list[str] = []
        for reference_id in request.reference_ids:
            try:
                out.append(self._store.get_reference(case.case_id,
                                                     reference_id))
            except GoldenCaseError:
                missing.append(reference_id)
        if missing:
            # A refused attempt is still history: record it, then refuse.
            at = utc_now_iso()
            for reference_id in missing:
                self._store.save_unseal_event(UnsealEvent(
                    event_id=GoldenCaseStore.new_event_id(
                        case.case_id, reference_id, at),
                    case_id=case.case_id,
                    case_version=case.case_version,
                    reference_id=reference_id,
                    outcome=UnsealOutcome.FAILED,
                    actor=request.actor,
                    reason=request.reason,
                    previous_hidden=True,
                    new_hidden=True,
                    diagnostics=f"case {case.case_id!r} has no reference "
                                f"{reference_id!r}",
                    at=at,
                ))
            raise GoldenCaseError(
                "REFERENCE_NOT_FOUND",
                f"case {case.case_id!r} has no reference(s): "
                f"{', '.join(missing)}")
        return out

    def _unseal_one(self, case: GoldenCase, material: ReferenceMaterial,
                    request: UnsealRequest) -> UnsealEvent:
        at = utc_now_iso()
        event = UnsealEvent(
            event_id=GoldenCaseStore.new_event_id(case.case_id,
                                                  material.reference_id, at),
            case_id=case.case_id,
            case_version=case.case_version,
            reference_id=material.reference_id,
            outcome=UnsealOutcome.UNSEALED,
            actor=request.actor,
            reason=request.reason,
            previous_hidden=material.hidden,
            new_hidden=False,
            at=at,
        )
        if not material.hidden:
            # Legal, recorded, and a no-op: nothing is rewritten.
            event.outcome = UnsealOutcome.ALREADY_UNSEALED
            return event
        opened = material.model_copy(update={"hidden": False})
        self._store.set_reference_sealed_state(case.case_id, opened)
        return event


__all__ = ["GoldenCaseUnsealService"]

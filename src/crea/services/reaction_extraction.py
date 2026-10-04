"""Experimental text -> a validated ReactionRecord.

    Evidence ──► model proposes ──► we check ──► ReactionRecord
                     (may say anything)

The model is never the source of a fact. It is the only thing that reads
prose, and everything it says is checked against the Evidence it was given
before any of it reaches a record: the cited ids must exist, the numbers must
appear in the quoted text, a yield outside 0-100 is refused rather than
repaired, and two passages that disagree produce a conflict instead of a
silent winner.

`extract` accepts a pre-built `proposal`. That path exists so the validation --
which is where the safety lives -- can be tested without a model, and so a
caller with its own extractor can reuse the checks unchanged.
"""

from __future__ import annotations

import json

from crea.agent.reaction_prompt import EXTRACTION_SYSTEM, build_user_message
from crea.core.models.evidence import Evidence
from crea.core.models.reaction import (
    ComponentRole,
    ExtractionOutcome,
    ExtractionProposal,
    ExtractionRejection,
    FieldStatus,
    ReactionConditions,
    ReactionExtractionResult,
    ReactionRecord,
    reaction_identity,
)
from crea.core.policy.reaction import (
    POLICY_VERSION,
    detect_conflicts,
    rejection,
    validate_component,
    validate_quantity,
    validate_yield_type,
)
from crea.llm.base import LLMProvider, LLMRequest

EXTRACTOR_VERSION = "reaction-extraction-v1"


class ReactionExtractor:
    """Proposes, then disposes."""

    def __init__(self, llm: LLMProvider | None = None) -> None:
        self._llm = llm

    # -- input handling -----------------------------------------------------

    @staticmethod
    def _index(evidence: list[Evidence]) -> dict[str, Evidence]:
        return {e.evidence_id: e for e in evidence}

    @staticmethod
    def _quotes_for(refs: list[str], index: dict[str, Evidence]) -> str:
        return "\n".join(index[r].raw_quote for r in refs if r in index)

    def _resolve_refs(self, refs, index, field, rejected, all_refs):
        """Keep only refs this extraction was actually given."""
        known = [r for r in (refs or []) if r in index]
        for r in (refs or []):
            if r not in index:
                rejected.append(rejection(
                    field,
                    f"{field}: evidence id {r!r} was not supplied to this "
                    f"extraction; a field cannot cite something it was not shown"))
        all_refs.update(known)
        return known

    # -- the model ----------------------------------------------------------

    def propose(self, evidence: list[Evidence]) -> ExtractionProposal | None:
        """Ask the model. Returns None when there is no usable answer."""
        if self._llm is None:
            return None
        response = self._llm.complete(LLMRequest(
            system=EXTRACTION_SYSTEM,
            messages=[{"role": "user",
                       "content": build_user_message(evidence)}],
        ))
        payload = None
        if isinstance(response.action, dict):
            payload = response.action.get("extraction")
        if payload is None:
            text = (response.text or "").strip()
            if text.startswith("{"):
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = None
        if not isinstance(payload, dict):
            return None
        try:
            return ExtractionProposal.model_validate(payload)
        except Exception:  # noqa: BLE001 - a malformed proposal is no proposal
            return None

    # -- the whole thing ----------------------------------------------------

    def extract(
        self,
        evidence: list[Evidence],
        document_id: str | None = None,
        proposal: ExtractionProposal | None = None,
        source: str = "document",
    ) -> ReactionExtractionResult:
        index = self._index(evidence)
        result = ReactionExtractionResult(
            extractor=(f"{EXTRACTOR_VERSION}:"
                       f"{self._llm.name}" if self._llm else f"{EXTRACTOR_VERSION}:proposal"),
            policy_version=POLICY_VERSION,
        )

        if not evidence:
            result.outcome = ExtractionOutcome.REJECTED
            result.notes.append("no experimental evidence was supplied")
            return result

        if proposal is None:
            proposal = self.propose(evidence)
        if proposal is None:
            result.outcome = ExtractionOutcome.REJECTED
            result.notes.append(
                "the extractor returned nothing usable; no record was created "
                "rather than an empty one"
            )
            return result

        rejected: list[ExtractionRejection] = []
        conditions = ReactionConditions()
        all_refs: set[str] = set()
        conflict_inputs: list[tuple[str, str, str]] = []

        desc_refs = self._resolve_refs(
            proposal.description_evidence_refs, index,
            "description", rejected, all_refs)

        # -- quantitative conditions
        for proposal_field in proposal.conditions:
            name = (proposal_field.name or "").strip().lower()
            if name not in ("temperature", "time", "pressure",
                            "concentration", "yield_value"):
                rejected.append(rejection(
                    name or "condition",
                    f"unknown condition {name!r}", proposal_field))
                continue
            refs = self._resolve_refs(
                proposal_field.evidence_refs, index, name, rejected, all_refs)
            if not refs and proposal_field.status is not FieldStatus.NOT_FOUND:
                rejected.append(rejection(
                    name,
                    f"{name}: stated without any evidence, so not recorded",
                    proposal_field))
                continue
            quotes = self._quotes_for(refs, index)
            quantity, problem = validate_quantity(name, proposal_field, quotes)
            if problem or quantity is None:
                rejected.append(rejection(name, problem or f"{name}: rejected",
                                          proposal_field))
                continue
            if refs:
                quantity.evidence_refs = refs
            setattr(conditions, name, quantity)
            rendered = quantity.render()
            if rendered and refs:
                conflict_inputs.append((name, rendered, refs[0]))

        # -- components
        buckets: dict[ComponentRole, list] = {
            ComponentRole.REACTANT: conditions.reactants,
            ComponentRole.PRODUCT: conditions.products,
            ComponentRole.REAGENT: conditions.reagents,
            ComponentRole.CATALYST: conditions.catalysts,
            ComponentRole.LIGAND: conditions.ligands,
            ComponentRole.SOLVENT: conditions.solvents,
            ComponentRole.UNKNOWN: conditions.unassigned,
        }
        for proposal_field in proposal.components:
            refs = self._resolve_refs(
                proposal_field.evidence_refs, index, "component", rejected, all_refs)
            if not refs and proposal_field.status is not FieldStatus.NOT_FOUND:
                # a named substance with nothing backing it is a guess
                rejected.append(rejection(
                    str(proposal_field.value or proposal_field.raw_value or "component"),
                    "component stated without any evidence, so not recorded",
                    proposal_field))
                continue
            quotes = self._quotes_for(refs, index)
            component, problem = validate_component(proposal_field, quotes)
            if problem or component is None:
                rejected.append(rejection(
                    str(proposal_field.value or proposal_field.raw_value or "component"),
                    problem or "component rejected", proposal_field))
                continue
            component.evidence_refs = refs
            buckets.setdefault(component.role, []).append(component)
            conflict_inputs.append(
                (f"component:{component.name}", component.name, refs[0] if refs else ""))

        # -- yield type
        all_quotes = self._quotes_for(
            [r for r in all_refs if r in index], index)
        ytype_refs = self._resolve_refs(
            proposal.yield_type_evidence_refs, index, "yield_type", rejected, all_refs)
        conditions.yield_type, ytype_problem = validate_yield_type(
            proposal.yield_type, ytype_refs, all_quotes)
        conditions.yield_type_evidence_refs = ytype_refs
        if ytype_problem:
            rejected.append(ExtractionRejection(
                field="yield_type", reason=ytype_problem, proposal={}))
            result.notes.append(ytype_problem)

        conflicts = detect_conflicts(conflict_inputs)
        result.conflicts = conflicts
        result.rejected = rejected

        if not (conditions.temperature.stated or conditions.time.stated
                or conditions.yield_value.stated or conditions.reactants
                or conditions.reagents or conditions.catalysts
                or conditions.solvents or conditions.ligands
                or conditions.unassigned):
            result.outcome = ExtractionOutcome.REJECTED
            result.notes.append(
                "no condition survived validation; no record was created")
            return result

        raw_text = proposal.raw_experimental_text or all_quotes
        record = ReactionRecord(
            reaction_id=reaction_identity(
                document_id, sorted(all_refs), proposal.description),
            source=source,
            document_id=document_id,
            reaction_description=proposal.description,
            description_evidence_refs=desc_refs,
            conditions=conditions,
            evidence_refs=sorted(all_refs),
            raw_experimental_text=raw_text,
            conflicts=conflicts,
            rejected=rejected,
            extractor=result.extractor,
            policy_version=POLICY_VERSION,
        )
        result.records = [record]
        result.outcome = (ExtractionOutcome.PARTIAL if rejected
                          else ExtractionOutcome.OK)
        return result


def extract_reaction_conditions(
    llm: LLMProvider | None,
    evidence: list[Evidence],
    document_id: str | None = None,
    proposal: ExtractionProposal | None = None,
) -> ReactionExtractionResult:
    return ReactionExtractor(llm).extract(
        evidence, document_id=document_id, proposal=proposal)
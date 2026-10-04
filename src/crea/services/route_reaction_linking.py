"""Retrosynthesis step <-> literature reaction, as a measured link.

Phase 3.9 (§36-§38). A candidate route step says "something like this
transform exists"; a `Reaction` says "this paper reports this transformation,
with these components and this evidence". This service records where the two
meet -- and the record is a comparison result, never a promotion.

The ladder stays where it lives (§38):

* `compare_reactions` decides how the structures relate (exact / partial /
  transformation / discovery) -- deterministic, no model.
* `RouteValidationService` decides what that does to the step's support
  level, fed pools it can audit (§57: the route layer does not bypass it).
* `RouteEvidenceService` decides the reaction-level evidence rung, fed
  `LiteratureReactionCandidate`s this service converts.

What this service never does: set a step's status itself, treat a similar
reaction as the reaction (§35 -- similarity is discovery/filtering, and the
link says `screening_only` whenever the match is anything short of exact),
or let a `title mentions reaction` promote anything (§11). §37's rule in
one line: exact match + real literature evidence may lift a step; a similar
reaction stays PARTIAL_SUPPORT-level discovery.
"""

from __future__ import annotations

from crea.core.ids import content_hash
from crea.core.models.reaction import ReactionRecord
from crea.core.models.reaction_linking import (
    LinkBasis,
    LiteratureReactionCandidate,
)
from crea.core.models.reaction_knowledge import Reaction, RouteStepReactionLink
from crea.core.models.route import ReactionMatchType
from crea.services.reaction_matching import compare_reactions

#: How a matcher outcome maps to what that outcome establishes (§35/§37).
_BASIS_FOR_MATCH = {
    ReactionMatchType.EXACT: LinkBasis.EXACT_STRUCTURE,
    ReactionMatchType.PARTIAL: LinkBasis.PARTIAL_STRUCTURE,
    ReactionMatchType.TRANSFORMATION: LinkBasis.PARTIAL_STRUCTURE,
}


class RouteReactionLinker:
    """Links reactions to route steps without deciding anything twice."""

    def link(self, step, reaction: Reaction, *, source_id: str = "",
             doi: str | None = None, title: str = "") -> RouteStepReactionLink:
        """Measure one reaction against one step. The result is a record.

        `screening_only` is True for every non-exact match: a similar or
        partial relation may be worth a reader's attention and is never
        proof that this step is that reaction (§35).
        """
        step_reactants = [m.canonical_smiles or m.smiles for m in step.reactants]
        step_products = [m.canonical_smiles or m.smiles for m in step.products]
        reaction_reactants = [c.canonical_smiles for c in reaction.reactants
                              if c.canonical_smiles]
        reaction_products = [c.canonical_smiles for c in reaction.products
                             if c.canonical_smiles]

        comparison = compare_reactions(
            step_reactants, step_products, reaction_reactants, reaction_products,
            source_id=source_id or reaction.source_id,
            doi=doi or reaction.doi or None, title=title)
        exact = comparison.match_type is ReactionMatchType.EXACT
        notes = list(comparison.notes)
        if not exact:
            notes.append(
                "the structures were not shown to be this reaction; "
                "screening only, never support")
        return RouteStepReactionLink(
            link_id=content_hash(
                f"route-step-reaction|{step.step_id}|{reaction.reaction_id}", 16),
            step_id=step.step_id,
            reaction_id=reaction.reaction_id,
            match_type=comparison.match_type,
            link_basis=_BASIS_FOR_MATCH.get(
                comparison.match_type, LinkBasis.UNVERIFIED_STRUCTURE),
            screening_only=not exact,
            source_id=source_id or reaction.source_id,
            doi=doi or reaction.doi or "",
            notes=notes,
        )

    def stage(self, validator, reaction: Reaction,
              record: ReactionRecord | None = None,
              evidence=None) -> dict:
        """Put what this reaction holds in front of the validation service.

        Feeding pools is not deciding: RouteValidationService.validate and
        apply remain the only writers of a step's support level (§57), and
        the service's own matching is step-scoped -- only a step whose
        structures meet these structures is lifted.

        The record deliberately does NOT enter the task-level record pool
        (§37): that pool's semantics are "this task holds an experimental
        record", and it would cover steps the record says nothing about.
        Step-scoped experimental support comes from the evidence chain --
        build_record_link + RouteEvidenceService -- where the link names
        exactly one step. `record` is accepted for callers that hold one and
        is reported back for audit; it is not pooled.
        """
        kind = "document" if reaction.document_id else "paper"
        source_id = reaction.document_id or reaction.source_id or reaction.reaction_id
        added_structures = 0
        for component in reaction.components():
            smiles = component.canonical_smiles
            if not smiles:
                continue
            validator.structures.add(source_id, smiles, kind)
            added_structures += 1
        added_evidence = 0
        held = {e.evidence_id for e in validator.evidence}
        for item in (evidence or []):
            item_id = getattr(item, "evidence_id", None)
            if item_id is not None and item_id not in held:
                validator.evidence.append(item)
                held.add(item_id)
                added_evidence += 1
        return {"structures": added_structures,
                "record": record.reaction_id if record is not None else None,
                "evidence": added_evidence,
                "kind": kind, "source_id": source_id}

    def literature_candidate(self, reaction: Reaction) -> LiteratureReactionCandidate:
        """The reaction, in the shape `RouteEvidenceService` consumes.

        `structure_origin` records what the structures are worth: an
        EXACT_STRUCTURE basis when every component resolved to an entity,
        UNVERIFIED otherwise -- the same honesty the service's own builder
        enforces.
        """
        components = reaction.components()
        resolved = [c for c in components if c.canonical_smiles]
        origin = ("EXACT_STRUCTURE"
                  if components and len(resolved) == len(components)
                  else "UNVERIFIED")
        return LiteratureReactionCandidate.build(
            source=reaction.source_id or reaction.paper_id or "unknown",
            source_id=reaction.record_id or reaction.reaction_id,
            paper_id=reaction.paper_id or None,
            doi=reaction.doi or None,
            reactants=[c.canonical_smiles for c in reaction.reactants
                       if c.canonical_smiles],
            products=[c.canonical_smiles for c in reaction.products
                      if c.canonical_smiles],
            structure_origin=origin)


__all__ = ["RouteReactionLinker"]

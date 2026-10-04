"""The route evidence chain: a candidate route plus what is actually behind it.

One service, one job. Given a `CandidateRoute`, the literature reaction
candidates someone read out of the works they found, the `ReactionRecord`s
extracted from documents, and the `EvidenceRegistry` of everything the task
holds, it produces a `RouteEvidenceReport` in which every step says how far it
got and why.

    A          a provider proposed this step          RETROSYNTHESIS_CANDIDATE
    B          a relevant work exists and is named     LITERATURE_DISCOVERY
    C          structures matched, on citable text     LITERATURE_REACTION_SUPPORT
    D          a validated ReactionRecord, exact       EXPERIMENTAL_REACTION_SUPPORT
    D'         ...from a file the operator supplied     USER_DOCUMENT_EXPERIMENTAL_SUPPORT

The four rules this module exists to enforce:

1. **Nothing skips a rung.** There is no code path from a provider's score, a
   paper's title, or a fingerprint similarity to anything above
   `LITERATURE_DISCOVERY`. The promotion functions are the only writers of
   level, and each requires its own precondition to be true.

2. **Every cited evidence id is checked.** A reference that does not resolve
   in the registry is moved to `unresolved_evidence_refs`, is excluded from
   `evidence_refs`, and *costs* the step its level: support derived only from
   evidence nobody holds is not support. The report still generates, because
   hiding the gap would hide it more effectively than saying so.

3. **A reaction record is attached on a stated basis, and an unchecked basis
   promotes nothing.** `UNVERIFIED_STRUCTURE` is a real record of a real
   procedure and is not evidence for *this* step. It is listed in
   `unlinked_records` so the reading is not lost, and the step does not move.

4. **User provenance is never restated as general literature.** A step
   supported by an operator's file reaches
   `USER_DOCUMENT_EXPERIMENTAL_SUPPORT` and says so in `support_provenance`.
   If any of the supporting evidence came from a user file, the user rung is
   taken -- being mixed is a reason to claim less, not more.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crea.core.models.reaction import ReactionRecord
from crea.core.models.reaction_linking import (
    CandidateRouteStepEvidence,
    LinkBasis,
    LiteratureReactionCandidate,
    ReactionEvidenceLevel,
    ReactionMatchResult,
    ReactionRecordLink,
    RouteEvidenceReport,
    at_least,
)
from crea.core.models.route import CandidateRoute, ReactionMatchType, ReactionStep

#: Strength order for choosing one `match_type` to show for a step. This is the
#: *best* answer we managed, and it is labelled as such; it is not a claim that
#: every candidate matched.
_MATCH_ORDER = (
    ReactionMatchType.EXACT,
    ReactionMatchType.PARTIAL,
    ReactionMatchType.TRANSFORMATION,
    ReactionMatchType.NO_MATCH,
    ReactionMatchType.DISCOVERY_ONLY,
    ReactionMatchType.DISCOVERY,
)


@dataclass
class RouteEvidenceInputs:
    """Everything outside the route that could support it."""

    candidates: list[LiteratureReactionCandidate] = field(default_factory=list)
    #: candidate_id -> the evidence ids the candidate's structures rest on.
    #: Kept apart from the candidate because `LiteratureReactionCandidate`
    #: must stay free of anything that could be mistaken for a quotation.
    candidate_evidence: dict[str, list[str]] = field(default_factory=dict)
    records: list[ReactionRecord] = field(default_factory=list)
    record_links: list[ReactionRecordLink] = field(default_factory=list)


class RouteEvidenceService:
    """Compose a route and its evidence into one auditable report."""

    def __init__(self, registry, inputs: RouteEvidenceInputs | None = None) -> None:
        #: An `EvidenceRegistry`, or anything exposing `get(id)`. Typed loosely
        #: so a plain dict works in a test without a registry-shaped shim.
        self.registry = registry
        self.inputs = inputs or RouteEvidenceInputs()

    # -- resolution ---------------------------------------------------------

    def _resolve(self, evidence_id: str):
        getter = getattr(self.registry, "get", None)
        if getter is None and isinstance(self.registry, dict):
            return self.registry.get(evidence_id)
        if getter is not None:
            try:
                return getter(evidence_id)
            except TypeError:
                return None
        return None

    # -- the report ---------------------------------------------------------

    def build(self, route: CandidateRoute) -> RouteEvidenceReport:
        report = RouteEvidenceReport(
            route_id=route.route_id,
            target=route.target.canonical_smiles or route.target.smiles
            or route.target.name or route.target.molecule_id,
            route_source=route.source,
            provider_score=route.score,
            score_source=route.score_source.value,
            warnings=list(route.warnings),
        )

        unresolved: list[str] = []
        for step in route.steps:
            step_evidence = self._step_evidence(route, step)
            report.steps.append(step_evidence)
            for ref in step_evidence.evidence_refs:
                if ref not in report.evidence_refs:
                    report.evidence_refs.append(ref)
            for ref in step_evidence.unresolved_evidence_refs:
                message = (
                    f"step {step.step_id}: evidence {ref} is cited but not in "
                    f"the registry, so it supports nothing and was not counted")
                if message not in unresolved:
                    unresolved.append(message)

        if unresolved:
            report.unresolved.extend(unresolved)
        report.levels_present = sorted(
            {s.evidence_level for s in report.steps},
            key=lambda level: level.value)

        if route.score is not None:
            report.warnings.append(
                f"the {report.score_source} score is that engine's own search "
                f"heuristic. It is not a confidence, a probability, or "
                f"evidence that the chemistry works.")
        return report

    # -- one step -----------------------------------------------------------

    def _step_evidence(self, route: CandidateRoute,
                       step: ReactionStep) -> CandidateRouteStepEvidence:
        result = CandidateRouteStepEvidence(
            step_id=step.step_id,
            step_index=step.step_index,
            # The provider's own view of the step, carried as it stands. It is
            # read, never written: a step's candidate status is the route
            # validator's to move, and this service's job is the reaction-level
            # chain beside it.
            candidate_status=step.status,
            evidence_level=ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE,
            match_type=ReactionMatchType.NO_MATCH,
        )

        self._apply_literature(result, step)
        self._apply_records(result, step)
        return result

    def _apply_literature(self, result: CandidateRouteStepEvidence,
                          step: ReactionStep) -> None:
        """What the literature candidates say about this step."""
        if not self.inputs.candidates:
            return

        step_reactants = [m.canonical_smiles or m.smiles for m in step.reactants]
        step_products = [m.canonical_smiles or m.smiles for m in step.products]

        from crea.services.literature_reactions import match_candidate, reference_for

        for candidate in self.inputs.candidates:
            refs = list(self.inputs.candidate_evidence.get(
                candidate.candidate_id, []))
            unresolved = [r for r in refs if self._resolve(r) is None]
            match = match_candidate(
                step_id=step.step_id,
                candidate=candidate,
                reference=reference_for(candidate),
                step_reactants=step_reactants,
                step_products=step_products,
                evidence_refs=refs,
                structures_resolved=not unresolved,
            )
            for ref in unresolved:
                if ref not in result.unresolved_evidence_refs:
                    result.unresolved_evidence_refs.append(ref)

            result.matches.append(match)
            self._promote(result, match.evidence_level)

            if at_least(match.evidence_level,
                        ReactionEvidenceLevel.LITERATURE_REACTION_SUPPORT):
                result.literature_support.append(match.reference_id)
                for ref in match.evidence_refs:
                    if ref not in result.evidence_refs:
                        result.evidence_refs.append(ref)
                if match.doi and match.doi not in result.doi:
                    result.doi.append(match.doi)
                result.support_source = match.source
                result.support_provenance = match.source
                result.support_reason = match.match_basis

        result.match_type = _best_match(result.matches)

    def _apply_records(self, result: CandidateRouteStepEvidence,
                       step: ReactionStep) -> None:
        """What the reaction records say about this step.

        Isolation is structural rather than promised: a link names one
        `step_id`, and only links naming *this* step are read here. A record
        attached to step 2 can never appear on step 1, whatever it contains.
        """
        for link in self.inputs.record_links:
            if link.step_id != step.step_id:
                continue

            record = next((r for r in self.inputs.records
                           if r.reaction_id == link.reaction_id), None)
            if record is None:
                result.warnings.append(
                    f"a record link names {link.reaction_id}, which is not "
                    f"among the records this task holds; it supports nothing")
                continue

            refs = [r for r in (link.evidence_refs or record.evidence_refs)]
            resolved = [(ref, self._resolve(ref)) for ref in refs]
            missing = [ref for ref, item in resolved if item is None]
            resolved = [(ref, item) for ref, item in resolved if item is not None]

            for ref in missing:
                if ref not in result.unresolved_evidence_refs:
                    result.unresolved_evidence_refs.append(ref)

            if not resolved:
                # No citable passage under this record. Same rule the route
                # validator already applies, for the same reason: a record
                # nobody can quote is a reading with nothing behind it.
                if link.reaction_id not in result.unlinked_records:
                    result.unlinked_records.append(link.reaction_id)
                result.warnings.append(
                    f"record {link.reaction_id} cites no evidence this task "
                    f"holds; it is listed but supports nothing")
                continue

            if not _measured_exactly(link):
                # The one rule this phase exists to enforce. The procedure is
                # real; that it is *this* procedure was not shown.
                if link.reaction_id not in result.unlinked_records:
                    result.unlinked_records.append(link.reaction_id)
                result.warnings.append(
                    f"record {link.reaction_id} was attached without an exact "
                    f"structure comparison ({_link_basis_reason(link)}), so it is "
                    f"reported as read but not as support for this step")
                continue

            sources = {item.source for _ref, item in resolved}
            from_user = any(s == "user_provided" for s in sources)
            level = (ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT
                     if from_user
                     else ReactionEvidenceLevel.EXPERIMENTAL_REACTION_SUPPORT)

            result.experimental_support.append(link.reaction_id)
            for ref, _item in resolved:
                if ref not in result.evidence_refs:
                    result.evidence_refs.append(ref)
            if record.document_id and record.document_id not in result.document_ids:
                result.document_ids.append(record.document_id)
            result.match_type = (ReactionMatchType.EXACT
                                 if link.match_type is ReactionMatchType.EXACT
                                 else link.match_type)
            result.support_source = sorted(sources)[0]
            result.support_provenance = "user_provided" if from_user else "literature"
            result.support_reason = (
                f"a validated ReactionRecord ({link.reaction_id}) with "
                f"conditions, its structures matched this step exactly, and "
                f"it cites {len(resolved)} piece(s) of evidence this task holds")
            self._promote(result, level)

    # -- promotion ----------------------------------------------------------

    @staticmethod
    def _promote(result: CandidateRouteStepEvidence,
                 level: ReactionEvidenceLevel) -> None:
        """Move a step's level up, and only up.

        Deliberately takes the *proposed* level rather than the maximum over
        everything seen so far: `at_least` already knows the two experimental
        rungs are peers, and comparing them with `>=` would silently rank a
        user document against a published procedure.
        """
        if at_least(level, result.evidence_level):
            result.evidence_level = level


def _measured_exactly(link: ReactionRecordLink) -> bool:
    """Whether the link's basis can be believed, not merely asserted.

    `build_record_link` compares structures itself and writes down what it
    found, so a link from it is a measurement. A link built by hand is not: a
    caller can name any basis it likes, including `EXACT_STRUCTURE`, without
    having compared anything. A comparison needs something to compare, so a
    link carrying no structures on either side could not have produced an exact
    result whatever its `basis` says. Claiming harder is not the way to earn
    support here; bringing the structures is.
    """
    if link.basis is not LinkBasis.EXACT_STRUCTURE:
        return False
    return bool(link.reactant_smiles) and bool(link.product_smiles)


def _link_basis_reason(link: ReactionRecordLink) -> str:
    """Why a link did not earn support, in words the reader can act on."""
    if link.basis is LinkBasis.EXACT_STRUCTURE and not (
            link.reactant_smiles and link.product_smiles):
        return (f"exact structure was asserted but no structures were carried, "
                f"so nothing was compared (stated_by={link.stated_by})")
    return link.basis.value


def _best_match(matches: list[ReactionMatchResult]) -> ReactionMatchType:
    """The best comparison we managed for a step, or NO_MATCH if none ran."""
    if not matches:
        return ReactionMatchType.NO_MATCH
    present = {m.match_type for m in matches}
    for kind in _MATCH_ORDER:
        if kind in present:
            return kind
    return ReactionMatchType.NO_MATCH


def build_record_link(step: ReactionStep, reaction_id: str,
                      reactant_smiles: list[str] | None = None,
                      product_smiles: list[str] | None = None,
                      evidence_refs: list[str] | None = None
                      ) -> ReactionRecordLink:
    """Attach a reaction record to a step on a *measured* basis, or decline to.

    The caller supplies the structures it read for the record; the comparison
    is run here, deterministically, so the basis is a result rather than a
    claim. A caller with no structures gets `UNVERIFIED_STRUCTURE`, which the
    service will refuse to promote -- so the way to earn support is to bring
    structures, not to assert harder.
    """
    from crea.services.reaction_matching import compare_reactions

    reactants = [s for s in (reactant_smiles or []) if s and s.strip()]
    products = [s for s in (product_smiles or []) if s and s.strip()]

    if not reactants and not products:
        return ReactionRecordLink(
            step_id=step.step_id, reaction_id=reaction_id,
            basis=LinkBasis.UNVERIFIED_STRUCTURE,
            match_type=ReactionMatchType.NO_MATCH,
            evidence_refs=list(evidence_refs or []),
            stated_by="caller")

    comparison = compare_reactions(
        [m.canonical_smiles or m.smiles for m in step.reactants],
        [m.canonical_smiles or m.smiles for m in step.products],
        reactants, products,
    )
    if comparison.match_type is ReactionMatchType.EXACT:
        basis = LinkBasis.EXACT_STRUCTURE
    elif comparison.match_type is ReactionMatchType.PARTIAL:
        basis = LinkBasis.PARTIAL_STRUCTURE
    else:
        basis = LinkBasis.UNVERIFIED_STRUCTURE

    return ReactionRecordLink(
        step_id=step.step_id, reaction_id=reaction_id,
        basis=basis, match_type=comparison.match_type,
        reactant_smiles=reactants, product_smiles=products,
        evidence_refs=list(evidence_refs or []),
        stated_by="deterministic")


def evidence_is_user_provided(evidence) -> bool:
    """Is this Evidence from a file the operator handed us?"""
    return getattr(evidence, "source", "") == "user_provided"
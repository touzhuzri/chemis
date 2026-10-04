"""Route validation: connect a candidate step to what we can actually show.

Deterministic throughout. Structures are compared with the chemistry core;
evidence and reaction records are resolved against what the task holds; and the
support level is the highest rung something actually reached. The LLM is not
consulted, and could not promote a step if it were -- there is no code path here
that turns an opinion into support.

    NO_SUPPORT            nothing matched
    PARTIAL_SUPPORT       a related structure, not the same one
    STRUCTURAL_SUPPORT    the structures match exactly
    LITERATURE_SUPPORT    a paper was found and describes this transformation
    EXPERIMENTAL_SUPPORT  a ReactionRecord with real conditions is attached
    USER_DOCUMENT_SUPPORT it came from a document the user supplied

Similarity is not support. Two molecules that fingerprint alike have not been
shown to be made by the same reaction, so a step that matches only by
similarity is `PARTIAL_SUPPORT` and says which paper it matched.

What v1 can and cannot see, stated plainly: a metadata `Paper` carries a title
and a DOI but no structure, so structural comparison runs against a structure
pool the caller supplies explicitly -- typically molecules the agent extracted
from a document. Nothing here infers a structure from a name, because there is
no verified name resolver in this project to infer one with.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crea.core.models.evidence import Evidence
from crea.core.models.literature import Paper
from crea.core.models.reaction import ReactionRecord
from crea.core.models.route import (
    ReactionStep,
    RouteValidationResult,
    StepStatus,
    SupportLevel,
)

#: Tanimato at or above which a structural similarity is worth reporting as
#: "related". Below this, two molecules being alike is noise, not a finding.
RELATED_SIMILARITY_FLOOR = 0.55

#: Step status is a consequence of support, never the other way round.
_STATUS_FOR = {
    SupportLevel.NO_SUPPORT: StepStatus.CANDIDATE,
    SupportLevel.PARTIAL_SUPPORT: StepStatus.CANDIDATE,
    SupportLevel.STRUCTURAL_SUPPORT: StepStatus.LITERATURE_SUPPORTED,
    SupportLevel.LITERATURE_SUPPORT: StepStatus.LITERATURE_SUPPORTED,
    SupportLevel.EXPERIMENTAL_SUPPORT: StepStatus.EXPERIMENTALLY_SUPPORTED,
    SupportLevel.USER_DOCUMENT_SUPPORT: StepStatus.EXPERIMENTALLY_SUPPORTED,
}

_RANK = {
    SupportLevel.NO_SUPPORT: 0,
    SupportLevel.PARTIAL_SUPPORT: 1,
    SupportLevel.STRUCTURAL_SUPPORT: 2,
    SupportLevel.LITERATURE_SUPPORT: 3,
    SupportLevel.EXPERIMENTAL_SUPPORT: 4,
    SupportLevel.USER_DOCUMENT_SUPPORT: 4,
}


@dataclass
class StructurePool:
    """Structures the task actually has, with where each came from.

    Explicit because the sources are explicit: molecules read out of a
    document, or listed by a caller who extracted them. A `Paper` does not
    carry structures, and pretending otherwise would make structural
    comparison silently vacuous.
    """

    entries: list[tuple[str, str, str]] = field(default_factory=list)
    # (source_id, smiles, kind) where kind is "document" | "paper" | "user"

    def add(self, source_id: str, smiles: str, kind: str = "document") -> None:
        self.entries.append((source_id, smiles, kind))

    def canonical(self) -> list[tuple[str, str, str]]:
        return [(sid, _canonical(s), kind) for sid, s, kind in self.entries if s]


def _canonical(smiles: str) -> str:
    from crea.chem.normalize import normalize

    if not smiles:
        return ""
    try:
        return normalize(smiles).canonical_smiles
    except Exception:  # noqa: BLE001
        return smiles


def _similar(a: str, b: str) -> float:
    """Fingerprint similarity, through the chemistry core's own entry point.

    Reimplemented here it would be a second answer to the same question, and
    two answers is exactly what the screening layer's design forbids.
    """
    from crea.chem.fingerprint import screen_similarity
    from crea.chem.normalize import normalize

    try:
        return screen_similarity(normalize(a), normalize(b)).tanimoto_ecfp4
    except Exception:  # noqa: BLE001
        return 0.0


class RouteValidationService:
    """Answers one question: what do we actually know about this step?"""

    def __init__(
        self,
        papers: list[Paper] | None = None,
        evidence: list[Evidence] | None = None,
        reaction_records: list[ReactionRecord] | None = None,
        structures: StructurePool | None = None,
    ) -> None:
        self.papers = list(papers or [])
        self.evidence = list(evidence or [])
        self.reaction_records = list(reaction_records or [])
        self.structures = structures or StructurePool()

    # -- the question ------------------------------------------------------

    def validate(self, step: ReactionStep) -> RouteValidationResult:
        result = RouteValidationResult(step_id=step.step_id)
        held_ids = {e.evidence_id for e in self.evidence}

        # 1. Experimental. A ReactionRecord only counts if it exists in this
        #    task and cites evidence this task holds. A caller may name one; we
        #    check rather than believe.
        for record in self.reaction_records:
            refs = [r for r in record.evidence_refs if r in held_ids]
            if not refs:
                continue
            result.reaction_record_ids.append(record.reaction_id)
            result.evidence_refs.extend(refs)
            result.checked_count += 1
            self._raise(result, SupportLevel.EXPERIMENTAL_SUPPORT, "reaction_record")

        # 2. Structural, against the pool the task actually has.
        pool = self.structures.canonical()
        wanted: list[str] = []
        for molecule in list(step.products) + list(step.reactants):
            canonical = _canonical(molecule.canonical_smiles or molecule.smiles)
            if canonical and canonical not in wanted:
                wanted.append(canonical)

        for canonical in wanted:
            for source_id, structure, kind in pool:
                result.checked_count += 1
                if structure == canonical:
                    if source_id not in result.paper_ids:
                        result.paper_ids.append(source_id)
                    result.matched_smiles.append(canonical)
                    level = (SupportLevel.STRUCTURAL_SUPPORT
                              if kind == "document"
                              else SupportLevel.LITERATURE_SUPPORT)
                    self._raise(result, level, "exact_structure")
                    if kind == "document":
                        self._raise(result, SupportLevel.USER_DOCUMENT_SUPPORT,
                                    "document_structure")
                elif _similar(structure, canonical) >= RELATED_SIMILARITY_FLOOR:
                    result.matched_smiles.append(canonical)
                    result.warnings.append(
                        f"{source_id} is structurally similar to {canonical} but "
                        f"was not shown to make this step")
                    self._raise(result, SupportLevel.PARTIAL_SUPPORT,
                                "related_structure")

        if result.support_level is SupportLevel.NO_SUPPORT:
            result.warnings.append(
                "nothing in the material consulted so far supports this step")
        return result

    @staticmethod
    def _raise(result: RouteValidationResult, level: SupportLevel, kind: str) -> None:
        """Support only ever moves up."""
        if _RANK[level] > _RANK[result.support_level]:
            result.support_level = level
            result.match_kind = kind

    # -- applying it -------------------------------------------------------

    @staticmethod
    def apply(result: RouteValidationResult, step: ReactionStep) -> ReactionStep:
        """Move a step's status to whatever the evidence earned -- upward only.

        A later check that finds nothing is not a retraction: the material we
        consulted has grown, it has not shrunk. Downgrading here would let a
        step lose its literature support merely because a second pass had
        nothing new to add, and would be a quiet way of losing evidence.
        """
        if _RANK[result.support_level] >= _RANK[step.support_level]:
            step.support_level = result.support_level
            step.status = _STATUS_FOR[result.support_level]
        for ref in result.evidence_refs:
            if ref not in step.evidence_refs:
                step.evidence_refs.append(ref)
        for paper in result.paper_ids:
            if paper not in step.paper_ids:
                step.paper_ids.append(paper)
        for record in result.reaction_record_ids:
            if record not in step.reaction_record_ids:
                step.reaction_record_ids.append(record)
        return step


#: The exact sentences required when a step has nothing behind it. They live
#: here because this is where the decision is made; the report displays them and
#: does not choose its own wording.
CANDIDATE_NOTE = "该步骤目前仅为逆合成工具提出的候选方案，尚未找到文献实验支持。"
PARTIAL_NOTE = "发现相关转化证据，但尚不足以证明该具体底物组合已有文献实验验证。"


def unresolved_note(result: RouteValidationResult, step: ReactionStep) -> str:
    """The sentence the report uses when a step has nothing behind it."""
    if result.support_level in (SupportLevel.LITERATURE_SUPPORT,
                                SupportLevel.STRUCTURAL_SUPPORT,
                                SupportLevel.EXPERIMENTAL_SUPPORT,
                                SupportLevel.USER_DOCUMENT_SUPPORT):
        return ""
    if result.support_level is SupportLevel.PARTIAL_SUPPORT:
        return ("发现相关转化证据，但尚不足以证明该具体底物组合已有文献实验验证。")
    return "该步骤目前仅为逆合成工具提出的候选方案，尚未找到文献实验支持。"
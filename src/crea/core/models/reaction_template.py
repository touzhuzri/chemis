"""The reaction knowledge model (CREA Stage 3.1).

A `ReactionTemplate` is a reusable chemical transformation pattern -- a formal
object, not a matched string. Its fields separate concerns the way a chemist
thinks of them (§6-§19):

* `template_id` is CREA's internal record id (§5), never an "international
  reaction identifier" -- external references go in `external_references`.
* `name` is a human label ("Suzuki Coupling"), never a source of semantics.
* `class_family` + `reaction_class` name the transformation category; the
  mechanism is a separate field and is never inferred from the class (§7/§14).
* `transformation` is the structured functional-group change (input -> output
  features), not a name (§9).
* `substrate_patterns` / `product_patterns` are the SMARTS the matcher uses;
  an unencoded product pattern is stated, never faked (§10/§11).
* `reagents` and `conditions` are distinct (§12/§13); conditions are *typical*,
  not experimental instances.
* `selectivity` / `limitations` are stored knowledge, not automatic judgement.
* `evidence_status` says what the facts rest on -- CURATED seed data is curated,
  never dressed up as literature evidence (§16/§18).

`ReactionMatch` only says the substrate pattern matched (§20): a structural
compatibility statement, never a guarantee that a reaction succeeds.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.provenance import Provenance, utc_now_iso


class ReactionClassFamily(str, Enum):
    """The broad reaction family (§7/§22)."""

    SUBSTITUTION = "substitution"
    ADDITION = "addition"
    ELIMINATION = "elimination"
    OXIDATION = "oxidation"
    REDUCTION = "reduction"
    COUPLING = "coupling"
    REARRANGEMENT = "rearrangement"
    CONDENSATION = "condensation"


class EvidenceStatus(str, Enum):
    """What a fact rests on (§18). There is deliberately no `VERIFIED`:
    no verification workflow exists yet, so claiming one would be a lie."""

    UNKNOWN = "unknown"
    CURATED = "curated"
    LITERATURE_SUPPORTED = "literature_supported"
    USER_PROVIDED = "user_provided"
    PROVIDER_DERIVED = "provider_derived"


class Transformation(BaseModel):
    """The structured functional-group change (§9): input -> output features."""

    input_features: list[str] = Field(default_factory=list)
    output_features: list[str] = Field(default_factory=list)
    note: str = ""

    def label(self) -> str:
        return self.note or (
            f"{'+'.join(self.input_features)} -> "
            f"{'+'.join(self.output_features)}")


class ReagentSpec(BaseModel):
    """One reagent or catalyst, distinct from conditions (§12)."""

    name: str = ""
    role: str = ""


class ConditionSpec(BaseModel):
    """One typical condition (§13): a named dimension and its value."""

    kind: str = ""   # solvent | temperature | time | atmosphere | pressure | catalyst | general
    value: str = ""


class SelectivitySpec(BaseModel):
    """Stored selectivity knowledge (§15), never auto-derived."""

    regioselectivity: str = ""
    stereoselectivity: str = ""
    chemoselectivity: str = ""
    note: str = ""


class Limitation(BaseModel):
    """A stored limitation, with its own evidence status (§16)."""

    text: str = ""
    evidence_status: EvidenceStatus = EvidenceStatus.CURATED


class ExternalReference(BaseModel):
    """An identifier in an external reaction database (§5)."""

    namespace: str = ""
    identifier: str = ""


class ReactionTemplate(BaseModel):
    """One reusable chemical transformation pattern."""

    template_id: str
    name: str
    class_family: ReactionClassFamily
    reaction_class: str = ""
    transformation: Transformation
    substrate_patterns: list[str] = Field(default_factory=list)
    product_patterns: list[str] = Field(default_factory=list)
    product_pattern_note: str = ""
    #: A deterministic reaction transform (`reactant>>product` SMARTS) when one
    #: exists. Empty for every seed template -- a pattern to *match* a product
    #: is not a rule to *make* one, and the difference is why Route Explorer
    #: stops rather than guessing (§30). Only a template carrying this can
    #: deterministically produce a product.
    reaction_smarts: str = ""
    reagents: list[ReagentSpec] = Field(default_factory=list)
    conditions: list[ConditionSpec] = Field(default_factory=list)
    mechanism_class: str = ""
    selectivity: SelectivitySpec | None = None
    limitations: list[Limitation] = Field(default_factory=list)
    source_type: str = "CURATED"
    source_id: str = ""
    source_url: str = ""
    retrieved_at: str = Field(default_factory=utc_now_iso)
    evidence_status: EvidenceStatus = EvidenceStatus.CURATED
    external_references: list[ExternalReference] = Field(default_factory=list)
    provenance: Provenance | None = None


class ReactionMatch(BaseModel):
    """A template whose substrate pattern matched a molecule (§20)."""

    template: ReactionTemplate
    matched: bool = True
    matched_atoms: list[list[int]] = Field(default_factory=list)
    #: Matching is a structural statement, never an experimental-success claim.
    note: str = (
        "the substrate pattern matched; this is a known transformation "
        "pattern, not a guarantee that the reaction succeeds")


__all__ = [
    "ConditionSpec",
    "EvidenceStatus",
    "ExternalReference",
    "Limitation",
    "ReactionClassFamily",
    "ReactionMatch",
    "ReactionTemplate",
    "ReagentSpec",
    "SelectivitySpec",
    "Transformation",
]

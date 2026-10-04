"""Structured experimental facts extracted from a paper's procedure.

A `ReactionRecord` is a reading of some Experimental text, not a fact about the
world. That distinction is the whole design: every field says which Evidence it
came from, and a field the paper never mentioned is absent rather than filled
with a plausible default.

Nothing here invents. There is no `pressure = 1 atm` because a reaction at
ambient pressure is the usual case; there is no `yield_type = isolated` because
that is how yields are usually reported. A field is present when the text says
so, and the record says which text.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from crea.core.ids import content_hash


class FieldStatus(str, Enum):
    """How a field got here. Not a confidence score.

    There is deliberately no number between these. A probability nobody defined
    looks like a measurement; these say only what kind of statement was made.
    """

    EXPLICIT = "explicit"    # the text states it
    INFERRED = "inferred"    # reasoned from the text, not stated
    NOT_FOUND = "not_found"  # looked, absent
    AMBIGUOUS = "ambiguous"  # the text offers more than one reading


class YieldType(str, Enum):
    """What kind of yield the paper reported.

    `UNKNOWN` is the common case and is not a failure. "82% yield" does not say
    whether that is isolated, crude or a conversion, and guessing `isolated`
    because it usually is would be exactly the kind of quiet invention this
    project refuses.
    """

    UNKNOWN = "unknown"
    ISOLATED = "isolated"
    CRUDE = "crude"
    CONVERSION = "conversion"
    SELECTIVITY = "selectivity"
    YIELD = "yield"          # the paper said "yield" and nothing more


class ComponentRole(str, Enum):
    """What the paper says a substance is doing.

    Literature is often ambiguous -- a base can be listed as a reagent or a
    component -- and a role we cannot establish is `UNKNOWN` rather than a guess.
    """

    REACTANT = "reactant"
    REAGENT = "reagent"
    CATALYST = "catalyst"
    LIGAND = "ligand"
    SOLVENT = "solvent"
    BASE = "base"
    ADDITIVE = "additive"
    PRODUCT = "product"
    UNKNOWN = "unknown"


class Quantity(BaseModel):
    """A number with its unit, unconverted.

    No unit conversion happens here. `80 °C` stays `80 °C`; it does not become
    `353 K`, because a conversion is an interpretation and this layer's job is
    to report what the paper said. If a caller wants Kelvin it can add it and
    leave the raw value beside it.
    """

    value: float | None = None
    raw_value: str = ""
    unit: str = "UNKNOWN"
    raw_unit: str = ""
    status: FieldStatus = FieldStatus.NOT_FOUND
    evidence_refs: list[str] = Field(default_factory=list)
    raw_text: str = ""

    @property
    def stated(self) -> bool:
        return self.status in (FieldStatus.EXPLICIT, FieldStatus.INFERRED)

    def render(self) -> str:
        if not self.stated or self.value is None:
            return ""
        return f"{_trim(self.raw_value)} {self.unit}".strip()


class Component(BaseModel):
    """A named substance. A name, never a structure we guessed.

    There is no reliable chemical-name-to-structure tool in this project, so a
    name stays a name. Attaching a SMILES here would assert an identification
    nobody verified.
    """

    name: str
    role: ComponentRole = ComponentRole.UNKNOWN
    amount: Quantity | None = None
    unit: str = ""
    raw_text: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class ReactionConditions(BaseModel):
    """The quantitative and qualitative conditions, one record per procedure."""

    temperature: Quantity = Field(default_factory=Quantity)
    time: Quantity = Field(default_factory=Quantity)
    pressure: Quantity = Field(default_factory=Quantity)
    concentration: Quantity = Field(default_factory=Quantity)
    yield_value: Quantity = Field(default_factory=Quantity)
    yield_type: YieldType = YieldType.UNKNOWN
    yield_type_evidence_refs: list[str] = Field(default_factory=list)
    atmosphere: str = ""
    atmosphere_evidence_refs: list[str] = Field(default_factory=list)
    workup: str = ""
    workup_evidence_refs: list[str] = Field(default_factory=list)
    purification: str = ""
    purification_evidence_refs: list[str] = Field(default_factory=list)

    reactants: list[Component] = Field(default_factory=list)
    #: What the procedure made. Additive (Phase 3.10): the extraction policy
    #: has always accepted PRODUCT-role components, and before this field
    #: existed they were routed to a bucket nobody read.
    products: list[Component] = Field(default_factory=list)
    reagents: list[Component] = Field(default_factory=list)
    catalysts: list[Component] = Field(default_factory=list)
    ligands: list[Component] = Field(default_factory=list)
    solvents: list[Component] = Field(default_factory=list)
    #: Named in the procedure, but the text never said what it was doing.
    #: Dropping these would lose a real substance from the record; guessing a
    #: role for them would be inventing one.
    unassigned: list[Component] = Field(default_factory=list)


class ExtractionConflict(BaseModel):
    """Two pieces of evidence disagree. Recorded, never resolved by picking one."""

    field: str
    values: dict[str, str] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    note: str = ""


class ExtractionRejection(BaseModel):
    """A proposed field that did not survive validation."""

    field: str
    reason: str
    proposal: dict[str, Any] = Field(default_factory=dict)


class ReactionRecord(BaseModel):
    """One procedure, read out of one or more pieces of Experimental text."""

    reaction_id: str
    source: str = "document"
    document_id: str | None = None
    paper_id: str | None = None

    reaction_description: str = ""
    description_evidence_refs: list[str] = Field(default_factory=list)

    conditions: ReactionConditions = Field(default_factory=ReactionConditions)
    evidence_refs: list[str] = Field(default_factory=list)
    raw_experimental_text: str = ""

    conflicts: list[ExtractionConflict] = Field(default_factory=list)
    rejected: list[ExtractionRejection] = Field(default_factory=list)
    extractor: str = ""
    policy_version: str = "reaction-v1"


class FieldProposal(BaseModel):
    """What the model proposes, before anything is believed.

    The model is a proposer. This is what comes out of it, and it has not yet
    been checked against a single thing.
    """

    name: str
    value: Any = None
    raw_value: str = ""
    unit: str = ""
    raw_unit: str = ""
    status: FieldStatus = FieldStatus.NOT_FOUND
    evidence_refs: list[str] = Field(default_factory=list)
    raw_text: str = ""
    component_role: str = ""

    @field_validator("status", mode="before")
    @classmethod
    def _accept_either_case(cls, value):
        """A model asked for "EXPLICIT" answers "EXPLICIT".

        The extraction prompt lists the statuses in upper case because that is
        how enums are conventionally written. Accepting only the lower-case
        spelling would mean every extraction a real model produced failed at the
        first field, for a casing reason.
        """
        if isinstance(value, str):
            return value.strip().lower()
        return value


class ExtractionProposal(BaseModel):
    """A model's whole answer for one procedure."""

    description: str = ""
    description_evidence_refs: list[str] = Field(default_factory=list)
    components: list[FieldProposal] = Field(default_factory=list)
    conditions: list[FieldProposal] = Field(default_factory=list)
    yield_type: str = "unknown"
    yield_type_evidence_refs: list[str] = Field(default_factory=list)
    raw_experimental_text: str = ""


class ExtractionOutcome(str, Enum):
    OK = "ok"
    PARTIAL = "partial"        # some fields held, some were rejected
    REJECTED = "rejected"      # nothing survived


class ReactionExtractionResult(BaseModel):
    """The whole story of one extraction, including what failed."""

    outcome: ExtractionOutcome = ExtractionOutcome.REJECTED
    records: list[ReactionRecord] = Field(default_factory=list)
    conflicts: list[ExtractionConflict] = Field(default_factory=list)
    rejected: list[ExtractionRejection] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    extractor: str = ""
    policy_version: str = "reaction-v1"


def _trim(text: str) -> str:
    return text.strip() or str(text)


REACTION_ID_VERSION = "reaction-id-v1"


def reaction_identity(document_id: str | None, evidence_refs: list[str],
                      description: str) -> str:
    """Stable identity for a record derived from given evidence.

    Deliberately excludes the extraction timestamp: re-reading the same passages
    should give the same record, or a report would accumulate duplicates of a
    procedure it has already described.
    """
    payload = "|".join([
        REACTION_ID_VERSION,
        document_id or "",
        ",".join(sorted(evidence_refs)),
        description.strip()[:120],
    ])
    return content_hash(payload, 16)
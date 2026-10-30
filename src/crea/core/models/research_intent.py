"""M1.6 -- the structured research request: what the user asked, in full.

`ResearchIntent` is the output of input understanding and the only thing the
Planner ever receives from the natural-language side. It keeps the original
user input verbatim, records every field's source, and refuses to blur the
line between what the user said, what a deterministic parse found, what the
study context inherited, and what a model merely inferred.

The load-bearing rules, as models:

* **Entities are recognised, never confirmed by language.** An entity's
  `identity_status` says exactly how far it got: a user-supplied SMILES that
  the chem layer parsed (`structure_validated`), a name that was recognised
  but not resolved (`name_recognized`), a name known to be ambiguous
  (`ambiguous`, with candidates), or a structure established through the
  identity mechanism (`resolved`). Nothing here promotes a name to a
  structure.
* **Intents are plural.** A request about papers *and* routes keeps both --
  a `primary_intent` and `secondary_intents`, never one collapsed label.
* **Constraints carry their source.** `USER_EXPLICIT` / `CONTEXT_INHERITED` /
  `SYSTEM_POLICY` / `MODEL_INFERRED`: a guessed constraint can never wear the
  user's words.
* **Missing information is typed by severity.** Required gaps block only the
  work that needs them; defaultable and deferrable gaps are recorded, not
  treated as failures.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.provenance import utc_now_iso


class ResearchIntentKind(str, Enum):
    """What kind of research work the request asks for."""

    MOLECULE_IDENTIFICATION = "molecule_identification"
    STRUCTURE_ANALYSIS = "structure_analysis"
    MOLECULE_COMPARISON = "molecule_comparison"
    SIMILARITY_SEARCH = "similarity_search"
    LITERATURE_RESEARCH = "literature_research"
    REACTION_RESEARCH = "reaction_research"
    RETROSYNTHESIS = "retrosynthesis"
    SYNTHESIS_ROUTE_RESEARCH = "synthesis_route_research"
    MECHANISM_ANALYSIS = "mechanism_analysis"
    PROPERTY_ANALYSIS = "property_analysis"
    GENERAL_CHEMISTRY_QUESTION = "general_chemistry_question"
    CONTINUE_RESEARCH = "continue_research"
    UNKNOWN = "unknown"


class IntentSource(str, Enum):
    """Where a field's value came from. Never merged, never laundered."""

    USER_EXPLICIT = "user_explicit"
    CONTEXT_INHERITED = "context_inherited"
    SYSTEM_POLICY = "system_policy"
    MODEL_INFERRED = "model_inferred"


class EntityRole(str, Enum):
    """The role an entity plays in this request."""

    TARGET = "target"
    REACTANT = "reactant"
    PRODUCT = "product"
    INTERMEDIATE = "intermediate"
    REFERENCE = "reference"
    COMPARISON = "comparison"
    UNKNOWN = "unknown"


class EntityIdentityStatus(str, Enum):
    """How far the entity's identity got -- recognition is not confirmation.

    `structure_validated` means the USER's own SMILES/InChI-like input passed
    the existing deterministic parser. `resolved` means the identity
    mechanism (including a user's candidate selection) established it.
    `name_recognized` means the words were found and nothing more.
    """

    STRUCTURE_VALIDATED = "structure_validated"
    STRUCTURE_INVALID = "structure_invalid"
    NAME_RECOGNIZED = "name_recognized"
    AMBIGUOUS = "ambiguous"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class IntentEntity(BaseModel):
    """One chemical object the request mentions, as the user mentioned it.

    `value` is the original text; `normalized_smiles` is filled only by the
    chem layer for input that actually parsed. `candidates` preserves every
    candidate when the name is ambiguous -- never truncated to a winner.
    """

    name: str
    #: The `StructureInput` kind when one applies ("smiles" / "inchi" / "name").
    kind: str = ""
    value: str = ""
    normalized_smiles: str = ""
    #: "molecule" | "reaction" | "route" | "document" ...
    entity_type: str = "molecule"
    role: EntityRole = EntityRole.UNKNOWN
    identity_status: EntityIdentityStatus = EntityIdentityStatus.UNRESOLVED
    #: Candidate views kept verbatim (label + kind + value at minimum).
    candidates: list[dict] = Field(default_factory=list)
    source: IntentSource = IntentSource.USER_EXPLICIT
    structure_validated: bool = False
    note: str = ""


class ResearchConstraint(BaseModel):
    """One bound on the research, with where it came from.

    `superseded_by` is set when a later user input replaced this constraint
    of the same kind: the old constraint is kept, visibly out of date --
    never silently deleted.
    """

    #: "time_range" | "literature_type" | "conditions" | "materials" |
    #: "equipment" | "cost" | "green_chemistry" | "safety" | "data_source" |
    #: "scope" | "output" | "comparison_scope" ...
    kind: str
    value: str
    source: IntentSource = IntentSource.USER_EXPLICIT
    superseded_by: str = ""


class MissingKind(str, Enum):
    """How blocking a gap is. Not every gap stops the whole request."""

    #: The work that depends on it cannot proceed; independent work may.
    REQUIRED = "required"
    #: A sensible default can stand in until the user says otherwise.
    DEFAULTABLE = "defaultable"
    #: Can be confirmed later, during the research itself.
    DEFERRABLE = "deferrable"


class MissingInformation(BaseModel):
    """Something the request needs before part of it can run."""

    #: "ambiguous_identity" | "missing_target" | "missing_second_entity" |
    #: "missing_reactants" | "missing_conditions" | "missing_study" |
    #: "identity_confirmation" | "invalid_structure" ...
    kind: str
    detail: str = ""
    severity: MissingKind = MissingKind.REQUIRED
    entity_name: str = ""


class ResearchIntent(BaseModel):
    """One user request, structured, sourced, and honest about its gaps."""

    #: The user's words, verbatim. Always.
    raw_input: str
    #: A one-sentence restatement of the goal the request implies.
    goal: str = ""
    primary_intent: ResearchIntentKind = ResearchIntentKind.UNKNOWN
    secondary_intents: list[ResearchIntentKind] = Field(default_factory=list)
    source_of_intent: IntentSource = IntentSource.USER_EXPLICIT
    entities: list[IntentEntity] = Field(default_factory=list)
    constraints: list[ResearchConstraint] = Field(default_factory=list)
    missing: list[MissingInformation] = Field(default_factory=list)
    #: The study this request continues or belongs to, when one is settled.
    study_id: str = ""
    continuation: bool = False
    #: The question to ask the user, when the request paused on ambiguity.
    clarification: str = ""
    #: Goals this request replaced, oldest first (a revision keeps history).
    superseded_goals: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)
    #: True once `validate` found no blocking issue.
    validated: bool = False

    def entity(self, name: str) -> IntentEntity | None:
        for item in self.entities:
            if item.name == name:
                return item
        return None


__all__ = [
    "EntityIdentityStatus",
    "EntityRole",
    "IntentEntity",
    "IntentSource",
    "MissingInformation",
    "MissingKind",
    "ResearchConstraint",
    "ResearchIntent",
    "ResearchIntentKind",
]

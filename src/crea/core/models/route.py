"""Candidate synthesis routes and what is known about each step.

The single distinction this module exists to hold:

    "a tool proposed this step"
        is not
    "a paper reports this step"
        is not
    "an experimental procedure was found"
        is not
    "this has been experimentally validated"

A `CandidateRoute` is the first of those and only the first. It becomes
`LITERATURE_SUPPORTED` when literature is found for it, and
`EXPERIMENTALLY_SUPPORTED` only when evidence carrying actual conditions is
attached. Nothing here can skip a rung, and nothing here turns a route search
score into a measure of whether the chemistry works.

No confidence numbers. A retrosynthesis engine's score is that engine's own
ranking heuristic and is recorded under `score_source`; an evidence's strength is
a separate axis. Collapsing them into one number is how "87% likely" ends up
being printed next to a claim nobody checked.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from crea.core.ids import content_hash


class MoleculeRole(str, Enum):
    TARGET = "target"
    PRECURSOR = "precursor"
    REAGENT = "reagent"
    CATALYST = "catalyst"
    SOLVENT = "solvent"
    UNKNOWN = "unknown"


class MoleculeSource(str, Enum):
    USER_INPUT = "user_input"
    COMPUTED = "computed"
    RETROSYNTHESIS = "retrosynthesis"
    LITERATURE = "literature"
    UNKNOWN = "unknown"


class StepStatus(str, Enum):
    """How far a step has actually been supported."""

    CANDIDATE = "candidate"                        # a tool proposed it, nothing else
    LITERATURE_SUPPORTED = "literature_supported"
    EXPERIMENTALLY_SUPPORTED = "experimentally_supported"
    UNVERIFIED = "unverified"
    CONFLICTING = "conflicting"                  # sources disagree


class SupportLevel(str, Enum):
    """The evidence ladder. Ordered by what had to be true, not by strength."""

    NO_SUPPORT = "no_support"
    PARTIAL_SUPPORT = "partial_support"            # a related transformation
    STRUCTURAL_SUPPORT = "structural_support"      # the structures match
    LITERATURE_SUPPORT = "literature_support"      # a paper reports it
    EXPERIMENTAL_SUPPORT = "experimental_support"  # with actual conditions
    USER_DOCUMENT_SUPPORT = "user_document_support"  # in a file the user supplied


class ProviderStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"      # not installed
    NOT_CONFIGURED = "not_configured"  # installed but missing model/stock/config
    FAILED = "failed"                 # ran and errored


class ReactionMatchType(str, Enum):
    """How a candidate reaction relates to the step that was asked about.

    Reaction-level, and deliberately not molecule-level. Molecular similarity
    cannot tell "this makes the product" from "this does something related to
    it", and conflating the two is how a plausible paper becomes a citation
    for a reaction nobody ran.

    Two families live here, and they are not interchangeable. `EXACT`,
    `PARTIAL`, `TRANSFORMATION` and `DISCOVERY` are what the **matcher**
    answers (`crea.services.reaction_matching`): it was handed structures on
    both sides and says how they relate. `DISCOVERY_ONLY` and `NO_MATCH` are
    what the **evidence layer** answers
    (`crea.services.route_evidence`): it says why there is no support, which is
    a different question. A title match gets `DISCOVERY_ONLY` because no
    comparison was possible; a compared structure that does not match gets
    `NO_MATCH` because the comparison ran and came back empty.

    DISCOVERY is a legitimate answer, not a failure: nothing was found, and
    that is worth recording.
    """

    EXACT = "exact"                  # both sides match the step
    PARTIAL = "partial"              # one side matches; the other differs
    TRANSFORMATION = "transformation"  # neither side matches, but it is related
    DISCOVERY = "discovery"          # the matcher ran and found nothing related

    # -- the evidence layer's answers, about *why* there is no support --
    NO_MATCH = "no_match"            # structures existed; they did not match
    DISCOVERY_ONLY = "discovery_only"  # the record carried no structures to
                                      # compare at all (title/abstract/DOI only)


class ScoreSource(str, Enum):
    AIZYNTHFINDER = "aizynthfinder"
    UNKNOWN = "unknown"
    NONE = "none"


class StockAssumption(str, Enum):
    """What the provider believed about purchasability.

    A stock flag is that provider's stock list saying something. It is not a
    vendor quotation, and `UNKNOWN` is a real answer when no stock was given.
    """

    PROVIDER_STOCK_LIST = "provider_stock_list"
    NOT_IN_STOCK_LIST = "not_in_stock_list"
    UNKNOWN = "unknown"


class RouteMolecule(BaseModel):
    """One molecule in a route. A structure, or nothing.

    There is no name->SMILES here: this project has no verified chemical name
    resolver, and attaching a structure to a name would assert an
    identification nobody checked.
    """

    molecule_id: str
    smiles: str = ""
    canonical_smiles: str = ""
    name: str = ""
    role: MoleculeRole = MoleculeRole.UNKNOWN
    source: MoleculeSource = MoleculeSource.UNKNOWN

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.canonical_smiles or self.smiles or self.name or self.molecule_id


class TransformationCandidate(BaseModel):
    """What the provider says changed in a step.

    `transformation_class` is `UNKNOWN` unless the provider supplied one. A model
    may not fill it in from its own chemistry knowledge: that would put an
    unverified reaction class into a structure that otherwise looks measured.
    """

    reactant_smiles: str = ""
    product_smiles: str = ""
    reaction_smiles: str | None = None
    template: str | None = None
    template_source: str | None = None
    transformation_class: str = "UNKNOWN"
    transformation_class_source: str = "UNKNOWN"
    source: str = "UNKNOWN"
    evidence_refs: list[str] = Field(default_factory=list)


class ReactionStep(BaseModel):
    """One step of a route, and what is actually known about it."""

    step_id: str
    step_index: int
    products: list[RouteMolecule] = Field(default_factory=list)
    reactants: list[RouteMolecule] = Field(default_factory=list)
    reagents: list[RouteMolecule] = Field(default_factory=list)
    transformation: TransformationCandidate | None = None

    source: str = "UNKNOWN"
    score: float | None = None
    score_source: ScoreSource = ScoreSource.NONE
    stock_assumptions: dict[str, StockAssumption] = Field(default_factory=dict)

    #: Starts CANDIDATE and only ever moves up through evidence.
    status: StepStatus = StepStatus.CANDIDATE
    support_level: SupportLevel = SupportLevel.NO_SUPPORT
    evidence_refs: list[str] = Field(default_factory=list)
    paper_ids: list[str] = Field(default_factory=list)
    reaction_record_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CandidateRoute(BaseModel):
    """A route a provider proposed. A proposal, on its face."""

    route_id: str
    target: RouteMolecule
    steps: list[ReactionStep] = Field(default_factory=list)

    #: The provider's own heuristic, kept as it came out. Never restated as a
    #: confidence, and never compared across providers.
    score: float | None = None
    score_source: ScoreSource = ScoreSource.NONE

    source: str = "UNKNOWN"
    provider_version: str | None = None
    stock_configuration: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def step(self, step_id: str) -> ReactionStep | None:
        for item in self.steps:
            if item.step_id == step_id:
                return item
        return None


class RetrosynthesisRequest(BaseModel):
    target_smiles: str
    max_routes: int = 3
    max_transforms: int = 6
    search_algorithm: str = "mcts"
    config_file: str | None = None


class ProviderProvenance(BaseModel):
    """What the upstream actually was, kept even when the run produced nothing."""

    provider: str = "UNKNOWN"
    provider_version: str | None = None
    model_version: str | None = None
    configuration: str | None = None
    stock_configuration: str | None = None
    raw_result_hash: str | None = None
    generated_at: str = ""


class ProviderReason(str, Enum):
    """Why a provider did or did not answer, in a form code can branch on.

    `RetrosynthesisResult.reason` is a sentence for a person. This is the same
    fact as a value, so a caller can tell "the engine is not installed here"
    from "the engine is installed but has no stock file" without reading
    English, and so a test can assert the boundary rather than the wording.

    Everything not on the list is `UNKNOWN` rather than a guess. A reason we
    cannot name is not a reason we should invent.
    """

    NONE = "none"                        # it ran
    MISSING_RUNTIME = "missing_runtime"  # no worker, or no interpreter for it
    MISSING_MODEL = "missing_model"      # no expansion policy
    MISSING_STOCK = "missing_stock"      # no purchasable-compound list
    INCOMPATIBLE_DEPENDENCY = "incompatible_dependency"  # imports, but the
                                                         # dependency versions
                                                         # cannot coexist
    WORKER_TIMEOUT = "worker_timeout"
    WORKER_CRASH = "worker_crash"
    INVALID_CONFIGURATION = "invalid_configuration"
    PROTOCOL_ERROR = "protocol_error"    # answered with something unreadable
    UNKNOWN = "unknown"


class RetrosynthesisResult(BaseModel):
    request: RetrosynthesisRequest
    status: ProviderStatus = ProviderStatus.AVAILABLE
    routes: list[CandidateRoute] = Field(default_factory=list)
    provenance: ProviderProvenance = Field(default_factory=ProviderProvenance)
    reason: str = ""
    reason_code: ProviderReason = ProviderReason.NONE
    warnings: list[str] = Field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.status is ProviderStatus.AVAILABLE


class RouteValidationResult(BaseModel):
    """What searching for a step actually turned up.

    `support_level` is the highest rung any check reached. It is never raised by
    the LLM and never inferred from similarity alone.
    """

    step_id: str
    support_level: SupportLevel = SupportLevel.NO_SUPPORT
    evidence_refs: list[str] = Field(default_factory=list)
    paper_ids: list[str] = Field(default_factory=list)
    reaction_record_ids: list[str] = Field(default_factory=list)
    match_kind: str = "none"       # exact_structure | formula | substructure | none
    matched_smiles: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    checked_count: int = 0


ROUTE_ID_VERSION = "route-id-v1"


def route_identity(target_smiles: str, provider: str,
                   step_signatures: list[str]) -> str:
    payload = "|".join([ROUTE_ID_VERSION, provider, target_smiles,
                        ",".join(step_signatures)])
    return content_hash(payload, 16)


def step_identity(target_smiles: str, index: int, reactants: list[str],
                  products: list[str]) -> str:
    payload = "|".join([
        "step-id-v1", target_smiles, str(index),
        ",".join(sorted(reactants)), ",".join(sorted(products)),
    ])
    return content_hash(payload, 16)
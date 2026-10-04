"""Literature service result models.

Orchestration results are modelled as themselves. Provider runs, failures, merge
outcomes and access evidence are first-class fields, not strings appended to a
paper's notes, because "your search found nothing" and "every provider failed"
are different answers and the difference has to survive to the caller.
"""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field

from crea.core.models.literature import Paper
from crea.core.models.merge import AccessReconciliation, MergeResult


class ProviderRunStatus(str, Enum):
    OK = "ok"                    # ran and answered
    SKIPPED = "skipped"          # deliberately not asked to
    UNAVAILABLE = "unavailable"  # cannot run here (e.g. no credential)
    ERROR = "error"              # ran and failed


class SearchOutcome(str, Enum):
    """The top-level answer, and the thing most easily got wrong.

    `NO_RESULTS` and `ALL_PROVIDERS_FAILED` are different facts. A caller that
    cannot tell them apart will show an empty list when it should show an
    error, and the empty list looks like an answer.
    """

    SUCCESS = "success"                        # every requested provider ran
    PARTIAL_SUCCESS = "partial_success"        # some ran, some did not
    NO_RESULTS = "no_results"                  # all ran; none of them found anything
    ALL_PROVIDERS_FAILED = "all_providers_failed"


class ProviderRun(BaseModel):
    name: str
    status: ProviderRunStatus
    availability: str = ""
    records_found: int = 0
    error_type: str | None = None
    error: str | None = None
    duration_ms: int = 0


class LiteratureSearchRequest(BaseModel):
    query: str = ""
    limit: int = 20
    #: Access enrichment is opt-in. Unpaywall is one call per DOI, and a search
    #: that quietly spends 40 of them turns a metadata lookup into a budget
    #: decision nobody asked for.
    include_access: bool = False
    #: Hard ceiling on Unpaywall calls, whatever the paper count.
    max_access_calls: int = 25
    providers: tuple[str, ...] = ("openalex", "crossref")
    access_provider: str = "unpaywall"


class AccessCall(BaseModel):
    doi: str
    outcome: str
    error: str | None = None


class LiteratureSearchResult(BaseModel):
    request: LiteratureSearchRequest
    outcome: SearchOutcome
    papers: list[Paper] = Field(default_factory=list)
    provider_runs: list[ProviderRun] = Field(default_factory=list)
    merge_policy_version: str = ""
    merged_count: int = 0
    unresolved_count: int = 0
    merges: list[MergeResult] = Field(default_factory=list)
    access: list[AccessReconciliation] = Field(default_factory=list)
    access_calls: list[AccessCall] = Field(default_factory=list)
    access_skipped_reason: str = ""
    warnings: list[str] = Field(default_factory=list)
    generated_at: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in (SearchOutcome.SUCCESS, SearchOutcome.PARTIAL_SUCCESS,
                                SearchOutcome.NO_RESULTS)


class DoiLookupRequest(BaseModel):
    doi: str
    include_access: bool = False
    providers: tuple[str, ...] = ("openalex", "crossref")


class DoiLookupResult(BaseModel):
    request: DoiLookupRequest
    found: bool = False
    identity: str = ""
    identity_basis: str = ""
    paper: Paper | None = None
    merge: MergeResult | None = None
    provider_runs: list[ProviderRun] = Field(default_factory=list)
    access: list[AccessReconciliation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    generated_at: str = ""

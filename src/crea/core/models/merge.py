"""Cross-provider merge: data model. Pure data, no I/O, no policy logic.

The question this exists to answer is not "which provider wins". It is "for
each field, what did we select, who said it, what else was offered, and did
they disagree". A merge that leaves only `title = "..."` behind has thrown away
the only thing that makes a record checkable.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from crea.core.models.literature import Author, Paper


class IdentityStatus(str, Enum):
    """Whether we believe these records are the same work.

    Deliberately boring. DOI equality is the only positive evidence used here.
    Title similarity already exists in `services.dedup` with a full audit
    trail, and a second, looser identity algorithm inside the merge layer would
    be a way for two records to become one without anyone deciding so.
    """

    DOI_MATCH = "doi_match"
    NO_DOI = "no_doi"
    DOI_CONFLICT = "doi_conflict"
    IDENTITY_UNKNOWN = "identity_unknown"


class FieldState(str, Enum):
    AGREED = "agreed"        # every source that had a value agrees
    CONFLICT = "conflict"    # sources offered different values
    MISSING = "missing"      # no source had a value
    UNIONED = "unioned"      # deliberately combined, e.g. identifier sets


class FieldAlternative(BaseModel):
    """A value that was offered and not selected, with who offered it."""

    value: str
    source: str
    note: str = ""


class FieldResolution(BaseModel):
    field: str
    state: FieldState
    selected: str | None = None
    selected_from: list[str] = Field(default_factory=list)
    alternatives: list[FieldAlternative] = Field(default_factory=list)
    conflict: str = ""


class ProviderRecord(BaseModel):
    """One input record's own provenance, carried through the merge intact.

    A merged paper is derived from these; it does not replace them. If the
    merge policy is changed next month, the evidence to re-run it is still here.
    """

    source: str
    source_id: str = ""
    paper_id: str = ""
    retrieved_at: str = ""
    raw_hashes: dict[str, str] = Field(default_factory=dict)
    doi: str | None = None


class MergePolicy(BaseModel):
    """Per-field source priority, explicit and versioned.

    There is deliberately no global `openalex > crossref` rule. Sources differ
    in which fields they are authoritative for: Crossref is the registration
    agency and is the better record of a title and a journal; neither is
    authoritative about the other. A single global ordering would encode one
    provider's trustworthiness where it has none.
    """

    version: str = "merge-v1"
    field_priority: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    def rank(self, field_name: str, source: str) -> int | None:
        order = self.field_priority.get(field_name)
        if not order or source not in order:
            return None
        return order.index(source)


class MergeResult(BaseModel):
    identity: IdentityStatus = IdentityStatus.IDENTITY_UNKNOWN
    identity_basis: str = ""
    identity_conflict: str = ""
    paper: Paper | None = None
    field_resolutions: list[FieldResolution] = Field(default_factory=list)
    provider_records: list[ProviderRecord] = Field(default_factory=list)
    #: Author lists kept whole, per source. Author order carries meaning and
    #: two sources disagreeing about a list is a fact, not a set to union.
    author_lists: dict[str, list[Author]] = Field(default_factory=dict)
    conflicts: list[str] = Field(default_factory=list)
    policy_version: str = "merge-v1"
    #: True only when records were actually combined. Set by the merge, not
    #: inferred from how many inputs arrived.
    merged: bool = False

    def resolution(self, field_name: str) -> FieldResolution | None:
        for r in self.field_resolutions:
            if r.field == field_name:
                return r
        return None


class ReconciliationInput(BaseModel):
    """The evidence one access assessment is built from."""

    source: str
    source_id: str = ""
    oa_status: str
    oa_status_raw: str | None = None      # the upstream string, before mapping
    license_spdx: str | None = None
    license_name: str = "UNKNOWN"
    license_url: str | None = None
    license_content_version: str = "UNKNOWN"
    fulltext_url: str | None = None
    url_type: str = "unknown"
    notes: list[str] = Field(default_factory=list)


class AccessConflict(BaseModel):
    field: str
    description: str
    values: dict[str, str] = Field(default_factory=dict)


class AccessReconciliation(BaseModel):
    """Every independent piece of access evidence we hold, and what we concluded.

    The point is not to crown a winner. It is to be able to answer "what do we
    actually know, and is that enough to permit retrieval" -- and to be able to
    show the work.
    """

    evidence: list[ReconciliationInput] = Field(default_factory=list)
    selected_source: str = ""
    selected_assessment: dict = Field(default_factory=dict)
    permission_basis: str = ""
    conflicts: list[AccessConflict] = Field(default_factory=list)
    policy_version: str = "access-v1"
    decisive: str = ""    # which single fact decided the outcome, if one did



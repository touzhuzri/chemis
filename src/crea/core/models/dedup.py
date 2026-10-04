"""Deduplication data model. Pure data, no I/O, no matching logic.

The point of modelling every *gate* rather than a final boolean is that a
merge decision has to remain reviewable after the fact. `deduplicate()` records
which checks passed, which were unknown, and why it ended where it did -- so a
wrong merge can be diagnosed rather than merely regretted.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from crea.core.models.literature import Paper


class GateStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"      # the input needed to decide was not available


class DedupPass(str, Enum):
    DOI_EXACT = "doi_exact"
    TITLE_YEAR_EXACT = "title_year_exact"
    TITLE_FUZZY = "title_fuzzy"


class DedupVerdict(str, Enum):
    MERGE = "merge"
    KEEP_SEPARATE = "keep_separate"


class DedupReason(str, Enum):
    EXACT_DOI = "exact_doi"
    EXACT_TITLE_AND_YEAR = "exact_title_and_year"
    ALL_GATES_PASSED = "all_gates_passed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    BELOW_SIMILARITY_THRESHOLD = "below_similarity_threshold"
    OUTSIDE_YEAR_WINDOW = "outside_year_window"
    JOURNAL_MISMATCH = "journal_mismatch"
    AUTHORS_DISJOINT = "authors_disjoint"
    NOT_SCREENED = "not_screened"


class DedupDecision(BaseModel):
    """One screening outcome, recorded whether or not a merge happened."""

    decision_id: str
    record_a: str
    record_b: str
    pass_name: DedupPass
    decision: DedupVerdict
    reason: DedupReason

    # per-gate status, kept individually so a decision can be explained
    title_check: GateStatus = GateStatus.UNKNOWN
    year_check: GateStatus = GateStatus.UNKNOWN
    journal_check: GateStatus = GateStatus.UNKNOWN
    author_check: GateStatus = GateStatus.UNKNOWN

    # the measurements the gates were evaluated against
    normalized_title_similarity: float | None = None
    year_difference: int | None = None
    journal_match: bool | None = None
    authors_overlap: float | None = None   # auxiliary corroboration only

    detail: str = ""
    threshold_version: str = "dedup-v1"


class DedupStats(BaseModel):
    input_count: int = 0
    output_count: int = 0
    exact_doi_merges: int = 0
    title_year_merges: int = 0
    fuzzy_merges: int = 0
    kept_separate_on_insufficient_evidence: int = 0
    decisions: list[DedupDecision] = Field(default_factory=list)


class DedupResult(BaseModel):
    papers: list[Paper] = Field(default_factory=list)
    groups: list[list[str]] = Field(default_factory=list)
    stats: DedupStats = Field(default_factory=DedupStats)


#: Version stamp written onto every decision. When a threshold changes, past
#: decisions remain attributable to the ruleset that produced them.
DEDUP_THRESHOLD_VERSION = "dedup-v1"
TITLE_SIMILARITY_THRESHOLD = 0.92
YEAR_WINDOW = 1


class DedupThresholds(BaseModel):
    title_similarity: float = TITLE_SIMILARITY_THRESHOLD
    year_window: int = YEAR_WINDOW
    version: str = DEDUP_THRESHOLD_VERSION
    merge_on_exact_doi: Literal[True] = True

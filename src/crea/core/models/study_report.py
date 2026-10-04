"""A research session's report (CREA Stage 7.3).

The readable expression of a `ResearchStudy`: what was asked, what was
examined, what the evidence and literature say, what routes were explored, and
what remains open. It is assembled **read-only** from what the session already
holds -- references, artifacts, events -- and it never re-runs chemistry, a
search, an agent or an LLM.

Name note: this is `StudyReport`, not `ResearchReport` -- `core.models.research`
already has a `ResearchReport` (a research *task's* report). This one reports a
*study*, and is stored as a `StudyArtifact` of type `RESEARCH_REPORT`, not in a
new table.

A report organises results; it never declares them audited (§六十四). It may
carry warnings; it may not print `VERIFIED` / `VALIDATED`.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.provenance import utc_now_iso

#: The explicit report schema version (§七/§四十四).
REPORT_VERSION = "crea-research-report-v1"


class SourceRefType(str, Enum):
    """What a section's source pointer points at (§六十二)."""

    MOLECULE = "molecule"
    REACTION = "reaction"
    EVIDENCE = "evidence"
    LITERATURE = "literature"
    ROUTE = "route"
    ARTIFACT = "artifact"


class ReportSourceRef(BaseModel):
    """A pointer from a section to the knowledge it rests on.

    This is the source graph a later audit reads (§62): every fact-bearing
    section names what it came from, or carries a warning saying it could not.
    """

    type: SourceRefType
    id: str
    label: str = ""


class ReportSection(BaseModel):
    section_id: str
    title: str
    kind: str = "narrative"
    summary: str = ""
    lines: list[str] = Field(default_factory=list)
    source_refs: list[ReportSourceRef] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ReportReference(BaseModel):
    """One cited work, deduped by paper id / normalized DOI (§三十五)."""

    ref_id: str            # "[P1]", stable per report
    paper_id: str = ""
    doi: str = ""
    title: str = ""
    journal: str = ""
    year: int | None = None
    authors: list[str] = Field(default_factory=list)


class ReportWarning(BaseModel):
    code: str
    message: str
    ref: str = ""


class StudyReport(BaseModel):
    report_id: str
    report_version: str = REPORT_VERSION
    study_id: str
    title: str = ""
    question: str = ""
    status: str = "generated"    # generated only; never "verified" (§六十四)
    generated_at: str = Field(default_factory=utc_now_iso)
    scope: dict = Field(default_factory=dict)
    sections: list[ReportSection] = Field(default_factory=list)
    references: list[ReportReference] = Field(default_factory=list)
    warnings: list[ReportWarning] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


__all__ = [
    "REPORT_VERSION",
    "ReportReference",
    "ReportSection",
    "ReportSourceRef",
    "ReportWarning",
    "SourceRefType",
    "StudyReport",
]

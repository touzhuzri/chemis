"""Final audit of a research session (CREA Stage 7.4).

The last stage of the CREA V1 line, and the boundary it holds is its whole
point:

    Audit != Scientific Truth

An audit can show that the record is *internally* consistent -- references
resolve, evidence is traceable, AI claims are grounded, the report cites the
data it describes, statuses agree. It cannot show that a reaction works, a
route is feasible, a yield is a number, or a paper's conclusion is true.

It is deterministic and offline (§六十八/§六十九/§七十): no network, no LLM,
no repair. It reads and reports; it never fixes (§六十七/§八十八). It is
stored as a `StudyArtifact` of type `FINAL_AUDIT` (§七十二), never a new table.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.provenance import utc_now_iso

#: The audit's own schema version, separate from the report's (§七十三).
AUDIT_VERSION = "crea-final-audit-v1"


class AuditCheckStatus(str, Enum):
    """One check's outcome (§六)."""

    PASS = "pass"
    WARN = "warn"    # a known limitation or missing-but-not-broken data
    FAIL = "fail"    # a dangling reference or an invalid relation


class AuditStatus(str, Enum):
    """The whole audit's outcome (§六)."""

    PASS = "pass"
    PASS_WITH_WARNINGS = "pass_with_warnings"
    FAIL = "fail"


class AuditCheck(BaseModel):
    check_id: str
    title: str
    status: AuditCheckStatus = AuditCheckStatus.PASS
    summary: str = ""
    details: list[str] = Field(default_factory=list)
    refs: list[str] = Field(default_factory=list)


class FinalAuditResult(BaseModel):
    audit_id: str
    audit_version: str = AUDIT_VERSION
    study_id: str
    #: The report version this audit read, so Report v1 -> Audit A1 (§七十四).
    report_artifact_id: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    status: AuditStatus = AuditStatus.PASS
    checks: list[AuditCheck] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    #: The standing statement: this is not a scientific verdict (§九十).
    statement: str = (
        "The research record passed CREA's structural, provenance and "
        "evidence-linking checks. This is not a scientific verification: it "
        "does not establish that any reaction, route or citation is "
        "experimentally correct.")


__all__ = [
    "AUDIT_VERSION",
    "AuditCheck",
    "AuditCheckStatus",
    "AuditStatus",
    "FinalAuditResult",
]

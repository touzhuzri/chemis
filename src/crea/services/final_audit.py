"""Final audit service (CREA Stage 7.4).

Reads a research session (and one of its reports) and reports whether the
record is internally consistent. It is deterministic, offline, and read-only:
it never searches, never calls a model, never repairs, and never writes except
when the caller saves the result as an artifact.

Known limitations (a missing product, an unknown permission, no route) are
`WARN`, never `FAIL` -- an unknown is not a corruption (§六十三/§六十四/§六十五).
A dangling reference -- evidence citing literature that does not exist, a
report citing evidence that does not exist -- is `FAIL`.
"""

from __future__ import annotations

from crea.core.ids import content_hash, normalize_doi
from crea.core.models.final_audit import (
    AUDIT_VERSION,
    AuditCheck,
    AuditCheckStatus,
    AuditStatus,
    FinalAuditResult,
)
from crea.core.models.research_study import ArtifactType, ReferenceKind
from crea.core.provenance import utc_now_iso


class _Checks:
    """A small accumulator; each check is independent and order-stable."""

    def __init__(self) -> None:
        self.items: list[AuditCheck] = []

    def add(self, check_id: str, title: str, status: AuditCheckStatus,
            summary: str = "", details=(), refs=()) -> None:
        self.items.append(AuditCheck(
            check_id=check_id, title=title, status=status, summary=summary,
            details=[str(d) for d in details], refs=[str(r) for r in refs]))


class FinalAuditService:
    """Audit a research session's internal consistency. Deterministic, offline."""

    def __init__(self, kb) -> None:
        self._kb = kb

    def audit(self, study_id: str, report_artifact_id: str = "") -> FinalAuditResult:
        from crea.core.errors import ResearchStudyError

        detail = self._kb.get_study_detail(study_id)
        if detail is None:
            raise ResearchStudyError(
                "STUDY_NOT_FOUND", f"no research session {study_id!r}")
        study = detail.study

        checks = _Checks()
        self._study_integrity(checks, study, detail)
        refs = self._reference_integrity(checks, detail)
        self._molecule_identity(checks, refs)
        self._reaction_references(checks, refs)
        evidence = self._evidence_chain(checks, refs)
        self._evidence_dedup(checks, evidence)
        self._evidence_locator_and_provenance(checks, evidence)
        literature = self._literature_integrity(checks, refs, evidence)
        self._literature_evidence_reverse(checks, evidence, literature)
        self._fulltext_permission(checks, literature)
        self._route_consistency(checks, detail)
        self._ai_grounding(checks, detail)
        self._workflow_integrity(checks, study_id)
        report = self._report_integrity(checks, detail, report_artifact_id)
        self._timeline_consistency(checks, detail, study)

        passed = sum(1 for c in checks.items if c.status is AuditCheckStatus.PASS)
        warns = [c for c in checks.items if c.status is AuditCheckStatus.WARN]
        fails = [c for c in checks.items if c.status is AuditCheckStatus.FAIL]
        status = (AuditStatus.FAIL if fails
                  else AuditStatus.PASS_WITH_WARNINGS if warns
                  else AuditStatus.PASS)

        warnings = [f"{c.check_id}: {c.summary or c.title}" for c in warns]
        failures = [f"{c.check_id}: {c.summary or c.title}" for c in fails]
        return FinalAuditResult(
            audit_id="audit-" + content_hash(
                f"{study_id}|{report.artifact_id if report else ''}|"
                f"{len(detail.artifacts)}|{len(detail.references)}", 16),
            audit_version=AUDIT_VERSION, study_id=study_id,
            report_artifact_id=(report.artifact_id if report else ""),
            created_at=utc_now_iso(), status=status, checks=checks.items,
            summary={"total_checks": len(checks.items), "passed": passed,
                     "warnings": len(warns), "failures": len(fails)},
            warnings=warnings, failures=failures)

    # -- checks -------------------------------------------------------------

    def _study_integrity(self, checks, study, detail) -> None:
        problems = []
        if study.updated_at < study.created_at:
            problems.append("updated_at is before created_at")
        checks.add("study_integrity", "Study integrity",
                   AuditCheckStatus.FAIL if problems else AuditCheckStatus.PASS,
                   summary="; ".join(problems) or "session metadata is consistent",
                   details=problems, refs=[study.study_id])

    def _reference_integrity(self, checks, detail):
        """Every reference resolves; duplicates are a warning (§十五)."""
        missing: list[str] = []
        seen: dict[tuple, str] = {}
        duplicates: list[str] = []
        for ref in detail.references:
            if not self._resolves(ref.kind, ref.object_id):
                missing.append(f"{ref.kind.value} {ref.object_id}")
            key = (ref.kind.value, ref.object_id)
            if key in seen:
                duplicates.append(f"{ref.kind.value} {ref.object_id}")
            seen[key] = ref.reference_id
        status = (AuditCheckStatus.FAIL if missing
                  else AuditCheckStatus.WARN if duplicates
                  else AuditCheckStatus.PASS)
        checks.add(
            "reference_integrity", "Reference integrity", status,
            summary=("; ".join(missing) if missing
                     else f"{len(duplicates)} duplicate reference(s)" if duplicates
                     else f"{len(detail.references)} reference(s) resolve"),
            details=missing + duplicates, refs=[r.reference_id for r in detail.references])
        return [r for r in detail.references if r.kind is not ReferenceKind.LITERATURE]

    def _resolves(self, kind: ReferenceKind, object_id: str) -> bool:
        from crea.repositories.errors import NotFound

        try:
            if kind is ReferenceKind.MOLECULE:
                return self._kb.get_entity(object_id) is not None
            if kind is ReferenceKind.REACTION:
                from crea.services.reaction_library import get_template

                return get_template(object_id) is not None
            if kind is ReferenceKind.LITERATURE:
                return self._kb.store.get_paper(object_id) is not None
            if kind is ReferenceKind.EVIDENCE:
                return self._kb.get_reaction_evidence(object_id) is not None
        except NotFound:
            return False
        return False

    def _molecule_identity(self, checks, refs) -> None:
        problems, weak = [], []
        for ref in [r for r in refs if r.kind is ReferenceKind.MOLECULE]:
            try:
                entity = self._kb.get_entity(ref.object_id)
            except Exception:  # noqa: BLE001 - resolution already reported
                continue
            if not entity.entity_id:
                problems.append(f"{ref.object_id}: no entity id")
            if not entity.inchikey:
                weak.append(f"{ref.object_id}: no Standard InChIKey")
            if not entity.canonical_smiles:
                weak.append(f"{ref.object_id}: no canonical SMILES")
        checks.add("molecule_identity", "Chemical identity",
                   AuditCheckStatus.FAIL if problems
                   else AuditCheckStatus.WARN if weak else AuditCheckStatus.PASS,
                   summary="; ".join(problems or weak) or "identities present",
                   details=problems + weak)

    def _reaction_references(self, checks, refs) -> None:
        problems = []
        for ref in [r for r in refs if r.kind is ReferenceKind.REACTION]:
            from crea.services.reaction_library import get_template

            template = get_template(ref.object_id)
            if template is None:
                problems.append(f"{ref.object_id}: not in the reaction library")
            elif not template.substrate_patterns:
                problems.append(f"{ref.object_id}: no substrate pattern")
        checks.add("reaction_references", "Reaction references",
                   AuditCheckStatus.FAIL if problems else AuditCheckStatus.PASS,
                   summary="; ".join(problems) or "reaction templates are valid",
                   details=problems)

    def _evidence_chain(self, checks, refs):
        """Evidence resolves; literature sources exist; curated stays curated."""
        dangling, upgraded, records = [], [], []
        for ref in [r for r in refs if r.kind is ReferenceKind.EVIDENCE]:
            record = self._kb.get_reaction_evidence(ref.object_id)
            if record is None:
                dangling.append(f"{ref.object_id}: evidence not found")
                continue
            records.append(record)
            src = record.source
            if src.source_type.value == "literature":
                if not src.source_id or self._kb.store.get_paper(src.source_id) is None:
                    dangling.append(
                        f"{record.evidence_id}: cites literature "
                        f"{src.source_id!r} that does not exist")
            if src.source_type.value == "curated" and record.status.value != "curated":
                upgraded.append(
                    f"{record.evidence_id}: curated evidence marked "
                    f"{record.status.value}")
        status = (AuditCheckStatus.FAIL if dangling or upgraded
                  else AuditCheckStatus.PASS)
        checks.add("evidence_chain", "Evidence linkage", status,
                   summary=("; ".join(dangling or upgraded)
                            or f"{len(records)} evidence record(s) traceable"),
                   details=dangling + upgraded,
                   refs=[r.evidence_id for r in records])
        return records

    def _evidence_dedup(self, checks, records) -> None:
        seen: dict[tuple, str] = {}
        duplicates = []
        for record in records:
            key = (record.reaction_template_id, record.source.source_type.value,
                   record.source.source_id, record.evidence_type.value,
                   record.support_scope)
            if key in seen:
                duplicates.append(f"duplicate evidence {record.evidence_id}")
            seen[key] = record.evidence_id
        checks.add("evidence_dedup", "Evidence deduplication",
                   AuditCheckStatus.WARN if duplicates else AuditCheckStatus.PASS,
                   summary="; ".join(duplicates) or "no duplicate evidence",
                   details=duplicates)

    def _evidence_locator_and_provenance(self, checks, records) -> None:
        bad_locator, no_provenance = [], []
        _known = {"page", "section", "figure", "table", "scheme",
                  "supporting_information"}
        for record in records:
            locator = record.source.locator
            if locator is not None:
                data = locator.model_dump(mode="json")
                if set(data) - _known:
                    bad_locator.append(f"{record.evidence_id}: unknown locator field")
                if locator.page is not None and not isinstance(locator.page, int):
                    bad_locator.append(f"{record.evidence_id}: page is not an integer")
            if record.provenance is None:
                no_provenance.append(record.evidence_id)
        checks.add("evidence_locator", "Evidence locator",
                   AuditCheckStatus.FAIL if bad_locator else AuditCheckStatus.PASS,
                   summary="; ".join(bad_locator) or "locators are well-formed",
                   details=bad_locator)
        checks.add("evidence_provenance", "Evidence provenance",
                   AuditCheckStatus.WARN if no_provenance else AuditCheckStatus.PASS,
                   summary=(f"{len(no_provenance)} evidence record(s) carry no "
                            f"provenance" if no_provenance else "provenance present"),
                   details=no_provenance)

    def _literature_integrity(self, checks, refs, records):
        papers, missing, bad_doi = [], [], []
        seen_doi: dict[str, str] = {}
        duplicate_doi = []
        ids = set()
        for ref in [r for r in refs if r.kind is ReferenceKind.LITERATURE]:
            ids.add(ref.object_id)
        for record in records:
            if record.source.source_type.value == "literature" and record.source.source_id:
                ids.add(record.source.source_id)
        for paper_id in sorted(ids):
            paper = self._kb.store.get_paper(paper_id)
            if paper is None:
                missing.append(paper_id)
                continue
            papers.append(paper)
            if paper.doi:
                normalized = normalize_doi(paper.doi)
                if normalized != paper.doi:
                    bad_doi.append(f"{paper.paper_id}: DOI not normalized")
                if normalized in seen_doi:
                    duplicate_doi.append(f"duplicate DOI {normalized}")
                seen_doi[normalized] = paper.paper_id
        status = (AuditCheckStatus.FAIL if missing
                  else AuditCheckStatus.WARN if (bad_doi or duplicate_doi)
                  else AuditCheckStatus.PASS)
        checks.add("literature_integrity", "Literature integrity", status,
                   summary=("; ".join(missing or bad_doi or duplicate_doi)
                            or f"{len(papers)} paper(s) resolve"),
                   details=missing + bad_doi + duplicate_doi,
                   refs=[p.paper_id for p in papers])
        return papers

    def _literature_evidence_reverse(self, checks, records, papers) -> None:
        """Forward evidence -> paper must be found by the reverse query (§二十七)."""
        from crea.services.reaction_evidence import ReactionEvidenceService

        service = ReactionEvidenceService(kb=self._kb)
        problems = []
        for record in records:
            if record.source.source_type.value != "literature":
                continue
            found = service.evidence_for_literature(record.source.source_id)
            if not any(e.evidence_id == record.evidence_id for e in found):
                problems.append(
                    f"{record.evidence_id}: not found by reverse lookup on "
                    f"{record.source.source_id!r}")
        checks.add("literature_evidence_reverse", "Literature ↔ evidence relation",
                   AuditCheckStatus.FAIL if problems else AuditCheckStatus.PASS,
                   summary="; ".join(problems) or "forward and reverse agree",
                   details=problems)

    def _fulltext_permission(self, checks, papers) -> None:
        """Permission is independent of evidence; unknown is a warning (§二十九)."""
        unknown = [p.paper_id for p in papers
                   if p.access.permission.value == "unknown"]
        checks.add("fulltext_permission", "Fulltext permission independence",
                   AuditCheckStatus.WARN if unknown else AuditCheckStatus.PASS,
                   summary=(f"{len(unknown)} paper(s) have unknown fulltext "
                            f"permission (independent of evidence validity)"
                            if unknown else "permissions stated or not required"),
                   details=[f"{pid}: permission unknown" for pid in unknown])

    def _route_consistency(self, checks, detail) -> None:
        routes = [a for a in detail.artifacts
                  if a.artifact_type is ArtifactType.ROUTE_EXPLORATION]
        problems, warns = [], []
        for artifact in routes:
            content = artifact.content or {}
            for key in ("complete", "partial", "blocked"):
                if key not in content:
                    problems.append(f"{artifact.artifact_id}: missing {key!r}")
            if not (content.get("complete") or []):
                warns.append("No deterministic route found in current knowledge")
        status = (AuditCheckStatus.FAIL if problems
                  else AuditCheckStatus.WARN if warns else AuditCheckStatus.PASS)
        checks.add("route_consistency", "Route consistency", status,
                   summary="; ".join(problems or warns) or "route statuses intact",
                   details=problems + warns)

    def _ai_grounding(self, checks, detail) -> None:
        """AI artifacts reference only real objects (§四十四-§四十七)."""
        from crea.services.reaction_library import get_template

        problems: list[str] = []
        for artifact in detail.artifacts:
            if artifact.artifact_type not in (
                    ArtifactType.MODIFICATION_ANALYSIS, ArtifactType.ROUTE_REASONING,
                    ArtifactType.AGENT_RESULT):
                continue
            for tid in _ids_in(artifact.content, "reaction_template_ids",
                               "reaction_ids", "reaction_template_id"):
                if get_template(tid) is None:
                    problems.append(f"{artifact.artifact_id}: unknown reaction {tid!r}")
            for pid in _ids_in(artifact.content, "literature_ids", "paper_id"):
                if self._kb.store.get_paper(pid) is None:
                    problems.append(f"{artifact.artifact_id}: unknown paper {pid!r}")
        checks.add("ai_grounding", "AI grounding",
                   AuditCheckStatus.FAIL if problems else AuditCheckStatus.PASS,
                   summary="; ".join(problems) or "AI references are grounded",
                   details=problems)

    def _workflow_integrity(self, checks, study_id: str) -> None:
        workflows = self._kb.list_workflows(study_id)
        problems, warns = [], []
        for workflow in workflows:
            tasks = self._kb.list_tasks(workflow.workflow_id)
            ids = {t.task_id for t in tasks}
            for task in tasks:
                for dep in task.depends_on:
                    if dep not in ids:
                        problems.append(f"{task.task_id}: depends on missing task")
                if task.status.value in ("blocked", "failed"):
                    warns.append(f"{task.task_type.value} {task.status.value}")
        status = (AuditCheckStatus.FAIL if problems
                  else AuditCheckStatus.WARN if warns else AuditCheckStatus.PASS)
        checks.add("workflow_integrity", "Workflow integrity", status,
                   summary="; ".join(problems or warns) or "workflows are consistent",
                   details=problems + warns)

    def _report_integrity(self, checks, detail, report_artifact_id: str):
        """The audited report's source refs and references resolve (§四十八-§五十)."""
        reports = [a for a in detail.artifacts
                   if a.artifact_type is ArtifactType.RESEARCH_REPORT]
        if report_artifact_id:
            reports = [a for a in reports if a.artifact_id == report_artifact_id]
        if not reports:
            checks.add("report_integrity", "Report source integrity",
                       AuditCheckStatus.WARN,
                       summary="no report artifact to audit")
            return None
        artifact = reports[-1]
        from crea.core.models.study_report import StudyReport

        report = StudyReport.model_validate(artifact.content)
        problems = []
        known_artifacts = {a.artifact_id for a in detail.artifacts}
        for section in report.sections:
            for ref in section.source_refs:
                if not self._source_resolves(ref.type.value, ref.id, known_artifacts):
                    problems.append(f"{section.section_id} → {ref.type.value} {ref.id}")
        for reference in report.references:
            if not reference.paper_id or self._kb.store.get_paper(
                    reference.paper_id) is None:
                problems.append(f"reference {reference.ref_id} → missing paper")
        checks.add("report_integrity", "Report source integrity",
                   AuditCheckStatus.FAIL if problems else AuditCheckStatus.PASS,
                   summary="; ".join(problems) or "report sources resolve",
                   details=problems, refs=[artifact.artifact_id])
        return artifact

    def _source_resolves(self, ref_type: str, object_id: str,
                          known_artifacts: set) -> bool:
        from crea.services.reaction_library import get_template

        if ref_type == "molecule":
            try:
                return self._kb.get_entity(object_id) is not None
            except Exception:  # noqa: BLE001
                return False
        if ref_type == "reaction":
            return get_template(object_id) is not None
        if ref_type == "evidence":
            return self._kb.get_reaction_evidence(object_id) is not None
        if ref_type == "literature":
            return self._kb.store.get_paper(object_id) is not None
        if ref_type == "artifact":
            # a report may only point at an artifact this same session holds
            return object_id in known_artifacts
        return True

    def _timeline_consistency(self, checks, detail, study) -> None:
        problems = [f"{e.event_id} predates the study"
                    for e in detail.events if e.at < study.created_at]
        checks.add("timeline_consistency", "Timeline consistency",
                   AuditCheckStatus.FAIL if problems else AuditCheckStatus.PASS,
                   summary="; ".join(problems) or "timeline is consistent",
                   details=problems)


def _ids_in(value, *keys) -> list[str]:
    """Every string under the named keys (recursively) -- ids an AI cited."""
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if key in keys:
                    if isinstance(item, str):
                        out.append(item)
                    elif isinstance(item, list):
                        out.extend(str(i) for i in item if isinstance(i, str))
                else:
                    walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return out


__all__ = ["FinalAuditService"]

"""Study report builder (CREA Stage 7.3).

Assembles a `StudyReport` from what a research session already holds. It is
**read-only** (§四十一/§四十二): it does not recompute chemistry, run a search,
call an agent or an LLM, or modify the study. The only write anywhere in this
stage is the caller saving the report as a `StudyArtifact`.

Deterministic assembly (§四十五): a fixed section order, references sorted by
paper id, source refs in a stable order -- no reliance on database order.
"""

from __future__ import annotations

from crea.core.ids import content_hash
from crea.core.models.research_study import ArtifactType, ReferenceKind, StudyEventType
from crea.core.models.study_report import (
    REPORT_VERSION,
    ReportReference,
    ReportSection,
    ReportSourceRef,
    ReportWarning,
    SourceRefType,
    StudyReport,
)
from crea.core.provenance import utc_now_iso

#: The section order is fixed so the same study always assembles the same way.
_SECTION_ORDER = (
    "question", "scope", "molecules", "reactions", "evidence",
    "routes", "analyses", "workflows", "notes", "limitations",
    "open_questions", "references",
)

_ANALYSIS_ARTIFACTS = {
    ArtifactType.MODIFICATION_ANALYSIS: "Modification analysis",
    ArtifactType.ROUTE_REASONING: "Route reasoning",
    ArtifactType.AGENT_RESULT: "Agent answer",
}


class StudyReportBuilder:
    """Build a readable report for a research session, read-only."""

    def __init__(self, kb) -> None:
        self._kb = kb

    def build(self, study_id: str) -> StudyReport:
        detail = self._kb.get_study_detail(study_id)
        if detail is None:
            from crea.core.errors import ResearchStudyError

            raise ResearchStudyError(
                "STUDY_NOT_FOUND", f"no research session {study_id!r}")
        study = detail.study

        sections: list[ReportSection] = []
        warnings: list[ReportWarning] = []
        limitations: list[str] = []
        references: list[ReportReference] = []
        ref_index = _RefIndex(references)

        # -- 1. the question -------------------------------------------------
        sections.append(ReportSection(
            section_id="question", title="Research Question", kind="question",
            summary=study.question or "（未记录主问题）"))

        # -- 2. scope --------------------------------------------------------
        by_kind: dict[str, int] = {}
        for ref in detail.references:
            by_kind[ref.kind.value] = by_kind.get(ref.kind.value, 0) + 1
        sections.append(ReportSection(
            section_id="scope", title="Scope", kind="scope",
            lines=[f"{kind}: {count}" for kind, count in sorted(by_kind.items())]
                  + [f"artifacts: {len(detail.artifacts)}",
                     f"events: {len(detail.events)}"],
            summary="本报告范围来自该 session 的引用、事件与 artifact。"))

        # -- 3. molecules (from references; data reused, never recomputed) ---
        sections.append(self._molecules_section(detail, warnings))

        # -- 4. reactions ----------------------------------------------------
        sections.append(self._reactions_section(detail, warnings))

        # -- 5. evidence & literature ---------------------------------------
        ev_section, refs_ = self._evidence_section(detail, ref_index, warnings)
        sections.append(ev_section)

        # -- 6. routes -------------------------------------------------------
        sections.append(self._routes_section(detail, limitations))

        # -- 7. AI analyses (existing artifacts quoted as-is, never re-run) --
        sections.append(self._analyses_section(detail, limitations))

        # -- 8. workflows ----------------------------------------------------
        sections.append(self._workflows_section(study_id, warnings,
                                                limitations))

        # -- 9. user notes (labelled, never upgraded to fact) ----------------
        sections.append(self._notes_section(detail))

        # -- 10. limitations (only what the study actually recorded) --------
        sections.append(ReportSection(
            section_id="limitations", title="Limitations", kind="limitations",
            lines=list(dict.fromkeys(limitations)) or ["（未记录限制）"]))

        # -- 11. open questions (from blocked/failed tasks and warnings) ----
        sections.append(self._open_questions_section(warnings))

        # -- 12. references (deduped) ---------------------------------------
        sections.append(ReportSection(
            section_id="references", title="References", kind="references",
            lines=[f"{r.ref_id} {r.title or r.doi or '(untitled)'}"
                   for r in references] or ["（无引用文献）"]))

        ordered = sorted(sections, key=lambda s: _SECTION_ORDER.index(s.section_id))
        warnings.append(ReportWarning(
            code="NOT_AUDITED",
            message="本报告仅组织已有研究结果，未经最终科学审计（7.4）；"
                    "不构成最终科学结论。"))
        return StudyReport(
            report_id="report-" + content_hash(
                f"{study_id}|{len(detail.artifacts)}|{len(detail.references)}", 16),
            report_version=REPORT_VERSION, study_id=study_id,
            title=study.title, question=study.question, status="generated",
            generated_at=utc_now_iso(),
            scope={"reference_kinds": by_kind, "artifacts": len(detail.artifacts)},
            sections=ordered, references=references, warnings=warnings,
            limitations=list(dict.fromkeys(limitations)))

    # -- sections -----------------------------------------------------------

    def _molecules_section(self, detail, warnings) -> ReportSection:
        refs = [r for r in detail.references if r.kind is ReferenceKind.MOLECULE]
        lines: list[str] = []
        source_refs: list[ReportSourceRef] = []
        for ref in refs:
            from crea.repositories.errors import NotFound

            try:
                entity = self._kb.get_entity(ref.object_id)
            except NotFound:
                warnings.append(ReportWarning(
                    code="MOLECULE_NOT_FOUND",
                    message=f"session 引用的 molecule {ref.object_id!r} 不在知识库",
                    ref=ref.object_id))
                continue
            name = entity.names[0] if entity.names else "unavailable"
            lines.append(
                f"{name} · SMILES {entity.canonical_smiles or '—'} · "
                f"InChIKey {entity.inchikey or '—'} · "
                f"formula {entity.molecular_formula or '—'} · "
                f"MW {entity.molecular_weight if entity.molecular_weight else '—'}")
            source_refs.append(ReportSourceRef(
                type=SourceRefType.MOLECULE, id=entity.entity_id, label=name))
        return ReportSection(
            section_id="molecules", title="Molecules", kind="molecules",
            lines=lines or ["（未引用 molecule）"], source_refs=source_refs)

    def _reactions_section(self, detail, warnings) -> ReportSection:
        refs = [r for r in detail.references if r.kind is ReferenceKind.REACTION]
        lines: list[str] = []
        source_refs: list[ReportSourceRef] = []
        for ref in refs:
            from crea.services.reaction_library import get_template

            template = get_template(ref.object_id)
            if template is None:
                warnings.append(ReportWarning(
                    code="REACTION_NOT_FOUND",
                    message=f"session 引用的 reaction {ref.object_id!r} 不在反应库",
                    ref=ref.object_id))
                continue
            lines.append(
                f"{template.name}（{template.class_family.value}）· "
                f"{template.transformation.note} · "
                f"reagents {'; '.join(r.name for r in template.reagents) or '—'}")
            source_refs.append(ReportSourceRef(
                type=SourceRefType.REACTION, id=template.template_id,
                label=template.name))
        return ReportSection(
            section_id="reactions", title="Reactions", kind="reactions",
            lines=lines or ["（未引用 reaction）"], source_refs=source_refs)

    def _evidence_section(self, detail, ref_index, warnings):
        refs = [r for r in detail.references if r.kind is ReferenceKind.EVIDENCE]
        lines: list[str] = []
        source_refs: list[ReportSourceRef] = []
        for ref in refs:
            record = self._kb.get_reaction_evidence(ref.object_id)
            if record is None:
                warnings.append(ReportWarning(
                    code="EVIDENCE_NOT_FOUND",
                    message=f"session 引用的 evidence {ref.object_id!r} 不在知识库",
                    ref=ref.object_id))
                continue
            literature = ""
            if record.source.source_type.value == "literature" and record.source.source_id:
                paper = self._kb.store.get_paper(record.source.source_id)
                if paper is not None:
                    literature = ref_index.add(paper)
                else:
                    warnings.append(ReportWarning(
                        code="LITERATURE_NOT_FOUND",
                        message=f"evidence {record.evidence_id!r} 引用的文献不在本地",
                        ref=record.source.source_id))
            locator = record.source.locator
            loc = "" if locator is None else " ".join(
                f"{k}={v}" for k, v in locator.model_dump(mode="json").items() if v)
            lines.append(
                f"{record.evidence_id} · {record.evidence_type.value} · "
                f"{record.status.value} · {record.support_scope or '—'} · "
                f"{literature or 'CURATED'} · {loc or 'no locator'}")
            source_refs.append(ReportSourceRef(
                type=SourceRefType.EVIDENCE, id=record.evidence_id,
                label=record.evidence_type.value))
        return ReportSection(
            section_id="evidence", title="Evidence & Literature",
            kind="evidence", lines=lines or ["（未引用 evidence）"],
            source_refs=source_refs), refs

    def _routes_section(self, detail, limitations) -> ReportSection:
        routes = [a for a in detail.artifacts
                  if a.artifact_type is ArtifactType.ROUTE_EXPLORATION]
        lines: list[str] = []
        source_refs: list[ReportSourceRef] = []
        for artifact in routes:
            content = artifact.content or {}
            complete = content.get("complete") or []
            partial = content.get("partial") or []
            blocked = content.get("blocked") or []
            lines.append(
                f"complete {len(complete)} · partial {len(partial)} · "
                f"blocked {len(blocked)}")
            for path in partial:
                lines.append(
                    f"  PARTIAL route {path.get('route_id', '?')}: "
                    f"{path.get('blocked_reason', '')}")
            for path in blocked:
                lines.append(
                    f"  BLOCKED path {path.get('route_id', '?')}: "
                    f"{path.get('blocked_reason', '')}")
            if not complete:
                limitations.append(
                    "No complete deterministic route was found in the current "
                    "CREA reaction knowledge.（这不同于'不存在合成方法'。）")
            source_refs.append(ReportSourceRef(
                type=SourceRefType.ARTIFACT, id=artifact.artifact_id,
                label="route exploration"))
        return ReportSection(
            section_id="routes", title="Route Exploration", kind="routes",
            lines=lines or ["（未进行路线探索）"], source_refs=source_refs)

    def _analyses_section(self, detail, limitations) -> ReportSection:
        lines: list[str] = []
        source_refs: list[ReportSourceRef] = []
        for artifact in detail.artifacts:
            title = _ANALYSIS_ARTIFACTS.get(artifact.artifact_type)
            if title is None:
                continue
            content = artifact.content or {}
            summary = (content.get("answer") or {}).get("summary") if isinstance(
                content.get("answer"), dict) else ""
            summary = summary or content.get("summary") or ""
            lines.append(f"{title}（{artifact.artifact_id}）：{summary or '（见 artifact）'}")
            for lim in (content.get("limitations") or [])[:3]:
                limitations.append(str(lim))
            source_refs.append(ReportSourceRef(
                type=SourceRefType.ARTIFACT, id=artifact.artifact_id, label=title))
        return ReportSection(
            section_id="analyses", title="AI-assisted Analyses", kind="analyses",
            summary="AI 解释性内容，非化学事实来源（§四十六）。",
            lines=lines or ["（无已保存的 AI 分析）"], source_refs=source_refs)

    def _workflows_section(self, study_id, warnings, limitations) -> ReportSection:
        workflows = self._kb.list_workflows(study_id)
        lines: list[str] = []
        for workflow in workflows:
            tasks = self._kb.list_tasks(workflow.workflow_id)
            counts: dict[str, int] = {}
            for task in tasks:
                counts[task.status.value] = counts.get(task.status.value, 0) + 1
            lines.append(
                f"{workflow.title}（{workflow.status.value}）: "
                + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
            for task in tasks:
                if task.status.value in ("blocked", "failed"):
                    warnings.append(ReportWarning(
                        code="TASK_NOT_COMPLETED",
                        message=f"task {task.task_type.value} {task.status.value}: "
                                f"{task.error}",
                        ref=task.task_id))
        return ReportSection(
            section_id="workflows", title="Workflows", kind="workflows",
            lines=lines or ["（无 workflow）"])

    def _notes_section(self, detail) -> ReportSection:
        notes = [e for e in detail.events
                 if e.event_type is StudyEventType.NOTE_ADDED]
        return ReportSection(
            section_id="notes", title="Research Notes", kind="notes",
            summary="USER_NOTE：用户笔记，不是科学事实（§五十九/§六十）。",
            lines=[f"User note: {n.note or n.summary}" for n in notes]
                  or ["（无用户笔记）"])

    def _open_questions_section(self, warnings) -> ReportSection:
        lines = [f"{w.code}: {w.message}" for w in warnings
                 if w.code in ("TASK_NOT_COMPLETED", "MOLECULE_NOT_FOUND",
                               "REACTION_NOT_FOUND", "EVIDENCE_NOT_FOUND",
                               "LITERATURE_NOT_FOUND")]
        return ReportSection(
            section_id="open_questions", title="Open Questions", kind="questions",
            lines=lines or ["（未记录未解决问题）"])


class _RefIndex:
    """Collects deduped references, assigning stable `[P1]` ids (§三十五)."""

    def __init__(self, references: list[ReportReference]) -> None:
        self._references = references
        self._by_key: dict[str, str] = {}
        self._next = 1

    def add(self, paper) -> str:
        from crea.core.ids import normalize_doi

        key = paper.paper_id or normalize_doi(paper.doi) or ""
        if key in self._by_key:
            return self._by_key[key]
        ref_id = f"[P{self._next}]"
        self._next += 1
        self._by_key[key] = ref_id
        self._references.append(ReportReference(
            ref_id=ref_id, paper_id=paper.paper_id, doi=paper.doi or "",
            title=paper.title or "", journal=paper.journal or "",
            year=paper.year,
            authors=[a.name or a.family for a in paper.authors][:20]))
        return ref_id


__all__ = ["StudyReportBuilder"]

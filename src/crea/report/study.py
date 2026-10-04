"""Render a study report as Markdown (CREA Stage 7.3).

Presentation only: it reads a `StudyReport` and writes Markdown. It does not
touch a database, a model, or the study. Every string it prints is escaped for
the Markdown it emits, so a note or a title cannot inject structure.
"""

from __future__ import annotations

from crea.core.models.study_report import StudyReport


def _safe(text: str) -> str:
    """Escape the characters that would change Markdown structure."""
    return (str(text).replace("\\", "\\\\").replace("|", "\\|")
            .replace("\n", " ").replace("\r", " "))


def render_study_markdown(report: StudyReport) -> str:
    """The report as a readable Markdown document."""
    lines: list[str] = []
    lines.append(f"# {_safe(report.title or 'Research Report')}")
    lines.append("")
    lines.append(f"- Report: {report.report_id} (v: {report.report_version})")
    lines.append(f"- Study: {report.study_id}")
    lines.append(f"- Status: {report.status} (not audited)")
    lines.append(f"- Generated: {report.generated_at}")
    lines.append("")

    for section in report.sections:
        lines.append(f"## {_safe(section.title)}")
        if section.summary:
            lines.append("")
            lines.append(_safe(section.summary))
        lines.append("")
        for line in section.lines:
            lines.append(f"- {_safe(line)}")
        if section.warnings:
            for warning in section.warnings:
                lines.append(f"- ⚠ {_safe(warning)}")
        lines.append("")

    if report.warnings:
        lines.append("## Warnings")
        lines.append("")
        for warning in report.warnings:
            lines.append(f"- `{_safe(warning.code)}`: {_safe(warning.message)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


__all__ = ["render_study_markdown"]

"""CLI smoke acceptance: the research workflow through the real entry point.

Phase 3.7 §7E. Every case runs `crea research` the way a user does -- through
`crea.cli.main`, the entry point pyproject's console script points at -- and
one case runs it as a separate process via `python -m crea.cli`. Nothing calls
`ResearchWorkflow.run` or `ResearchAgent.run` directly: the point is the whole
path, argument parsing and workflow dispatch included.

Fixture honesty, recorded here so nobody "fixes" a test by weakening it later:
the offline stack has no extraction model, so a user PDF yields Evidence but
no ReactionRecord -- a run with `--pdf` therefore ends PARTIAL and says so.
Without `--pdf` the identical chain ends COMPLETED. Neither outcome is
adjusted; both are read from the report's existing status field.

No network, no API key, no model download. ENVIRONMENT note: the console
script itself is not installed in this development venv; the module entry
(`python -m crea.cli`) runs the same `crea.cli:main`.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from crea.cli import main

QUESTION_DIYNE = "Analyze 1,4-diphenyl-1,3-diyne"
QUESTION_PHENYL = "Analyze phenylacetylene"
DIYNE = "c1ccccc1C#CC#Cc2ccccc2"


@pytest.fixture(autouse=True)
def _isolated_default_db(tmp_path, monkeypatch):
    """Run every case from `tmp_path`: the workflow form's knowledge base
    defaults to `data/crea.db` under the working directory (Phase 3.11
    §37), and a test that ran from the repository would write into the
    real one. The default stays under test; only its location moves."""
    monkeypatch.chdir(tmp_path)

SECTION_NAMES = (
    "Research Task",
    "Chemical Identity",
    "Molecular Analysis",
    "Literature",
    "Experimental Evidence",
    "Retrosynthesis",
    "Evidence and Provenance",
    "Limitations",
)


def _run(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _demo_pdf(tmp_path):
    from tests.fakes import pdfs

    path = tmp_path / "procedure.pdf"
    path.write_bytes(pdfs.make_pdf(pdfs.EXPERIMENTAL))
    return path


def _assert_core_sections(text: str) -> None:
    for name in SECTION_NAMES:
        assert name in text, f"missing report section: {name}"


# ---------------------------------------------------------------------------
# Case 1 -- complete offline research
# ---------------------------------------------------------------------------


def test_case_1_full_offline_research_chain_with_user_pdf(tmp_path, capsys):
    """CLI → workflow → explicit SMILES → identity → analysis → literature →
    user PDF evidence → retrosynthesis → route validation → route evidence →
    report. The target is the one the offline fixtures actually hold routes
    for, so every link of the chain is exercised; the PDF is the synthetic
    procedure fixture.

    The terminal status is read from the report's existing field and pinned:
    PARTIAL, because the offline stack has no extraction model and the PDF
    yields Evidence but no ReactionRecord. That gap is a fixture limit, not a
    reason to call the run complete.
    """
    pdf = _demo_pdf(tmp_path)
    save = tmp_path / "report.json"
    code, out, _err = _run(
        ["research", "--question", QUESTION_DIYNE, "--smiles", DIYNE,
         "--pdf", str(pdf), "--save", str(save)], capsys)

    assert code == 0
    assert out.startswith("# Research Report")
    _assert_core_sections(out)

    # chain: identity from the explicit SMILES, never guessed
    assert "input: smiles" in out
    assert "resolution status: resolved" in out
    # chain: molecular analysis is computed and labelled so
    assert "[Computed]" in out
    # chain: literature is discovery, not evidence
    assert "[Discovery]" in out
    # chain: user PDF evidence exists and is not published literature
    data = json.loads(save.read_text(encoding="utf-8"))
    assert data["evidence"], "the user PDF must yield evidence"
    assert all(e["source"] == "user_provided" for e in data["evidence"])
    assert all(e["strength"] == "user_provided_evidence" for e in data["evidence"])
    # chain: retrosynthesis produced candidates, validated and chained
    assert "### Route 1" in out
    assert "Candidate only" in out
    assert data["routes"], "retrosynthesis must run"
    assert data["route_evidence"], "route evidence must be generated"
    trace = data["trace_summary"]
    assert "validate_route_step" in trace
    assert "route_evidence_report" in trace

    # terminal status from the report's own field: PARTIAL, and why
    assert "Status: PARTIAL" in out
    assert data["status"] == "partial"
    assert "(no experimental evidence)" in out  # no record was fabricated


def test_case_1_reaches_completed_when_no_gap_remains(tmp_path, capsys):
    """The same chain without the unextractable PDF: the report's existing
    status field then says COMPLETED. Confirms the completion semantics are
    reachable through the CLI and are not withheld or invented."""
    code, out, _err = _run(
        ["research", "--question", QUESTION_DIYNE, "--smiles", DIYNE,
         "--json"], capsys)

    assert code == 0
    data = json.loads(out)
    assert data["status"] == "completed"
    assert data["routes"] and data["route_evidence"]
    assert any(f["kind"] == "computed" for f in data["findings"])


# ---------------------------------------------------------------------------
# Case 2 -- a question with no structure
# ---------------------------------------------------------------------------


def test_case_2_no_structure_is_never_guessed(capsys):
    """`crea research --question "Analyze phenylacetylene"` and nothing else.

    exit 0 (PARTIAL is a research result, not a CLI error), identity
    unresolved, and not one structure anywhere: no resolved SMILES, no
    computed molecular finding, no retrosynthesis route, no selected
    structure. The question text is never fed to a structure parser.
    """
    code, out, _err = _run(["research", "--question", QUESTION_PHENYL], capsys)

    assert code == 0
    assert "# Research Report" in out
    assert "Identity unresolved" in out
    assert "Status: PARTIAL" in out

    identity_section = out.split("## 2. Chemical Identity")[1].split("## 3.")[0]
    assert "SMILES:" not in identity_section
    assert "InChI" not in identity_section
    assert "InChIKey:" not in identity_section
    assert "resolution status: resolved" not in out
    assert "[Computed]" not in out          # no analysis of a guessed structure
    assert "### Route" not in out           # no route for a guessed target
    assert "(retrosynthesis unavailable)" in out
    assert "selected" not in identity_section.lower()


# ---------------------------------------------------------------------------
# Case 3 -- the output file
# ---------------------------------------------------------------------------


def test_case_3_output_file_receives_the_report_not_stdout(tmp_path, capsys):
    target = tmp_path / "report.md"
    code, out, _err = _run(
        ["research", "--question", QUESTION_DIYNE, "--smiles", DIYNE,
         "--output", str(target)], capsys)

    assert code == 0
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert text.strip(), "the report file must not be empty"
    assert text.startswith("# Research Report")
    _assert_core_sections(text)
    # existing semantics: with --output the report goes to the file, and
    # stdout is not used to repeat it
    assert "# Research Report" not in out


# ---------------------------------------------------------------------------
# Case 4 -- illegal arguments are CLI errors, not research results
# ---------------------------------------------------------------------------


def test_case_4a_missing_question_is_a_cli_error(capsys):
    code, _out, err = _run(["research"], capsys)

    assert code == 2
    assert "--question" in err


def test_case_4b_missing_pdf_is_a_cli_error_not_partial(tmp_path, capsys):
    missing = tmp_path / "nope.pdf"
    code, out, err = _run(
        ["research", "--question", QUESTION_DIYNE, "--smiles", DIYNE,
         "--pdf", str(missing)], capsys)

    assert code == 2
    assert "PDF 不存在" in err
    assert "Status:" not in out, "a CLI input error must not render a research report"


# ---------------------------------------------------------------------------
# Case 5 -- the legacy positional form keeps its own dispatch
# ---------------------------------------------------------------------------


def test_case_5_legacy_form_still_dispatches_to_the_agent_path(capsys):
    """`crea research "..."` alone stays on the old agent path (Chinese v1
    report); the new flags take the workflow form (Research Report v2).
    Dispatch is per-invocation, not per-command."""
    code, out, _err = _run(
        ["research", "分析并研究 1,4-diphenyl-1,3-diyne"], capsys)

    assert code == 0
    assert "研究报告" in out            # the legacy renderer
    assert "# Research Report" not in out  # not the workflow form

    # and the flag form does enter the workflow
    code, out, _err = _run(
        ["research", "--question", QUESTION_PHENYL], capsys)
    assert code == 0
    assert "# Research Report" in out


# ---------------------------------------------------------------------------
# the entry itself, as a process
# ---------------------------------------------------------------------------


def test_the_module_entry_runs_as_a_real_process(tmp_path):
    """`python -m crea.cli research ...` in a subprocess: the same
    `crea.cli:main` a console script would call, exercised at process level.
    (The console script is not installed in this venv -- ENVIRONMENT.)"""
    src = pathlib.Path(__file__).resolve().parents[2] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src)
    proc = subprocess.run(
        [sys.executable, "-m", "crea.cli", "research",
         "--question", QUESTION_PHENYL],
        capture_output=True, text=True, encoding="utf-8",
        env=env, cwd=str(tmp_path), timeout=120)

    assert proc.returncode == 0, proc.stderr
    assert "# Research Report" in proc.stdout
    assert "Identity unresolved" in proc.stdout
    assert "Traceback" not in proc.stderr

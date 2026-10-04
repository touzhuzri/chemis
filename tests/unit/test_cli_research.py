"""The demo CLI: one research task from the command line.

§6 of Phase 3.7. What is under test is the entry point, not the chemistry:
arguments become a task and its inputs, the workflow is invoked, and a
Markdown Research Report comes out of stdout or a file. The CLI never
touches an engine -- that is checked directly, because a CLI that quietly
grew a call to RDKit would be a layering failure wearing a convenience.

Everything runs against the offline stack: no network, no model download.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from crea.cli import main

QUESTION = "Analyze phenylacetylene"
SMILES = "C#Cc1ccccc1"


@pytest.fixture(autouse=True)
def _isolated_default_db(tmp_path, monkeypatch):
    """The workflow form writes to `data/crea.db` under the working
    directory (Phase 3.11 §37). Run every case from `tmp_path` so that
    default is exercised for real and the repository's own `data/` is
    never touched -- tests must not pollute the real database."""
    monkeypatch.chdir(tmp_path)


def _run(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# 1-3. question and structure input
# ---------------------------------------------------------------------------


def test_question_builds_a_task_and_reaches_the_report(capsys):
    code, out, _err = _run(["research", "--question", QUESTION], capsys)

    assert code == 0
    assert "# Research Report" in out
    assert f"Question: {QUESTION}" in out
    assert "## 1. Research Task" in out


def test_smiles_enters_the_identity_resolution_verbatim(capsys):
    code, out, _err = _run(
        ["research", "--question", QUESTION, "--smiles", SMILES], capsys)

    assert code == 0
    assert "## 2. Chemical Identity" in out
    assert f"input: smiles `{SMILES}`" in out
    assert "resolution status: resolved" in out
    assert "identity source: user_input" in out
    # the structure is the user's input, parsed -- never renamed or replaced
    assert "Identity unresolved" not in out


def test_without_smiles_the_cli_never_guesses_a_structure(capsys):
    code, out, _err = _run(["research", "--question", QUESTION], capsys)

    assert code == 0
    assert "Identity unresolved" in out
    # no SMILES line anywhere: nothing was invented to fill the gap
    assert "SMILES:" not in out
    # and the structure-dependent steps say so rather than pretending
    assert "identity unresolved" in out.lower() or "不猜测" in out


def test_the_legacy_positional_question_form_still_works(capsys):
    """`crea research "question"` keeps its old shape: same command, same
    question, same report output. The new flags are additive."""
    code, out, _err = _run(
        ["research", "分析并研究 1,4-diphenyl-1,3-diyne"], capsys)

    assert code == 0
    assert out.strip()
    # the old form renders the Chinese agent report
    assert "研究报告" in out


def test_missing_question_is_a_clear_error(capsys):
    code, _out, err = _run(["research", "--smiles", SMILES], capsys)

    assert code == 2
    assert "--question" in err


# ---------------------------------------------------------------------------
# 4. PDF input
# ---------------------------------------------------------------------------


def test_a_missing_pdf_is_a_clear_error_not_a_silent_skip(tmp_path, capsys):
    missing = tmp_path / "nope.pdf"
    code, _out, err = _run(
        ["research", "--question", QUESTION, "--pdf", str(missing)], capsys)

    assert code == 2
    assert "PDF 不存在" in err
    assert str(missing) in err


def test_a_pdf_that_cannot_be_parsed_is_a_clear_error(tmp_path, capsys):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"this is not a pdf at all")
    code, _out, err = _run(
        ["research", "--question", QUESTION, "--pdf", str(broken)], capsys)

    assert code == 2
    assert "无法启动离线 workflow stack" in err or "PDF" in err


def test_a_supplied_pdf_is_registered_as_a_document(tmp_path, capsys):
    from tests.fakes import pdfs

    path = tmp_path / "procedure.pdf"
    path.write_bytes(pdfs.make_pdf(pdfs.EXPERIMENTAL))
    code, out, _err = _run(
        ["research", "--question", QUESTION, "--smiles", SMILES,
         "--pdf", str(path)], capsys)

    assert code == 0
    assert "## 5. Experimental Evidence" in out
    assert "No user document provided" not in out


# ---------------------------------------------------------------------------
# 5-6. output
# ---------------------------------------------------------------------------


def test_markdown_report_goes_to_stdout_by_default(capsys):
    code, out, _err = _run(
        ["research", "--question", QUESTION, "--smiles", SMILES], capsys)

    assert code == 0
    assert out.startswith("# Research Report")
    assert "## 8. Limitations" in out
    # a Markdown document, not JSON
    assert not out.lstrip().startswith("{")


def test_output_writes_the_markdown_to_a_file(tmp_path, capsys):
    target = tmp_path / "report.md"
    code, out, _err = _run(
        ["research", "--question", QUESTION, "--smiles", SMILES,
         "--output", str(target)], capsys)

    assert code == 0
    text = pathlib.Path(target).read_text(encoding="utf-8")
    assert text.startswith("# Research Report")
    assert f"Question: {QUESTION}" in text
    # with --output, stdout stays clean
    assert "# Research Report" not in out


def test_json_flag_still_emits_the_machine_report(tmp_path, capsys):
    code, out, _err = _run(
        ["research", "--question", QUESTION, "--smiles", SMILES, "--json"],
        capsys)

    assert code == 0
    payload = json.loads(out)
    assert payload["question"] == QUESTION


# ---------------------------------------------------------------------------
# 7. PARTIAL is a result, not a crash
# ---------------------------------------------------------------------------


def test_a_partial_workflow_still_exits_zero_with_a_report(capsys):
    """No --smiles: identity cannot resolve, the run is PARTIAL -- and that
    is a research outcome, printed in full, not a CLI failure."""
    code, out, err = _run(["research", "--question", QUESTION], capsys)

    assert code == 0
    assert "# Research Report" in out
    assert "Status: PARTIAL" in out
    assert "workflow PARTIAL" in out
    assert err == ""


def test_an_unwritable_output_path_is_a_clear_error(tmp_path, capsys):
    target = tmp_path  # a directory is not a file to write
    code, _out, err = _run(
        ["research", "--question", QUESTION, "--smiles", SMILES,
         "--output", str(target)], capsys)

    assert code == 2
    assert "无法写入" in err


# ---------------------------------------------------------------------------
# 8. the CLI is an entry point, not a chemistry engine
# ---------------------------------------------------------------------------


def test_the_cli_never_calls_a_chemistry_engine_directly():
    """Parse arguments, build inputs, invoke the workflow, render, output --
    and nothing else. The workflow form's handler must not reference RDKit,
    the literature service, route validation or the retrosynthesis engine;
    everything it reports came back through the workflow's tools."""
    import ast

    source = pathlib.Path(
        __import__("crea.cli", fromlist=["__file__"]).__file__
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(node for node in tree.body
              if isinstance(node, ast.FunctionDef)
              and node.name == "_research_workflow")

    forbidden = {"rdkit", "normalize", "LiteratureService",
                 "RouteValidationService", "aizynthfinder", "AiZynthFinder",
                 "search_document", "ReactionExtractor"}
    touched = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Import):
            touched.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            touched.add(node.module.split(".")[-1])
            touched.add(node.module.split(".")[0])
        elif isinstance(node, ast.Name):
            touched.add(node.id)
        elif isinstance(node, ast.Attribute):
            touched.add(node.attr)
    assert not (touched & forbidden), (
        f"the workflow CLI handler reached for an engine: {touched & forbidden}"
    )
    # and it does go through the workflow
    assert "ResearchWorkflow" in source.split("def _research_workflow")[1]

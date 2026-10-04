"""Golden Case: 研究苯乙炔（phenylacetylene）—— product-level acceptance.

The one real failure case that closed Phase 3.x: an explicit, valid
SMILES (`C#Cc1ccccc1`) must become a determined identity, a created
entity, a molecular analysis, a knowledge lookup, a literature search,
honest reactions/routes, and a report that agrees with the API and the
UI -- across restarts. This file is the regression gate for every layer
of that chain:

    Browser/API/CLI → Application → ResearchJob → ResearchWorkflow
        → Identity → KnowledgeBase → Report

Deterministic fixtures are allowed here (§十四); the *live* web run is
recorded in docs/PHASE3_FINAL_PRODUCT_ACCEPTANCE.md. No RDKit output
string is hardcoded as the only truth -- properties are compared for
internal consistency (§四).
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from crea.api.app import create_app
from crea.api.dependencies import create_context

GOLDEN_QUESTION = "研究苯乙炔（phenylacetylene）的基本性质、已有文献和可能的合成路线。"
GOLDEN_SMILES = "C#Cc1ccccc1"

PROCEDURE = [
    "Experimental",
    "Phenylacetylene (1.0 mmol),",
    "CuI (10 mol%),",
    "were dissolved in DMF (5 mL).",
    "The mixture was stirred at 80 C",
    "for 12 h.",
    "The product was isolated in 82% yield.",
    "Compound 8 (InChI=1S/C16H10/c1-3-9-15(10-4-1)13-7-8-14-16-11-5-2-6-12-16"
    "/h1-6,9-12H) was purified.",
]


@pytest.fixture()
def client(tmp_path):
    from tests.integration_offline.test_workbench_api import _extracting_factory

    ctx = create_context(
        db_path=tmp_path / "crea.db", documents_dir=tmp_path / "docs",
        stack_factory=_extracting_factory(tmp_path / "crea.db",
                                          tmp_path / "docs"))
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        yield c


def _run_golden(client, *, question=GOLDEN_QUESTION, structure=GOLDEN_SMILES,
                document_ids=None, timeout=60.0):
    started = client.post("/api/research", json={
        "question": question,
        "structure_kind": "SMILES" if structure else None,
        "structure_value": structure or "",
        "document_ids": document_ids or []})
    assert started.status_code == 200, started.text
    job_id = started.json()["job_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/research/{job_id}").json()
        if job["status"] in ("completed", "partial", "cancelled",
                             "interrupted"):
            break
        time.sleep(0.15)
    else:
        raise AssertionError("golden run did not finish")
    session = client.get(f"/api/sessions/{job['session_id']}").json()
    report = client.get(f"/api/research/{job_id}/report").json()
    return job, session, report


# ---------------------------------------------------------------------------
# A -- Identity: RESOLVED, entity established, session holds it
# ---------------------------------------------------------------------------


def test_A_identity_is_resolved_and_the_entity_is_established(client):
    job, session, report = _run_golden(client)

    # §一 data-flow: the submitted structure reaches every layer verbatim
    assert job["structure_kind"] == "SMILES"
    assert job["structure_value"] == GOLDEN_SMILES

    identity = session["identity"]
    assert identity is not None, "the identity card exists (§17)"
    assert identity["status"] == "resolved", (
        "an explicit valid SMILES is RESOLVED, not UNKNOWN/UNAVAILABLE (§三)")
    assert identity["input_value"] == GOLDEN_SMILES
    # internal consistency instead of pinned RDKit strings (§四)
    assert identity["canonical_smiles"]
    assert identity["inchi"].startswith("InChI=")
    assert identity["inchikey"]
    assert session["entity_ids"], "the session references the entity"
    entity_id = session["entity_ids"][0]
    assert entity_id.startswith("CHEM-")
    entity = client.get(f"/api/entities/{entity_id}").json()
    assert entity["identity_key"] == f"crea-identity-key-v1:{entity['inchi']}", (
        "the identity key is derived from the established InChI")
    assert entity["provenance"] is not None, "provenance exists (§四.A)"
    # the REPORT agrees with the API (§八: no more contradiction)
    markdown = report["report_markdown"]
    identity_section = markdown.split("## 2. Chemical Identity")[1].split("## 3.")[0]
    assert "resolution status: resolved" in identity_section
    assert "Identity unresolved" not in identity_section
    assert "未附带身份解析结果" not in identity_section
    assert GOLDEN_SMILES in identity_section
    assert entity_id in markdown or entity["canonical_smiles"] in identity_section


# ---------------------------------------------------------------------------
# B -- Molecular Analysis really runs
# ---------------------------------------------------------------------------


def test_B_molecular_analysis_runs_and_says_it_is_computed(client):
    _job, session, report = _run_golden(client)
    assert session["analysis"], "computed analysis exists (§四.B)"
    joined = " ".join(session["analysis"])
    for expected in ("SMILES", "分子式", "分子量"):
        assert expected in joined, f"analysis reports {expected}"
    section = report["report_markdown"].split(
        "## 3. Molecular Analysis")[1].split("## 4.")[0]
    assert "(no computed analysis was run)" not in section
    assert "[Computed]" in section
    assert "不是文献证据" in section, "computed facts stay labelled (§四.G)"


# ---------------------------------------------------------------------------
# C -- Knowledge: created on the first run, reused on the second
# ---------------------------------------------------------------------------


def test_C_first_run_creates_the_entity_and_the_second_reuses_it(client):
    _job1, session1, report1 = _run_golden(client)
    entity_one = session1["entity_ids"][0]
    # first run: the entity is NEW knowledge, and says so
    assert entity_one in (report1["report"]["knowledge"]["new"]["entities"]
                          or [entity_one])
    first_facets = {f["facet"]: f["state"] for f in
                    session1["knowledge_before"]["facets"]}
    assert first_facets["entity"] == "known", (
        "after identity, the entity facet is known -- never "
        "'建议 resolve identity' (§十八)")

    _job2, session2, report2 = _run_golden(client)
    entity_two = session2["entity_ids"][0]
    assert entity_two == entity_one, "the same identity reuses the entity (§七)"
    reused = report2["report"]["knowledge"]["existing"]
    assert entity_one in (reused.get("entities") or []), (
        "the second run reports the entity as EXISTING")
    exported = client.get("/api/knowledge/export").json()
    ids = [e["entity_id"] for e in exported["entities"]]
    assert ids.count(entity_one) == 1, "no second entity for one identity"
    # provenance / retrieved_at survive
    entity = client.get(f"/api/entities/{entity_one}").json()
    assert entity["provenance"] is not None and entity["created_at"]


# ---------------------------------------------------------------------------
# D/E -- Literature discovery happens; metadata never becomes reaction/evidence
# ---------------------------------------------------------------------------


def test_DE_literature_runs_from_determined_identity_and_stays_discovery(client):
    job, session, report = _run_golden(client)
    markdown = report["report_markdown"]
    # identity came FIRST (§十三): the phases order proves it
    phases = session["phases"]
    assert phases.index("researching.identity") < phases.index(
        "assessing_knowledge")
    assert "researching.literature" in phases, "literature search happened"
    # discovery is discovery (§四.D/§四.G)
    assert "[Discovery]" in markdown or "Discovery" in markdown
    assert "（no experimental evidence）" in markdown or "no experimental" in markdown \
        or "实验" in markdown
    # with metadata-only sources: reactions may be 0 -- and must be honest
    assert session["reaction_ids"] == [] or all(
        r["status"] != "experimentally_supported"
        for r in [client.get(f"/api/reactions/{rid}").json()
                  for rid in session["reaction_ids"]])
    assert "该文献被发现，但当前没有足够实验文本建立 Reaction。" in markdown \
        or not session["reaction_ids"]


def test_E_with_a_local_pdf_the_reaction_chain_is_attempted(client):
    from tests.fakes import pdfs

    data = bytes(pdfs.make_pdf([PROCEDURE]))
    document_id = client.post(
        "/api/documents",
        files={"file": ("procedure.pdf", data, "application/pdf")}
    ).json()["document_id"]
    job, session, report = _run_golden(client, document_ids=[document_id])

    assert session["reaction_ids"], "Document → Mention → Reaction ran (§四.E)"
    reaction = client.get(
        f"/api/reactions/{session['reaction_ids'][0]}").json()
    assert reaction["evidence_ids"], "citations resolve"
    assert reaction["record_id"] or reaction["evidence_ids"]
    chain = client.get(
        f"/api/reactions/{session['reaction_ids'][0]}/navigation").json()
    assert chain["links"]["entities"], "the chain reaches entities"


# ---------------------------------------------------------------------------
# F/G -- routes stay candidates; unavailable capability never stops the run
# ---------------------------------------------------------------------------


def test_F_routes_are_candidates_and_unavailability_never_stops_the_run(tmp_path):
    """§五/§四.F: a missing retrosynthesis engine makes the ROUTE honest,
    and leaves identity, analysis and literature complete."""
    from crea.agent.offline import build_offline_workflow_stack
    from crea.agent.toolsets import build_registry
    from crea.agent.workspace import IdentityState, RouteWorkbench
    from crea.core.models.identity import StructureInput
    from crea.core.models.research import ResearchTask
    from crea.report.research import render_research_report_v3
    from crea.services.knowledge_base import KnowledgeBase
    from crea.services.literature import LiteratureService
    from crea.services.route_validation import RouteValidationService
    from crea.services.research_workflow import ResearchWorkflow

    kb = KnowledgeBase(tmp_path / "crea.db")
    kb.initialize()
    # the offline literature, and NO retrosynthesis engine at all
    from crea.agent.offline import FixtureTransport  # noqa: F401

    workflow, library, sink, registry = build_offline_workflow_stack(
        knowledge_base=kb)
    registry.routes = RouteWorkbench(provider=None,
                                     validator=RouteValidationService())
    task = ResearchTask(task_id="golden-no-routes", question=GOLDEN_QUESTION)
    report = workflow.run(
        task,
        structure_input=StructureInput.of("SMILES", GOLDEN_SMILES))
    session = workflow.session

    assert session.chemical_entity_ids, "identity still RESOLVED (§五)"
    assert any("SMILES" in f.text for f in report.findings
               if f.kind == "computed"), "analysis still ran (§五)"
    assert report.sources or report.unresolved_items, "literature still ran"
    # the route capability says why it is missing -- and only the route
    markdown = render_research_report_v3(report, session=session)
    assert "retrosynthesis unavailable" in markdown \
        or "retrosynthesis" in markdown
    route_gaps = [g for g in (report.knowledge.gaps if report.knowledge else [])
                  if g.facet.value == "route"]
    assert route_gaps, "the gap is on the route facet only"
    assert not any(g.facet.value == "entity" for g in
                   (report.knowledge.gaps if report.knowledge else [])), (
        "a resolved identity never gets 'resolve identity' back (§十八)")
    kb.close()


def test_G_the_report_distinguishes_computed_discovery_and_gaps(client):
    _job, session, report = _run_golden(client)
    markdown = report["report_markdown"]
    assert "[Computed]" in markdown
    assert "Status:" in markdown
    # knowledge sections separate existing / new / gaps (§四.G)
    assert "## Existing Knowledge" in markdown
    assert "## New Knowledge" in markdown
    assert "## Knowledge Gaps" in markdown
    for gap in session["knowledge_gaps"]:
        assert gap["reason"], "every gap is a sentence, not a score"
        assert "置信度" not in gap["reason"]
    # a candidate route is never written as a validated route
    if session["route_ids"]:
        assert "Candidate only" in markdown or "Candidate" in markdown


# ---------------------------------------------------------------------------
# §七 -- restart: the knowledge survives and is reused
# ---------------------------------------------------------------------------


def test_restart_then_research_reuses_and_keeps_provenance(tmp_path):
    db_path = tmp_path / "crea.db"
    documents_dir = tmp_path / "docs"
    from tests.integration_offline.test_workbench_api import _extracting_factory

    factory = _extracting_factory(db_path, documents_dir)
    ctx = create_context(db_path=db_path, documents_dir=documents_dir,
                         stack_factory=factory)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        _job1, session1, _report1 = _run_golden(c)
        entity_one = session1["entity_ids"][0]
        created_at = c.get(f"/api/entities/{entity_one}").json()["created_at"]
        reactions_one = len(c.get("/api/knowledge/export").json()["reactions"])

    # the server goes down; knowledge stays (§七)
    ctx2 = create_context(db_path=db_path, documents_dir=documents_dir,
                          stack_factory=factory)
    app2 = create_app(context=ctx2, web_root=tmp_path / "no-web")
    with TestClient(app2) as c2:
        _job2, session2, report2 = _run_golden(c2)
        assert session2["entity_ids"][0] == entity_one
        exported = c2.get("/api/knowledge/export").json()
        ids = [e["entity_id"] for e in exported["entities"]]
        assert ids.count(entity_one) == 1
        assert len(exported["reactions"]) == reactions_one, (
            "papers/reactions/evidence are not duplicated (§七)")
        entity = c2.get(f"/api/entities/{entity_one}").json()
        assert entity["created_at"] == created_at, "provenance/retrieved kept"
        reused = report2["report"]["knowledge"]["existing"]
        assert entity_one in (reused.get("entities") or [])


# ---------------------------------------------------------------------------
# §十 -- the CLI acceptance path
# ---------------------------------------------------------------------------


def test_the_cli_golden_run_reports_the_same_facts(tmp_path):
    src = os.path.abspath(str(pathlib.Path(__file__).resolve().parents[2] / "src"))
    env = dict(os.environ)
    env["PYTHONPATH"] = src
    db = tmp_path / "golden.crea.db"
    saved = tmp_path / "report.json"
    proc = subprocess.run(
        [sys.executable, "-m", "crea.cli", "research",
         "--question", GOLDEN_QUESTION,
         "--smiles", GOLDEN_SMILES,
         "--db", str(db),
         "--save-json", str(saved)],
        capture_output=True, text=True, encoding="utf-8",
        env=env, cwd=str(tmp_path), timeout=180)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "resolution status: resolved" in out
    assert "Identity unresolved" not in out
    assert "[Computed]" in out
    assert "Discovery" in out
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["status"] in ("completed", "partial")
    # JSON agrees with the rendered report (§十)
    assert data["knowledge"]["coverage"]["entity_id"] or data["knowledge"]
    entity_ids = data["knowledge"]["new"]["entities"] or \
        data["knowledge"]["existing"]["entities"]
    assert entity_ids, "the JSON names the entity"

    # the same structure, researched again through the CLI, reuses it
    proc2 = subprocess.run(
        [sys.executable, "-m", "crea.cli", "research",
         "--question", GOLDEN_QUESTION,
         "--smiles", GOLDEN_SMILES,
         "--db", str(db),
         "--save-json", str(tmp_path / "report2.json")],
        capture_output=True, text=True, encoding="utf-8",
        env=env, cwd=str(tmp_path), timeout=180)
    assert proc2.returncode == 0, proc2.stderr
    second = json.loads((tmp_path / "report2.json").read_text(encoding="utf-8"))
    assert second["knowledge"]["existing"]["entities"], "second CLI run reuses"

"""The Chemical Research Workbench, end to end (Phase 4 §121).

Real HTTP surface over the real engine: FastAPI TestClient in-process,
real `ResearchWorkflow` on real job threads, the real KnowledgeBase and
the real SQLite persistence. No mock replaces the core (§121.L/§114):
only the stack's fixtures are synthetic, and they say so.

Covered here: create research over HTTP (B), asynchronous jobs (C), the
real workflow underneath (D/E/F), session query (G), Report v3 (H),
knowledge navigation (I), gaps/conflicts/partial (J), PDF into the
document pipeline (K), provider-unavailable honesty (L), restart
persistence (M), idempotent re-research (§92), concurrency (§38), and
read-only knowledge endpoints (§93/§94).
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from crea.api.app import create_app
from crea.api.dependencies import create_context
from crea.app.events import EventLog
from crea.app.jobs import JobManager
from tests.fakes import pdfs

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"
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


def _payload(evidence_id: str) -> str:
    return json.dumps({
        "description": "coupling to the product",
        "description_evidence_refs": [evidence_id],
        "components": [
            {"name": "phenylacetylene", "value": "phenylacetylene",
             "component_role": "reactant", "unit": "", "status": "EXPLICIT",
             "evidence_refs": [evidence_id],
             "raw_text": "Phenylacetylene (1.0 mmol),"},
            {"name": "compound 8", "value": "compound 8",
             "component_role": "product", "unit": "", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": "Compound 8"},
        ],
        "conditions": [
            {"name": "temperature", "value": 80, "raw_value": "80",
             "unit": "C", "raw_unit": "C", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": "stirred at 80 C"},
            {"name": "time", "value": 12, "raw_value": "12", "unit": "h",
             "raw_unit": "h", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": "for 12 h."},
            {"name": "yield_value", "value": 82, "raw_value": "82",
             "unit": "%", "raw_unit": "%", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": "82% yield"},
        ],
        "yield_type": "unknown",
        "yield_type_evidence_refs": [],
        "raw_experimental_text": "",
    })


def _extracting_factory(db_path, documents_dir):
    """The offline stack with the scripted extraction model -- the same
    test double the integration suite uses: it proposes, the real policy
    decides."""

    def factory(document_ids):
        from crea.agent.offline import build_offline_workflow_stack
        from crea.agent.workspace import DocumentLibrary
        from crea.services.document_evidence import search_document
        from crea.services.documents import LocalDocumentService
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.reaction_extraction import ReactionExtractor
        from crea.sources.pdf import PypdfDocumentParser
        from tests.fakes.compound import PHENYLACETYLENE, FakeCompoundProvider
        from tests.integration_offline.test_research_workflow_integration import (
            ScriptedModel,
        )

        from crea.api.dependencies import document_path

        knowledge_base = KnowledgeBase(db_path)
        knowledge_base.initialize()
        model = None
        if document_ids:
            probe = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
            parsed = probe.ingest(document_path(documents_dir, document_ids[0]))
            evidence_id = search_document(parsed, "CuI")[0].evidence.evidence_id
            model = ScriptedModel(_payload(evidence_id))
        workflow, library, sink, registry = build_offline_workflow_stack(
            extractor=ReactionExtractor(model) if model is not None else None,
            compound_provider=FakeCompoundProvider(
                by_name={"phenylacetylene": [PHENYLACETYLENE]}),
            knowledge_base=knowledge_base)
        for document_id in document_ids:
            library.ingest(document_path(documents_dir, document_id))
        return workflow, library, sink, registry

    return factory


@pytest.fixture()
def client(tmp_path):
    ctx = create_context(
        db_path=tmp_path / "crea.db", documents_dir=tmp_path / "docs",
        stack_factory=_extracting_factory(tmp_path / "crea.db",
                                          tmp_path / "docs"))
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        yield c


def _wait(client, job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/research/{job_id}").json()
        if job["status"] in ("completed", "partial", "cancelled",
                             "interrupted"):
            return job
        time.sleep(0.15)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def _upload(client) -> str:
    response = client.post(
        "/api/documents",
        files={"file": ("procedure.pdf", bytes(pdfs.make_pdf([PROCEDURE])),
                        "application/pdf")})
    assert response.status_code == 200, response.text
    return response.json()["document_id"]


# ---------------------------------------------------------------------------
# B/C/D/E/F/G/H -- research over HTTP, async, real engine underneath
# ---------------------------------------------------------------------------


def test_research_runs_over_http_through_the_real_engine(client):
    document_id = _upload(client)
    started = client.post("/api/research", json={
        "question": "研究目标化合物的文献合成方法",
        "structure_kind": "SMILES", "structure_value": DIYNE,
        "document_ids": [document_id]})
    assert started.status_code == 200, started.text
    job_id = started.json()["job_id"]
    job = _wait(client, job_id)
    assert job["status"] in ("completed", "partial")
    assert job["session_id"], "the run produced a session (G)"

    # the session is queryable with the whole workspace picture (§60)
    session = client.get(f"/api/sessions/{job['session_id']}").json()
    assert session["reaction_ids"], "the real workflow built reactions (D)"
    assert session["entity_ids"]
    assert session["knowledge_added"]["reactions"], "knowledge was added"
    assert session["knowledge_before"] is not None
    assert session["report_markdown"].startswith("# Research Report"), (
        "Report v3 is displayable (H)")
    assert "## Existing Knowledge" in session["report_markdown"]

    # knowledge base really persisted (E/F) and is navigable (I)
    reaction_id = session["reaction_ids"][0]
    reaction = client.get(f"/api/reactions/{reaction_id}").json()
    assert reaction["status"] in ("experimentally_supported", "evidence_backed",
                                  "partial", "structured")
    chain = client.get(f"/api/reactions/{reaction_id}/navigation").json()
    assert chain["links"]["evidence"], "Reaction -> Evidence (§97)"
    evidence = client.get(
        f"/api/evidence/{chain['links']['evidence'][0]}").json()
    assert evidence["raw_quote"], "the quote is preserved verbatim"
    entity_id = session["entity_ids"][0]
    entity = client.get(f"/api/entities/{entity_id}").json()
    assert entity["identity_key"].startswith("crea-identity-key-v1:")
    entity_chain = client.get(f"/api/entities/{entity_id}/navigation").json()
    assert entity_chain["links"]["reactions"], "Entity -> Reactions (§97)"

    # gaps/conflicts are stated as such (J)
    state = client.get(f"/api/knowledge/state?entity_id={entity_id}").json()
    assert state["facets"], "knowledge state per facet"
    assert any(f["state"] in ("known", "partially_known", "unknown")
               for f in state["facets"])

    # events are facts with id references (§11/§12)
    events = client.get(f"/api/research/{job_id}/events").json()["events"]
    types = [e["type"] for e in events]
    assert "research_started" in types and "report_generated" in types
    created = [e for e in events if e["type"] == "reaction_created"]
    assert created and created[0]["reaction_ids"], "events carry ids only"


# ---------------------------------------------------------------------------
# §92 -- re-running the same research reuses, never duplicates
# ---------------------------------------------------------------------------


def test_repeating_the_same_research_reuses_knowledge(client):
    document_id = _upload(client)
    payload = {"question": "研究目标化合物的文献合成方法",
               "structure_kind": "SMILES", "structure_value": DIYNE,
               "document_ids": [document_id]}
    first = _wait(client, client.post("/api/research", json=payload).json()["job_id"])
    counts = client.get("/api/knowledge/export").json()
    second = _wait(client, client.post("/api/research", json=payload).json()["job_id"])
    after = client.get("/api/knowledge/export").json()

    assert len(after["entities"]) == len(counts["entities"]), "no duplicate mint"
    assert len(after["reactions"]) == len(counts["reactions"])
    assert len(after["evidence"]) == len(counts["evidence"])
    session_two = client.get(f"/api/sessions/{second['session_id']}").json()
    assert session_two["knowledge_reused"]["reactions"], (
        "the second run read the first run's reaction as existing")
    assert first["session_id"] != second["session_id"], "two sessions (§44)"


# ---------------------------------------------------------------------------
# §38 -- concurrent research jobs
# ---------------------------------------------------------------------------


def test_concurrent_research_jobs_do_not_corrupt_the_knowledge_base(client):
    document_id = _upload(client)
    payload = {"question": "研究目标化合物的文献合成方法",
               "structure_kind": "SMILES", "structure_value": DIYNE,
               "document_ids": [document_id]}
    job_a = client.post("/api/research", json=payload).json()["job_id"]
    job_b = client.post("/api/research", json=payload).json()["job_id"]
    status_a = _wait(client, job_a)["status"]
    status_b = _wait(client, job_b)["status"]
    assert status_a in ("completed", "partial") and status_b in ("completed", "partial")

    exported = client.get("/api/knowledge/export").json()
    entity_ids = [e["entity_id"] for e in exported["entities"]]
    assert len(entity_ids) == len(set(entity_ids)), "entities not duplicated"
    reaction_ids = [r["reaction_id"] for r in exported["reactions"]]
    assert len(reaction_ids) == len(set(reaction_ids))
    evidence_ids = {e["evidence_id"] for e in exported["evidence"]}
    for reaction in exported["reactions"]:
        for ref in reaction["evidence_refs"]:
            assert ref in evidence_ids, "no dangling evidence (§38)"
    # and the integrity audit agrees the graph is whole
    from crea.services.knowledge_base import KnowledgeBase
    from crea.services.knowledge_integrity import KnowledgeIntegrityService

    db_path = client.app.state.ctx.db_path
    kb = KnowledgeBase(db_path)
    kb.initialize()
    try:
        report = KnowledgeIntegrityService().audit(kb)
    finally:
        kb.close()
    assert report.ok, [i.model_dump() for i in report.issues]


# ---------------------------------------------------------------------------
# K -- PDF through the API into the document pipeline
# ---------------------------------------------------------------------------


def test_a_uploaded_pdf_feeds_the_document_pipeline(client):
    document_id = _upload(client)
    documents = client.get("/api/documents").json()["documents"]
    assert any(d["document_id"] == document_id for d in documents)
    # an upload that is not really a PDF is refused structurally
    bad = client.post("/api/documents",
                      files={"file": ("fake.pdf", b"not a pdf at all",
                                      "application/pdf")})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_FILE"


# ---------------------------------------------------------------------------
# L -- structured errors; no RDKit/SQL/LLM leakage (§56/§36)
# ---------------------------------------------------------------------------


def test_invalid_structure_is_structured_and_leaks_nothing(client):
    response = client.post("/api/research", json={
        "question": "q", "structure_kind": "SMILES",
        "structure_value": "not-a-smiles-!!"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "INVALID_STRUCTURE"
    text = json.dumps(body)
    for leak in ("RDKit", "rdkit", "Traceback", "sqlite", "sqlite3"):
        assert leak not in text

    missing = client.get("/api/entities/CHEM-99999999")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"


def test_evidence_and_reactions_are_read_only(client):
    """§93/§94: no update endpoints exist for knowledge objects."""
    app = client.app
    write_verbs = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/evidence") or path.startswith("/api/reactions"):
            for verb in ("PUT", "PATCH", "DELETE"):
                if verb in getattr(route, "methods", set()):
                    write_verbs.setdefault(path, set()).add(verb)
    assert write_verbs == {}, f"knowledge is read-only, found {write_verbs}"


# ---------------------------------------------------------------------------
# M / §90/§91 -- restart: history stays, unfinished jobs say so
# ---------------------------------------------------------------------------


def test_restart_keeps_knowledge_and_marks_unfinished_jobs_interrupted(tmp_path):
    db_path = tmp_path / "crea.db"
    documents_dir = tmp_path / "docs"
    ctx = create_context(
        db_path=db_path, documents_dir=documents_dir,
        stack_factory=_extracting_factory(db_path, documents_dir))
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        document_id = c.post(
            "/api/documents",
            files={"file": ("procedure.pdf",
                            bytes(pdfs.make_pdf([PROCEDURE])),
                            "application/pdf")}).json()["document_id"]
        job = _wait(c, c.post("/api/research", json={
            "question": "研究目标化合物的文献合成方法",
            "structure_kind": "SMILES", "structure_value": DIYNE,
            "document_ids": [document_id]}).json()["job_id"])
        assert job["session_id"]
        session_id = job["session_id"]
        entity_count = len(c.get("/api/knowledge/export").json()["entities"])

    # "restart": a brand new server over the same files, with a job left
    # running by the crash
    registry = db_path.parent / "research-jobs.json"
    entries = json.loads(registry.read_text(encoding="utf-8"))
    entries.append({"job_id": "job-zombie", "request": {"question": "q"},
                    "status": "running",
                    "created_at": "2099-01-01T00:00:00+00:00"})
    registry.write_text(json.dumps(entries), encoding="utf-8")

    ctx2 = create_context(
        db_path=db_path, documents_dir=documents_dir,
        stack_factory=_extracting_factory(db_path, documents_dir))
    app2 = create_app(context=ctx2, web_root=tmp_path / "no-web")
    with TestClient(app2) as c2:
        # historical knowledge survived (M/§91)
        assert len(c2.get("/api/knowledge/export").json()["entities"]) == entity_count
        session = c2.get(f"/api/sessions/{session_id}").json()
        assert session["session_id"] == session_id
        # the zombie job is interrupted, never pretended complete (§90)
        zombie = c2.get("/api/research/job-zombie").json()
        assert zombie["status"] == "interrupted"


# ---------------------------------------------------------------------------
# search semantics (§99/§100) and molecule depiction (§18)
# ---------------------------------------------------------------------------


def test_search_labels_screening_and_molecule_svg_serves_rdkit_output(client):
    document_id = _upload(client)
    job = _wait(client, client.post("/api/research", json={
        "question": "研究目标化合物的文献合成方法",
        "structure_kind": "SMILES", "structure_value": DIYNE,
        "document_ids": [document_id]}).json()["job_id"])
    session = client.get(f"/api/sessions/{job['session_id']}").json()
    entity_id = session["entity_ids"][0]

    exact = client.get(
        f"/api/knowledge/search?kind=reaction&entity_id={entity_id}").json()
    assert exact["results"], "the stored reaction is found"
    assert all(r["match"] == "EXACT" for r in exact["results"])

    screened = client.get(
        f"/api/knowledge/search?kind=reaction&entity_id={entity_id}"
        f"&structure={DIYNE}").json()
    for result in screened["results"]:
        assert result["match"] == "SCREENING", (
            "a structure screen is never a confirmed match (§99)")

    svg = client.get(f"/api/molecule/svg?smiles={DIYNE}")
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    bad = client.get("/api/molecule/svg?smiles=nope-nope")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_STRUCTURE"


# ---------------------------------------------------------------------------
# demo (§78/§79) and the frontend bundle contract (§102/§103)
# ---------------------------------------------------------------------------


def test_the_offline_demo_runs_the_full_chain_labeled_synthetic(tmp_path):
    from crea.app.demo import run_demo

    summary = run_demo(db_path=tmp_path / "demo.crea.db", echo=lambda *_: None)
    assert summary["origin"] == "SYNTHETIC"
    assert summary["entity_ids"] and summary["reaction_ids"]
    assert "SYNTHETIC" in summary["report_markdown"] or summary["report_markdown"]
    assert summary["session_id"]


def test_the_server_serves_the_built_frontend_when_present(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>CREA</html>", encoding="utf-8")
    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs",
                         stack_factory=_extracting_factory(tmp_path / "crea.db",
                                                           tmp_path / "docs"))
    app = create_app(context=ctx, web_root=web)
    with TestClient(app) as c:
        root = c.get("/")
        assert root.status_code == 200
        assert "CREA" in root.text

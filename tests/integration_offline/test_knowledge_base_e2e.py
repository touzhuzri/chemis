"""Phase 3.11 end-to-end: CREATE -> PERSIST -> CLOSE -> REOPEN -> QUERY -> REUSE.

§73-§83. Every scenario below walks real code paths: `ResearchWorkflow.run`
over the offline stack, the real registries over the real SQLite store, the
real CLI in a real process for Case 8. What is under test is not that bytes
land in tables -- the store tests cover that -- but that *research knowledge*
survives the process boundary with its identity, its provenance and its
relations intact, and that a second research run reuses what the first
established instead of minting `CHEM-00000099` (§41).

The extraction model is scripted (it is allowed to propose); everything
below it is the real policy, identity and knowledge layers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from crea.core.models.entity import ChemicalMention
from crea.core.models.identity import StructureInput
from crea.core.models.literature import Paper
from crea.core.models.reaction import ComponentRole
from crea.core.models.reaction_knowledge import (
    ReactionComponent,
    RouteStepReactionLink,
    reaction_key,
)
from crea.core.models.research import ResearchTask
from crea.core.models.route import ReactionMatchType
from crea.core.models.session import WorkflowStatus
from crea.repositories.errors import ConstraintViolation
from crea.services.knowledge_base import KnowledgeBase
from tests.fakes import pdfs
from tests.fakes.compound import PHENYLACETYLENE, FakeCompoundProvider
from tests.integration_offline.test_research_workflow_integration import (
    DIYNE,
    DIYNE_INCHI,
    PROCEDURE,
    ScriptedModel,
    _evidence_id_of,
    _payload,
)
from tests.unit.test_repositories_sqlite import make_evidence, make_reaction


def _stack(tmp_path, kb, *, lines=PROCEDURE, product_name="compound 8",
           product_raw="Compound 8", extractor=True):
    """One workflow over the offline stack, wired to `kb`, one PDF supplied.

    One entity registry and one reaction registry per knowledge base: the
    identity tool, the reaction tools and the workflow all write to the
    same place, or run two of these and the knowledge splits in two.
    """
    from crea.agent.offline import build_offline_workflow_stack
    from crea.services.reaction_extraction import ReactionExtractor

    path = tmp_path / "procedure.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdfs.make_pdf([lines]))

    model = None
    if extractor:
        model = ScriptedModel(_payload(_evidence_id_of(path),
                                       product_name, product_raw))
    workflow, library, sink, registry = build_offline_workflow_stack(
        extractor=ReactionExtractor(model) if model is not None else None,
        compound_provider=FakeCompoundProvider(
            by_name={"phenylacetylene": [PHENYLACETYLENE]}),
        knowledge_base=kb)
    parsed = library.ingest(path)
    return workflow, registry, parsed.document.document_id


def _run(tmp_path, kb, *, lines=PROCEDURE, paper_id="", doi="",
         question="研究目标化合物的文献合成方法", **stack_kwargs):
    workflow, _registry, document_id = _stack(tmp_path, kb, lines=lines,
                                              **stack_kwargs)
    task = ResearchTask(task_id="t-" + tmp_path.name, question=question)
    report = workflow.run(
        task,
        structure_input=StructureInput.of("SMILES", DIYNE),
        document_id=document_id,
        document_query="CuI",
        paper_id=paper_id,
        doi=doi,
    )
    return workflow.session, report


# ---------------------------------------------------------------------------
# §73 -- Case 1: persist, close the process, reopen, query everything
# ---------------------------------------------------------------------------


def test_case_1_the_workflows_knowledge_survives_a_process_boundary(tmp_path):
    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    session, report = _run(tmp_path / "run", kb)

    assert session.reaction_ids, "the run produced reactions"
    assert session.chemical_entity_ids, "the run produced entities"
    assert report.routes, "the run produced candidate routes"
    reaction_ids = list(session.reaction_ids)
    entity_ids = list(session.chemical_entity_ids)
    session_id = session.session_id
    kb.close()  # the process ends here

    reopened = KnowledgeBase(db)
    reopened.initialize()

    # entity
    entity = reopened.get_entity(entity_ids[0])
    assert entity.entity_id == entity_ids[0]
    assert entity.identity_key.startswith("crea-identity-key-v1:")

    # reaction (+ its components, mentions, entities)
    reaction = reopened.get_reaction(reaction_ids[0])
    assert reaction.reaction_id == reaction_ids[0]
    assert reaction.components(), "components survived"
    assert any(c.mention is not None for c in reaction.components()), (
        "the mention layer survived")

    # paper -- the literature discovery's own record
    papers = reopened.list_papers()
    assert papers, "discovered papers were persisted"
    assert reopened.get_paper(papers[0].paper_id).paper_id == papers[0].paper_id

    # evidence -- what the reaction actually cites
    evidence = reopened.get_evidence_for_reaction(reaction_ids[0])
    assert evidence, "the reaction's citations resolve after reopening"
    assert evidence[0].raw_quote, "the quote is stored verbatim"

    # route
    routes = reopened.list_routes()
    assert routes, "candidate routes were persisted"
    assert reopened.get_route(routes[0].route_id).steps

    # session
    stored = reopened.get_session(session_id)
    assert stored.status is WorkflowStatus.COMPLETED
    assert stored.reaction_ids == reaction_ids
    assert stored.chemical_entity_ids == entity_ids
    reopened.close()


# ---------------------------------------------------------------------------
# §74 -- Case 2: the same PDF researched twice must not double anything
# ---------------------------------------------------------------------------


def test_case_2_researching_the_same_pdf_twice_does_not_double_the_knowledge(tmp_path):
    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    session_one, _report_one = _run(tmp_path / "one", kb)
    snapshot = (len(kb.list_entities()), len(kb.list_reactions()),
                len(kb.list_evidence()), len(kb.list_mentions()),
                len(kb.list_papers()), len(kb.list_routes()))
    session_two, _report_two = _run(tmp_path / "one", kb, lines=PROCEDURE)

    assert snapshot == (len(kb.list_entities()), len(kb.list_reactions()),
                        len(kb.list_evidence()), len(kb.list_mentions()),
                        len(kb.list_papers()), len(kb.list_routes())), (
        "a second look at the same PDF adds no knowledge twice (§42)")
    # ... but it is a second research run, and sessions accumulate (§44)
    assert session_one.session_id != session_two.session_id
    assert len(kb.list_sessions()) == 2
    # and both sessions point at the same established knowledge
    assert session_one.reaction_ids == session_two.reaction_ids
    assert session_one.chemical_entity_ids == session_two.chemical_entity_ids
    kb.close()


# ---------------------------------------------------------------------------
# §75 -- Case 3: incremental research accumulates
# ---------------------------------------------------------------------------


def test_case_3_incremental_research_adds_without_replacing(tmp_path):
    """Paper A, then Paper A again plus Paper B: the database ends holding
    both papers and both reactions -- not just the second run (§43)."""
    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    # the two papers of the scenario are known works; the PDFs are them
    for paper_id in ("paper-a", "paper-b"):
        kb.save_paper(Paper(paper_id=paper_id, title=paper_id,
                            retrieved_at="2026-10-02T00:00:00+00:00"))

    session_a, _ = _run(tmp_path / "read-a", kb, paper_id="paper-a")
    reaction_a = session_a.reaction_ids[0]
    # the second research effort re-reads A and adds B
    _session_a2, _ = _run(tmp_path / "read-a", kb, paper_id="paper-a")
    session_b, _ = _run(tmp_path / "read-b", kb, lines=PROCEDURE + ["extra line"],
                        paper_id="paper-b")

    assert kb.get_paper("paper-a").paper_id == "paper-a"
    assert kb.get_paper("paper-b").paper_id == "paper-b"
    assert reaction_a in [r.reaction_id for r in kb.list_reactions()]
    assert session_b.reaction_ids[0] in [r.reaction_id for r in kb.list_reactions()]
    assert session_b.reaction_ids[0] != reaction_a
    # Paper A is not duplicated and its reaction was not re-minted
    assert len(kb.search_reactions(paper_id="paper-a")) == 1
    assert len(kb.search_reactions(paper_id="paper-b")) == 1
    assert len(kb.list_sessions()) == 3
    kb.close()


# ---------------------------------------------------------------------------
# §76 -- Case 4: compound numbers are paper-local, through the database
# ---------------------------------------------------------------------------


def test_case_4_compound_7_stays_paper_local_across_the_database(tmp_path):
    """Paper A: compound 7 = phenylacetylene. Paper B: compound 7 = the
    diyne. Same words, one knowledge base, two entities -- and reopening
    the database must not collapse the scopes (§20)."""
    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    for paper_id in ("paper-a", "paper-b"):
        kb.save_paper(Paper(paper_id=paper_id, title=paper_id,
                            retrieved_at="2026-10-02T00:00:00+00:00"))

    lines_a = [
        "Experimental",
        "Compound 7 (phenylacetylene) was used as received.",
        "CuI (10 mol%) was added.",
        "The mixture was stirred at 80 C for 12 h.",
        "The product was isolated in 82% yield.",
    ]
    lines_b = [
        "Experimental",
        f"Compound 7 ({DIYNE_INCHI}) was recrystallized.",
        "CuI (10 mol%) was added.",
        "The mixture was stirred at 80 C for 12 h.",
        "The product was isolated in 82% yield.",
    ]
    _session_a, report_a = _run(tmp_path / "paper-a", kb, lines=lines_a,
                                product_name="compound 7",
                                product_raw="Compound 7", paper_id="paper-a")
    _session_b, report_b = _run(tmp_path / "paper-b", kb, lines=lines_b,
                                product_name="compound 7",
                                product_raw="Compound 7", paper_id="paper-b")

    def product_entity(report):
        reaction = report.reactions[0]
        product = next(c for c in reaction.products
                       if (c.name or "").lower() == "compound 7")
        assert product.entity_id, "each product resolved to a real entity"
        return product.entity_id

    entity_a = product_entity(report_a)
    entity_b = product_entity(report_b)
    assert entity_a != entity_b, "the same words, two papers, two entities"
    kb.close()

    # reopen: the scopes are still separate (§76)
    reopened = KnowledgeBase(db)
    reopened.initialize()
    def seven(paper_id):
        return [m for m in reopened.get_mentions_for_paper(paper_id)
                if m.raw_text.lower() == "compound 7"]
    mentions_a, mentions_b = seven("paper-a"), seven("paper-b")
    assert mentions_a and mentions_b
    assert mentions_a[0].entity_id == entity_a
    assert mentions_b[0].entity_id == entity_b
    assert mentions_a[0].mention_id != mentions_b[0].mention_id
    # and the words themselves are names of neither entity
    for entity_id in (entity_a, entity_b):
        names = [n.lower() for n in reopened.get_entity(entity_id).names]
        assert "compound 7" not in names
    reopened.close()


# ---------------------------------------------------------------------------
# §77 -- Case 5: reaction evidence is only what resolves
# ---------------------------------------------------------------------------


def test_case_5_evidence_citations_survive_and_never_borrow(tmp_path):
    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    session, report = _run(tmp_path / "run", kb)
    reaction_id = session.reaction_ids[0]
    cited = [e.evidence_id for e in kb.get_evidence_for_reaction(reaction_id)]
    assert cited, "the reaction's evidence is real and stored"
    assert set(cited) <= set(session.evidence_ids)
    kb.close()

    reopened = KnowledgeBase(db)
    reopened.initialize()
    recovered = reopened.get_evidence_for_reaction(reaction_id)
    assert [e.evidence_id for e in recovered] == cited, (
        "get_evidence_for_reaction(R) returns exactly E after reopening")
    # a reaction with no citations gets nothing -- and is not treated as
    # supported by evidence that was never its own
    bare = make_reaction(
        "RXN-bare", key=reaction_key(["e:br"], ["e:bp"]))
    reopened.save_reaction(bare)
    assert reopened.get_evidence_for_reaction("RXN-bare") == []
    assert reopened.get_reaction("RXN-bare").evidence_refs == []
    reopened.close()


# ---------------------------------------------------------------------------
# §78 -- Case 6: route links survive and stay measured
# ---------------------------------------------------------------------------


def test_case_6_route_step_reaction_links_survive_reopening(tmp_path):
    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    session, report = _run(tmp_path / "run", kb)
    assert report.reaction_links, "the run measured step<->reaction links"
    reaction_id = session.reaction_ids[0]
    kb.close()

    reopened = KnowledgeBase(db)
    reopened.initialize()
    recovered = reopened.get_routes_for_reaction(reaction_id)
    assert recovered, "the measured link is a stored relation (§68)"
    route_id, link = recovered[0]
    assert route_id, "the link keeps its route context (§23)"
    assert link.reaction_id == reaction_id
    assert link.step_id in {s.step_id for r in reopened.list_routes()
                            for s in r.steps}
    exact = [l for _r, l in recovered if not l.screening_only]
    assert exact, "the coupling step linked exactly in this scenario"
    # nothing is generated at query time: a fresh link object is never
    # invented for an unlinked reaction
    assert reopened.get_routes_for_reaction("RXN-nope") == []
    reopened.close()


# ---------------------------------------------------------------------------
# §79 -- Case 7: a broken commit leaves nothing half-stored
# ---------------------------------------------------------------------------


def test_case_7_a_dangling_citation_rolls_the_commit_back(tmp_path):
    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    session, _report = _run(tmp_path / "run", kb)
    before = (len(kb.list_reactions()), len(kb.list_entities()))
    kb.close()

    reopened = KnowledgeBase(db)
    reopened.initialize()
    from crea.core.models.session import ResearchSession

    broken = make_reaction("RXN-broken", key=reaction_key(["e:xr"], ["e:xp"]),
                           evidence_refs=["EVIDENCE-never-existed"])
    doomed = ResearchSession(
        session_id="doomed", task=ResearchTask(task_id="t", question="q"),
        reaction_ids=["RXN-broken"])
    with pytest.raises(ConstraintViolation):
        reopened.persist_research_result(session=doomed, reactions=[broken])
    assert reopened.store.get_reaction("RXN-broken") is None, (
        "no half a reaction (§51/§79)")
    assert reopened.store.get_session("doomed") is None
    assert (len(reopened.list_reactions()), len(reopened.list_entities())) == before
    reopened.close()


# ---------------------------------------------------------------------------
# §80-§83 -- Case 8: the real CLI, then query, then research reuses
# ---------------------------------------------------------------------------


def _cli(tmp_path, *argv):
    src = str(tmp_path / ".." / ".." / "src")
    src = os.path.abspath(str((__import__("pathlib").Path(__file__)
                               .resolve().parents[2] / "src")))
    env = dict(os.environ)
    env["PYTHONPATH"] = src
    return subprocess.run(
        [sys.executable, "-m", "crea.cli", *argv],
        capture_output=True, text=True, encoding="utf-8",
        env=env, cwd=str(tmp_path), timeout=180)


def test_case_8_the_cli_persists_queries_and_reuses(tmp_path):
    """`python -m crea.cli research ... --db` in real processes: research
    writes knowledge, `kb` commands answer from it without re-researching,
    and a second research run reuses the first's entity (§83)."""
    db = tmp_path / "crea.db"

    # seed the knowledge base with an established reaction (the in-process
    # workflow is the extraction path; the CLI demo stack has no extractor)
    kb = KnowledgeBase(db)
    kb.initialize()
    session, _report = _run(tmp_path / "seed", kb, paper_id="paper-a")
    reaction_id = session.reaction_ids[0]
    entity_id = session.chemical_entity_ids[0]
    kb.save_paper(Paper(paper_id="paper-a", title="seeded",
                        retrieved_at="2026-10-02T00:00:00+00:00"))
    kb.close()

    # 1) real process research against the same database
    first = _cli(tmp_path, "research", "--question", "Analyze",
                 "--smiles", DIYNE, "--db", str(db))
    assert first.returncode == 0, first.stderr
    assert "# Research Report" in first.stdout

    # 2) queries answer from accumulated knowledge, no research involved
    got_entity = _cli(tmp_path, "kb", "entity", entity_id,
                      "--db", str(db), "--json")
    assert got_entity.returncode == 0, got_entity.stderr
    payload = json.loads(got_entity.stdout)
    assert payload["entity_id"] == entity_id

    got_reactions = _cli(tmp_path, "kb", "reactions", entity_id,
                         "--db", str(db), "--json")
    assert got_reactions.returncode == 0
    reactions_payload = json.loads(got_reactions.stdout)
    assert any(r["reaction_id"] == reaction_id
               for r in reactions_payload["reactions"]), (
        "the seeded reaction is queryable by its entity")

    got_one = _cli(tmp_path, "kb", "reaction", reaction_id,
                   "--db", str(db), "--json")
    assert got_one.returncode == 0
    assert json.loads(got_one.stdout)["reaction_id"] == reaction_id

    got_evidence = _cli(tmp_path, "kb", "evidence", reaction_id,
                        "--db", str(db), "--json")
    assert got_evidence.returncode == 0
    assert json.loads(got_evidence.stdout)["count"] >= 1

    missing = _cli(tmp_path, "kb", "entity", "CHEM-99999999", "--db", str(db))
    assert missing.returncode == 1, "a lookup miss is a refusal, not a crash"

    # 3) a second research run reuses the first's knowledge
    second = _cli(tmp_path, "research", "--question", "Analyze",
                  "--smiles", DIYNE, "--db", str(db))
    assert second.returncode == 0, second.stderr

    export = tmp_path / "knowledge.json"
    exported = _cli(tmp_path, "kb", "export", "--output", str(export),
                    "--db", str(db))
    assert exported.returncode == 0, exported.stderr
    payload = json.loads(export.read_text(encoding="utf-8"))
    entity_ids = [e["entity_id"] for e in payload["entities"]]
    assert entity_ids.count(entity_id) == 1, "no duplicate entity was minted"
    # sessions accumulate; knowledge does not
    assert len(payload["sessions"]) >= 3
    reaction_ids = [r["reaction_id"] for r in payload["reactions"]]
    assert len(reaction_ids) == len(set(reaction_ids))
    assert reaction_id in reaction_ids


# ---------------------------------------------------------------------------
# §60/§61 -- the agent's knowledge tools answer from established knowledge
# ---------------------------------------------------------------------------


def test_the_agent_knowledge_tools_answer_without_researching_the_internet(tmp_path):
    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    session, _report = _run(tmp_path / "run", kb, paper_id="paper-a")
    kb.save_paper(Paper(paper_id="paper-a", title="seeded",
                        retrieved_at="2026-10-02T00:00:00+00:00"))
    # the paper relation is written when the paper is stored: link the
    # reaction's mentions to it now that paper-a exists
    reaction = kb.get_reaction(session.reaction_ids[0])
    for component in reaction.components():
        if component.mention is not None:
            kb.link_paper_mention("paper-a", component.mention.mention_id)
    entity_id = session.chemical_entity_ids[0]

    # a fresh stack over the same knowledge base: the "next session"
    workflow, registry, _document_id = _stack(tmp_path / "next", kb)

    # "我们以前研究过 phenylacetylene 的哪些反应？" -> knowledge, not the internet
    found = registry.execute("search_reactions", {"paper_id": "paper-a"})
    assert found.status.value == "ok"
    assert any(r["reaction_id"] == session.reaction_ids[0]
               for r in found.data["reactions"])

    got = registry.execute("get_entity", {"entity_id": entity_id})
    assert got.data["status"] == "ok"
    assert got.data["entity_id"] == entity_id

    by_name = registry.execute("search_entities", {"name": "phenylacetylene"})
    assert by_name.data["status"] == "ok"
    assert by_name.data["entities"], "the name finds what was established"

    papers = registry.execute("get_papers_for_entity", {"entity_id": entity_id})
    assert papers.data["status"] == "ok"

    evidence = registry.execute("get_evidence_for_reaction",
                                {"reaction_id": session.reaction_ids[0]})
    assert evidence.data["count"] >= 1

    routes = registry.execute("get_routes_for_entity", {"entity_id": entity_id})
    assert routes.data["status"] == "ok"

    invalid = registry.execute("search_entities", {})
    assert invalid.data["status"] == "invalid_query"
    kb.close()

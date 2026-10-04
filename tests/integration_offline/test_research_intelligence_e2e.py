"""Phase 3.12 end-to-end: knowledge-aware research, across restarts.

§74-§84. The point of this phase is the *loop*: research produces
knowledge, knowledge drives the next research, and the next run
recognizes what exists, finds the gaps, and adds only what is missing.

    Research #1 -> knowledge -> persist
    Research #2 -> read existing -> gaps -> only the missing work
    Research #3 -> keep growing, never lose the old

Every case walks real code paths -- `ResearchWorkflow.run` over the
offline stack, the real registries over the real SQLite store, the real
CLI in real processes (§114). The extraction model is scripted (it may
propose); everything below it is the real policy and knowledge layer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from crea.core.models.identity import StructureInput
from crea.core.models.knowledge import KnowledgeFacet, ResearchPlanAction
from crea.core.models.reaction_knowledge import ReactionStatus
from crea.core.models.research import ResearchTask
from crea.core.models.session import WorkflowStatus
from crea.services.knowledge_base import KnowledgeBase
from crea.services.knowledge_integrity import KnowledgeIntegrityService
from tests.fakes import pdfs
from tests.fakes.compound import PHENYLACETYLENE, FakeCompoundProvider
from tests.integration_offline.test_knowledge_base_e2e import _run
from tests.integration_offline.test_research_workflow_integration import (
    DIYNE,
    DIYNE_INCHI,
    ScriptedModel,
    _evidence_id_of,
)
from tests.unit.test_repositories_sqlite import make_entity


def _payload_at(evidence_id: str, *, temperature: int, yield_pct: int,
                product_raw: str = "Compound 8") -> str:
    """A valid extraction proposal for one procedure's own numbers.

    Every number and raw string comes from the passage the policy checks
    against -- the model proposes, and a value the text does not contain
    is dropped. Two papers, two payloads, two records.
    """
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
             "evidence_refs": [evidence_id], "raw_text": product_raw},
        ],
        "conditions": [
            {"name": "temperature", "value": temperature,
             "raw_value": str(temperature), "unit": "C", "raw_unit": "C",
             "status": "EXPLICIT", "evidence_refs": [evidence_id],
             "raw_text": f"stirred at {temperature} C"},
            {"name": "time", "value": 12, "raw_value": "12", "unit": "h",
             "raw_unit": "h", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": "for 12 h."},
            {"name": "yield_value", "value": yield_pct,
             "raw_value": str(yield_pct), "unit": "%", "raw_unit": "%",
             "status": "EXPLICIT", "evidence_refs": [evidence_id],
             "raw_text": f"{yield_pct}% yield"},
        ],
        "yield_type": "unknown",
        "yield_type_evidence_refs": [],
        "raw_experimental_text": "",
    })


def _procedure(temperature: int, yield_pct: int) -> list[str]:
    return [
        "Experimental",
        "Phenylacetylene (1.0 mmol),",
        "CuI (10 mol%),",
        "were dissolved in DMF (5 mL).",
        f"The mixture was stirred at {temperature} C",
        "for 12 h.",
        f"The product was isolated in {yield_pct}% yield.",
        f"Compound 8 ({DIYNE_INCHI}) was purified.",
    ]


def _run_with(tmp_path, kb, *, lines, paper_id="", question="研究目标化合物的文献合成方法",
              temperature=80, yield_pct=82):
    """One research run whose extraction proposes this procedure's numbers."""
    from crea.agent.offline import build_offline_workflow_stack
    from crea.services.reaction_extraction import ReactionExtractor

    path = tmp_path / "procedure.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdfs.make_pdf([lines]))
    model = ScriptedModel(_payload_at(
        _evidence_id_of(path), temperature=temperature, yield_pct=yield_pct))
    workflow, library, sink, registry = build_offline_workflow_stack(
        extractor=ReactionExtractor(model),
        compound_provider=FakeCompoundProvider(
            by_name={"phenylacetylene": [PHENYLACETYLENE]}),
        knowledge_base=kb)
    parsed = library.ingest(path)
    task = ResearchTask(task_id="t-" + tmp_path.name, question=question)
    report = workflow.run(
        task,
        structure_input=StructureInput.of("SMILES", DIYNE),
        document_id=parsed.document.document_id,
        document_query="CuI",
        paper_id=paper_id,
    )
    return workflow.session, report, task


# ---------------------------------------------------------------------------
# §74 -- E2E A: first research produces knowledge and persists it
# ---------------------------------------------------------------------------


def test_e2e_a_first_research_establishes_and_persists_knowledge(tmp_path):
    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    session, report, _task = _run_with(
        tmp_path / "run1", kb, lines=_procedure(80, 82), paper_id="paper-a")

    assert report.knowledge is not None, "the report is knowledge-aware"
    assert not report.knowledge.existing.get("reactions"), "nothing existed"
    assert report.knowledge.new.get("reactions"), "this run built reactions"
    assert session.reaction_ids
    reaction_id = session.reaction_ids[0]
    kb.close()

    reopened = KnowledgeBase(db)
    reopened.initialize()
    assert reopened.get_reaction(reaction_id) is not None
    assert reopened.get_reaction_records(reaction_id), "record chain persisted"
    assert reopened.get_evidence_for_reaction(reaction_id)
    assert reopened.get_session(session.session_id).status in (
        WorkflowStatus.COMPLETED, WorkflowStatus.PARTIAL)
    reopened.close()


# ---------------------------------------------------------------------------
# §76 -- E2E B: a restart + second research recognizes existing knowledge
# ---------------------------------------------------------------------------


def test_e2e_b_second_research_recognizes_existing_and_reuses(tmp_path):
    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    session_one, _report_one, _task_one = _run_with(
        tmp_path / "run1", kb, lines=_procedure(80, 82), paper_id="paper-a")
    reaction_ids = list(session_one.reaction_ids)
    entities_after_one = len(kb.list_entities())
    kb.close()

    # a whole new process: same target, same question
    reopened = KnowledgeBase(db)
    reopened.initialize()
    session_two, report_two, task_two = _run_with(
        tmp_path / "run1", reopened, lines=_procedure(80, 82),
        paper_id="paper-a")

    assert report_two.knowledge.existing.get("reactions"), (
        "the second run saw the first run's reaction as EXISTING")
    assert set(report_two.knowledge.existing["reactions"]) >= set(reaction_ids)
    assert not report_two.knowledge.new.get("reactions"), (
        "no new reaction was minted for knowledge that already existed (§41)")
    assert not report_two.knowledge.new.get("entities"), (
        "same identity: no second entity (§41)")
    assert session_two.reaction_ids == reaction_ids
    assert len(reopened.list_reactions()) == 1, "no RXN-00000099"
    assert len(reopened.list_entities()) == entities_after_one

    # and the plan did NOT mechanically re-search (§39)
    skipped = [s for s in task_two.steps
               if s.tool == "search_literature" and s.result_status is None]
    assert skipped, "search_literature was deliberately not run"
    assert any("知识已覆盖" in (s.note or "") for s in skipped)
    reopened.close()


# ---------------------------------------------------------------------------
# §77 -- E2E C: incremental research grows the knowledge base
# ---------------------------------------------------------------------------


def test_e2e_c_incremental_research_keeps_old_and_adds_new(tmp_path):
    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    session_a, _report_a, _task_a = _run_with(
        tmp_path / "paper-a", kb, lines=_procedure(80, 82), paper_id="paper-a")
    reaction_a = session_a.reaction_ids[0]
    entities_before = len(kb.list_entities())
    evidence_a = {e.evidence_id for e in kb.list_evidence()}

    # a second research effort: a NEW paper with its own procedure
    session_b, report_b, _task_b = _run_with(
        tmp_path / "paper-b", kb, lines=_procedure(60, 71),
        paper_id="paper-b", temperature=60, yield_pct=71)

    reaction_b = session_b.reaction_ids[0]
    assert reaction_b != reaction_a
    assert {r.reaction_id for r in kb.list_reactions()} >= {reaction_a, reaction_b}
    assert len(kb.list_entities()) == entities_before, (
        "same identity: the entities did not double (§101)")
    assert evidence_a <= {e.evidence_id for e in kb.list_evidence()}, (
        "old evidence is never lost")
    assert kb.get_evidence_for_reaction(reaction_a), (
        "Reaction A's evidence still resolves")
    assert report_b.knowledge.new.get("reactions"), "Reaction B is NEW"
    assert reaction_a in (report_b.knowledge.existing.get("reactions") or []), (
        "Reaction A is EXISTING -- old knowledge is not replaced (§43)")
    kb.close()


# ---------------------------------------------------------------------------
# §78 -- E2E D: conflicting conditions are CONFLICT with both citations
# ---------------------------------------------------------------------------


def test_e2e_d_conflicting_conditions_are_kept_as_conflict(tmp_path):
    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    session_a, _report_a, _task_a = _run_with(
        tmp_path / "paper-a", kb, lines=_procedure(80, 82), paper_id="paper-a")
    session_b, report_b, _task_b = _run_with(
        tmp_path / "paper-b", kb, lines=_procedure(60, 71),
        paper_id="paper-b", temperature=60, yield_pct=71)

    conflicts = [c for c in report_b.knowledge.conflicts
                 if c.field == "temperature"]
    assert conflicts, "80 °C vs 60 °C is a CONFLICT, kept (§23)"
    conflict = conflicts[0]
    values = {o.raw_value for o in conflict.observations}
    assert values == {"80", "60"}, "both values kept -- no average (§22)"
    cited = {o.evidence_id for o in conflict.observations}
    assert len(cited) >= 2, "each value keeps its own evidence (§24)"
    papers = {o.paper_id for o in conflict.observations}
    assert papers == {"paper-a", "paper-b"}

    # and the rendered report shows both, says CONFLICT, picks nothing
    from crea.report.research import render_research_report_v3

    markdown = render_research_report_v3(report_b)
    block = markdown.split("## Conflicts")[1].split("##")[0]
    assert "CONFLICT" in block
    assert "80" in block and "60" in block
    assert "70 C" not in block, "the average temperature never appears"
    # both reactions survive in the knowledge base
    assert len(kb.list_reactions()) == 2
    kb.close()


# ---------------------------------------------------------------------------
# §79 -- E2E E: a candidate without evidence stays CANDIDATE
# ---------------------------------------------------------------------------


def test_e2e_e_a_candidate_without_evidence_never_becomes_supported(tmp_path):
    from crea.core.models.entity import ChemicalEntity
    from crea.core.models.reaction import ComponentRole
    from crea.core.models.reaction_knowledge import (
        Reaction,
        ReactionComponent,
    )

    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    # a bare proposal against THIS run's target identity: someone said the
    # reaction exists. Nothing more -- no evidence, no record.
    entity_id = "CHEM-00000001"
    target = ChemicalEntity(
        entity_id=entity_id,
        identity_key=f"crea-identity-key-v1:{DIYNE_INCHI}",
        canonical_smiles=DIYNE, inchi=DIYNE_INCHI, inchikey="DIYNEKEY")
    kb.save_entity(target)
    bare = Reaction(
        reaction_id="RXN-bare",
        reaction_key="crea-reaction-key-v1:R=e:x|P=e:y|O=|S=",
        reactants=[ReactionComponent(role=ComponentRole.REACTANT, name="x",
                                     entity_id=entity_id)],
        status=ReactionStatus.CANDIDATE)
    kb.save_reaction(bare)
    kb.close()

    reopened = KnowledgeBase(db)
    reopened.initialize()
    # a research run over the same target, with no extraction available:
    # the candidate is the only reaction knowledge there is
    _session, report = _run(
        tmp_path / "run", reopened, extractor=False,
        question="研究目标化合物的文献合成方法")
    stored = reopened.get_reaction("RXN-bare")
    assert stored.status is ReactionStatus.CANDIDATE, (
        "the database never promotes a status (§49)")
    assert reopened.get_evidence_for_reaction("RXN-bare") == []
    # the coverage says the gap plainly -- structures known, conditions not
    gaps = [g.reason for g in report.knowledge.gaps]
    assert any("尚未找到可验证的实验条件证据" in reason for reason in gaps)
    reopened.close()


# ---------------------------------------------------------------------------
# §80 -- E2E F: metadata-only knowledge stays DISCOVERY
# ---------------------------------------------------------------------------


def test_e2e_f_metadata_only_sources_stay_discovery(tmp_path):
    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    # no extraction model: the only knowledge available is metadata
    # (title/abstract/DOI) and prose -- never an experiment
    session, report = _run(
        tmp_path / "meta", kb, extractor=False,
        question="研究目标化合物的文献合成方法")

    assert report.sources, "literature discovery found the paper"
    assert not kb.list_reactions(), (
        "metadata never becomes a reaction (§29)")
    for reaction in kb.list_reactions():
        assert reaction.status is not ReactionStatus.EXPERIMENTALLY_SUPPORTED
    from crea.report.research import render_research_report_v3

    markdown = render_research_report_v3(report)
    assert "[Discovery]" in markdown or "DISCOVERY" in markdown
    assert "该文献被发现，但当前没有足够实验文本建立 Reaction。" in markdown
    kb.close()


# ---------------------------------------------------------------------------
# §74/§82 -- knowledge growth across three research runs
# ---------------------------------------------------------------------------


def test_three_research_runs_grow_knowledge_without_duplicating(tmp_path):
    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    _s1, _r1, _t1 = _run_with(
        tmp_path / "paper-a", kb, lines=_procedure(80, 82), paper_id="paper-a")
    after_first = (len(kb.list_entities()), len(kb.list_reactions()),
                   len(kb.list_evidence()))
    _s2, _r2, _t2 = _run_with(
        tmp_path / "paper-b", kb, lines=_procedure(60, 71),
        paper_id="paper-b", temperature=60, yield_pct=71)
    after_second = (len(kb.list_entities()), len(kb.list_reactions()),
                    len(kb.list_evidence()))
    # third run: the same first paper again
    _s3, _r3, _t3 = _run_with(
        tmp_path / "paper-a", kb, lines=_procedure(80, 82), paper_id="paper-a")

    entities_1, reactions_1, evidence_1 = after_first
    entities_2, reactions_2, evidence_2 = after_second
    assert entities_2 == entities_1, "Entities: identity unchanged, count unchanged"
    assert reactions_2 == reactions_1 + 1, "Reactions: +1 for the new paper"
    assert evidence_2 > evidence_1, "Evidence: the new paper added its own"
    assert (len(kb.list_entities()), len(kb.list_reactions()),
            len(kb.list_evidence())) == after_second, (
        "the third run (a re-read) changed no counts")
    assert len(kb.list_sessions()) == 3, "every run's session is kept (§44)"
    kb.close()


# ---------------------------------------------------------------------------
# §84 -- real processes: research, query, audit, export
# ---------------------------------------------------------------------------


def _cli(tmp_path, *argv):
    src = os.path.abspath(str((__import__("pathlib").Path(__file__)
                               .resolve().parents[2] / "src")))
    env = dict(os.environ)
    env["PYTHONPATH"] = src
    return subprocess.run(
        [sys.executable, "-m", "crea.cli", *argv],
        capture_output=True, text=True, encoding="utf-8",
        env=env, cwd=str(tmp_path), timeout=180)


def test_cli_research_query_audit_export_in_real_processes(tmp_path):
    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    session, _report, _task = _run_with(
        tmp_path / "seed", kb, lines=_procedure(80, 82), paper_id="paper-a")
    entity_id = session.chemical_entity_ids[0]
    reaction_id = session.reaction_ids[0]
    kb.close()

    first = _cli(tmp_path, "research", "--question", "研究合成方法",
                 "--smiles", DIYNE, "--db", str(db))
    assert first.returncode == 0, first.stderr
    assert "# Research Report" in first.stdout
    assert "## Existing Knowledge" in first.stdout, "the report is knowledge-aware"

    second = _cli(tmp_path, "research", "--question", "研究合成方法",
                  "--smiles", DIYNE, "--db", str(db))
    assert second.returncode == 0, second.stderr

    got = _cli(tmp_path, "kb", "reactions", entity_id, "--db", str(db),
               "--json")
    assert got.returncode == 0, got.stderr
    payload = json.loads(got.stdout)
    assert any(r["reaction_id"] == reaction_id for r in payload["reactions"])

    audit = _cli(tmp_path, "kb", "audit", "--db", str(db), "--json")
    assert audit.returncode == 0, audit.stdout + audit.stderr
    assert json.loads(audit.stdout)["ok"] is True

    export = tmp_path / "knowledge.json"
    exported = _cli(tmp_path, "kb", "export", "--output", str(export),
                    "--db", str(db))
    assert exported.returncode == 0, exported.stderr
    data = json.loads(export.read_text(encoding="utf-8"))
    reaction_ids = [r["reaction_id"] for r in data["reactions"]]
    assert reaction_ids.count(reaction_id) == 1, "reuse, not duplicates (§83)"
    assert data["reaction_records"], "the record chain is exported too"


# ---------------------------------------------------------------------------
# §37-§38 -- the knowledge-aware agent tools
# ---------------------------------------------------------------------------


def test_the_agent_tools_answer_from_knowledge_not_the_internet(tmp_path):
    db = tmp_path / "crea.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    session, _report, _task = _run_with(
        tmp_path / "run", kb, lines=_procedure(80, 82), paper_id="paper-a")
    entity_id = session.chemical_entity_ids[0]
    reaction_id = session.reaction_ids[0]
    kb.close()

    reopened = KnowledgeBase(db)
    reopened.initialize()
    from crea.agent.offline import build_offline_workflow_stack

    _workflow, _library, _sink, registry = build_offline_workflow_stack(
        knowledge_base=reopened)

    existing = registry.execute("get_existing_knowledge",
                                {"entity_id": entity_id})
    assert existing.data["status"] == "ok"
    assert any(r["reaction_id"] == reaction_id
               for r in existing.data["reactions"])
    assert "不代表当前最新" in existing.data["note"], "§15"

    gaps = registry.execute("get_knowledge_gaps", {"entity_id": entity_id})
    assert gaps.data["status"] == "ok"

    found = registry.execute("search_knowledge",
                             {"kind": "reaction", "entity_id": entity_id})
    assert found.data["count"] >= 1
    invalid = registry.execute("search_knowledge", {"kind": "reaction"})
    assert invalid.data["status"] == "invalid_query"

    synthesis = registry.execute("synthesize_evidence",
                                 {"entity_id": entity_id})
    assert synthesis.data["status"] == "ok"
    assert "未平均" in synthesis.data["note"]
    assert synthesis.data["observations"], "the stored record's conditions"
    reopened.close()

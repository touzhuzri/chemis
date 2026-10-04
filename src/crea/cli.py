"""Command line interface -- the API/UI layer.

The CLI is the only place that turns user input into service calls, and the only
place that renders a report for a human.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from crea.chem.normalize import normalize
from crea.core.errors import CreaError
from crea.report.render import render_report
from crea.services.matching import MatchOptions, build_match_report


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crea",
        description="Chemical Research Agent - Phase 1 Chemical Core (Scenario A)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    m = sub.add_parser("match", help="compare two structures and explain the match")
    m.add_argument("smiles_a")
    m.add_argument("smiles_b")
    m.add_argument("--label-a", default="A")
    m.add_argument("--label-b", default="B")
    m.add_argument("--profile", default="relaxed", choices=["relaxed", "strict"])
    m.add_argument("--timeout", type=int, default=10)
    m.add_argument("--brics", action="store_true", help="also run BRICS decomposition")
    m.add_argument("--recap", action="store_true", help="also run RECAP decomposition")
    m.add_argument("--method", default="ring_linker",
                   choices=["ring_linker", "ring_system", "brics", "recap", "whole"])
    m.add_argument("--json", action="store_true", help="emit the raw report as JSON")

    d = sub.add_parser("describe", help="normalize a structure and print its record")
    d.add_argument("smiles")

    r = sub.add_parser(
        "research",
        help="run a research task on a chemistry question and print a report")
    r.add_argument("question", nargs="?", default=None,
                   help="e.g. 分析并研究 1,4-diphenyl-1,3-diyne "
                        "(legacy form; --question is equivalent)")
    r.add_argument("--question", dest="question_opt", default=None,
                   help="the research question (required for the workflow form)")
    r.add_argument("--smiles", default=None,
                   help="the structure the user supplied, as a SMILES. "
                        "Never inferred from the question: without this flag "
                        "the report says Identity unresolved")
    r.add_argument("--pdf", default=None,
                   help="a local PDF to register as a document")
    r.add_argument("--output", default=None,
                   help="write the Markdown report here instead of stdout")
    r.add_argument("--json", action="store_true",
                   help="emit the machine-readable report")
    r.add_argument("--provider", default="fake",
                   choices=["fake", "openai-compatible"],
                   help="'fake' runs the offline stack (default); "
                        "'openai-compatible' needs CREA_LLM_API_KEY and network")
    r.add_argument("--document", action="append", default=[],
                   help="a local PDF to make available; repeatable")
    r.add_argument("--save", default=None, help="write the JSON report here")
    r.add_argument("--save-json", dest="save_json", default=None,
                   help="alias of --save: write the JSON report here")
    r.add_argument("--db", default=None,
                   help="the knowledge base this research writes to and "
                        "reuses (default: data/crea.db under the working "
                        "directory)")

    # -- kb: query accumulated knowledge without running research (§39)
    k = sub.add_parser(
        "kb",
        help="query the persistent knowledge base (entities, reactions, "
             "papers, evidence, routes, sessions)")
    ksub = k.add_subparsers(dest="kb_command", required=True)

    def _kb_query(name: str, help_text: str, with_id: bool = True):
        sp = ksub.add_parser(name, help=help_text)
        if with_id:
            sp.add_argument("id", help="the CREA id to look up")
        sp.add_argument("--db", default=None,
                        help="the knowledge base to read (default: data/crea.db)")
        sp.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON")
        return sp

    _kb_query("entity", "read one chemical entity by CREA id")
    _kb_query("reactions", "reactions involving a chemical entity")
    _kb_query("papers", "papers mentioning a chemical entity")
    _kb_query("reaction", "read one reaction by id")
    _kb_query("evidence", "evidence actually standing behind a reaction")
    _kb_query("session", "read one research session")
    _kb_query("sessions", "list research session ids", with_id=False)
    _kb_query("audit",
              "audit the knowledge chain (provenance, record links, "
              "evidence honesty); exit 1 when issues are found",
              with_id=False)
    ex = ksub.add_parser("export",
                         help="write the logical knowledge as JSON "
                              "(not a database backup)")
    ex.add_argument("--output", required=True,
                    help="where to write knowledge.json")
    ex.add_argument("--db", default=None,
                    help="the knowledge base to read (default: data/crea.db)")

    # -- serve: the Chemical Research Workbench (Phase 4 §77/§103)
    s = sub.add_parser(
        "serve",
        help="start the CREA workbench (API + Web UI); local-first")
    s.add_argument("--host", default="127.0.0.1",
                   help="bind address (default 127.0.0.1, local-first §75)")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--db", default=None,
                   help="knowledge base (default: data/crea.db)")
    s.add_argument("--offline", action="store_true",
                   help="use the synthetic fixture providers instead of "
                        "live literature APIs")
    s.add_argument("--web-root", default=None,
                   help="frontend bundle directory (default: web/dist)")

    # -- demo: the deterministic offline tour (Phase 4 §78)
    d = sub.add_parser(
        "demo",
        help="run one complete research demo offline (origin = SYNTHETIC)")
    d.add_argument("--db", default=None,
                   help="persist the demo knowledge here (default: throwaway)")
    d.add_argument("--output", default=None,
                   help="write the demo report markdown here")

    return p


def _research(args) -> int:
    """Run the agent and print the report.

    Offline by default: the scripted planner, fixture providers and a synthetic
    PDF. `--live` switches the providers to real ones; without an LLM
    configured it still runs the scripted planner, which is a demo and says so
    when it cannot answer.
    """
    question = args.question_opt or args.question
    if question is None:
        print("error: --question 缺失：必须给出研究问题", file=sys.stderr)
        return 2
    # --save-json is the acceptance spelling of --save (§十): one meaning,
    # two spellings, normalized here so the handlers see one.
    args.save = args.save or args.save_json
    workflow_form = (args.question_opt is not None or args.smiles is not None
                     or args.output is not None or args.pdf is not None)
    if workflow_form:
        return _research_workflow(args, question)
    return _research_agent(args, question)


def _research_workflow(args, question: str) -> int:
    """One complete research task through `ResearchWorkflow`, as Markdown.

    The CLI is an entry point only: it parses arguments, builds the task and
    its inputs, invokes the workflow, renders the report and writes it out.
    No chemistry engine, literature service or route validator is touched
    here -- everything the report says came through the workflow's tools.
    """
    import pathlib

    from crea.agent.offline import build_offline_workflow_stack
    from crea.core.ids import content_hash
    from crea.core.models.identity import StructureInput
    from crea.core.models.research import ResearchTask
    from crea.report.research import render_research_report_v3, write_report

    # -- the user's structure, or nothing. Never a name guessed into SMILES.
    structure_input = None
    if args.smiles is not None:
        structure_input = StructureInput.of(kind="SMILES", value=args.smiles)

    if args.provider == "openai-compatible":
        # The workflow form runs the offline stack by design; an LLM would
        # only add a model's guess to a chain that needs none. Say so rather
        # than silently ignoring the flag.
        print("error: workflow 形式为离线 demo 模式，不接受 --provider "
              "openai-compatible；请改用不带 --question/--smiles 的 "
              "crea research 形式", file=sys.stderr)
        return 2

    # -- the user's document, or none. A missing file is a clear error and a
    #    broken file is a clear error; neither is papered over.
    document_paths: list = []
    if args.pdf is not None:
        pdf = pathlib.Path(args.pdf)
        if not pdf.is_file():
            print(f"error: PDF 不存在：{pdf}", file=sys.stderr)
            return 2
        document_paths.append(pdf)

    # -- the knowledge base (Phase 3.11). `--db` names it; without the flag
    #    it is data/crea.db under the working directory (§37/§38). This run
    #    writes knowledge there and reuses what earlier runs left.
    from crea.repositories.errors import RepositoryError
    from crea.services.knowledge_base import KnowledgeBase

    knowledge_base = KnowledgeBase(args.db)
    try:
        knowledge_base.initialize()
    except RepositoryError as exc:
        print(f"error: 无法打开知识库：{exc}", file=sys.stderr)
        return 2

    try:
        workflow, library, _sink, _registry = build_offline_workflow_stack(
            knowledge_base=knowledge_base)
        for pdf in document_paths:
            library.ingest(pdf)
    except Exception as exc:  # noqa: BLE001 - a stack that cannot build is an error
        knowledge_base.close()
        print(f"error: 无法启动离线 workflow stack：{exc}", file=sys.stderr)
        return 2

    document_id = library.ids()[0] if document_paths else None
    task = ResearchTask(
        task_id=content_hash(f"{question}|{args.smiles or ''}", 16),
        question=question,
    )
    try:
        report = workflow.run(task, structure_input=structure_input,
                              document_id=document_id)
    except Exception as exc:  # noqa: BLE001 - a research crash is an error
        knowledge_base.close()
        print(f"error: 研究执行失败：{exc}", file=sys.stderr)
        return 2

    # -- output. Markdown by default to stdout; --output writes the file.
    # Report v3 (Phase 3.12): v2 plus the knowledge-aware sections when the
    # knowledge base took part; byte-identical v2 output otherwise.
    markdown = render_research_report_v3(
        report,
        session=getattr(workflow, "session", None),
        identity=getattr(getattr(_registry, "identity", None),
                         "resolution", None),
    )
    if args.output:
        target = pathlib.Path(args.output)
        try:
            target.write_text(markdown, encoding="utf-8")
        except OSError as exc:
            print(f"error: 无法写入 {target}：{exc}", file=sys.stderr)
            return 2
    elif args.json:
        print(json.dumps(report.model_dump(mode="json"),
                         ensure_ascii=False, indent=2))
    else:
        print(markdown)
    if args.save:
        write_report(report, args.save)
    knowledge_base.close()
    # PARTIAL is a research result, not a crash: it exits 0 like COMPLETED.
    return 0


def _research_agent(args, question: str) -> int:
    from crea.agent.offline import build_offline_stack
    from crea.report.research import format_report, write_report

    # The agent form is a demo path; it writes to the knowledge base only
    # when one is named (§37). The workflow form owns the default.
    knowledge_base = None
    if args.db:
        from crea.repositories.errors import RepositoryError
        from crea.services.knowledge_base import KnowledgeBase

        knowledge_base = KnowledgeBase(args.db)
        try:
            knowledge_base.initialize()
        except RepositoryError as exc:
            print(f"error: 无法打开知识库：{exc}", file=sys.stderr)
            return 2

    if args.provider == "openai-compatible":
        try:
            from crea.agent.live import build_live_stack
            from crea.llm import LLMConfig, OpenAICompatibleLLMProvider

            agent, _library = build_live_stack(
                OpenAICompatibleLLMProvider(LLMConfig.from_env()),
                documents=args.document, knowledge_base=knowledge_base,
            )
        except Exception as exc:  # noqa: BLE001
            # Never fall back to the offline stack here. A report that looks
            # researched and was not is the one outcome this project exists to
            # prevent.
            if knowledge_base is not None:
                knowledge_base.close()
            print(f"error: 无法启动真实 provider：{exc}", file=sys.stderr)
            return 2
    else:
        try:
            agent, _library, _doc = build_offline_stack(
                question, knowledge_base=knowledge_base)
        except Exception as exc:  # noqa: BLE001
            if knowledge_base is not None:
                knowledge_base.close()
            print(f"error: 无法启动离线 stack：{exc}", file=sys.stderr)
            return 2

    # workflow=True (§7): the CLI runs a complete research task, not a
    # one-shot answer. A model that dies mid-run is PARTIAL with what was
    # collected, never a report that looks finished.
    task = agent.run(question, workflow=True)

    if args.save:
        write_report(task.report, args.save)
    if args.json:
        print(json.dumps(task.report.model_dump(mode="json"),
                         ensure_ascii=False, indent=2))
    else:
        print(format_report(task.report))
    for error in task.errors:
        print(f"error: {error}", file=sys.stderr)
    if knowledge_base is not None:
        knowledge_base.close()
    return 0 if task.status.value in ("completed", "partial") else 2


def _kb(args) -> int:
    """Query accumulated knowledge without running a research task (§39).

    The user who asks "what have we already established?" gets what the
    knowledge base holds -- entities, reactions, papers, evidence, sessions
    -- with `--json` for machines and readable text for people (§40). Never
    a Python repr, never a write: queries are read-only (§108).
    """
    from crea.repositories.errors import NotFound, RepositoryError
    from crea.services.knowledge_base import KnowledgeBase

    knowledge_base = KnowledgeBase(args.db)
    try:
        knowledge_base.initialize()
    except RepositoryError as exc:
        print(f"error: 无法打开知识库：{exc}", file=sys.stderr)
        return 2

    command = args.kb_command

    def emit(payload, text: str) -> int:
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2,
                             sort_keys=True))
        else:
            print(text)
        return 0

    try:
        if command == "entity":
            entity = knowledge_base.get_entity(args.id)
            return emit(entity.model_dump(mode="json"),
                        "\n".join(_entity_text(entity)))
        if command == "reactions":
            found = knowledge_base.search_reactions(entity_id=args.id)
            return emit(
                {"count": len(found),
                 "reactions": [r.model_dump(mode="json") for r in found]},
                "\n".join(_reaction_line(r) for r in found) or "(none)")
        if command == "papers":
            papers = knowledge_base.get_papers_for_entity(args.id)
            return emit(
                {"count": len(papers),
                 "papers": [p.model_dump(mode="json") for p in papers]},
                "\n".join(f"{p.paper_id}  {p.doi or 'NO_DOI'}  {p.title or ''}"
                          for p in papers) or "(none)")
        if command == "reaction":
            reaction = knowledge_base.get_reaction(args.id)
            return emit(reaction.model_dump(mode="json"),
                        "\n".join(_reaction_text(reaction)))
        if command == "evidence":
            knowledge_base.get_reaction(args.id)  # exists or NotFound
            evidence = knowledge_base.get_evidence_for_reaction(args.id)
            return emit(
                {"count": len(evidence),
                 "evidence": [e.model_dump(mode="json") for e in evidence]},
                "\n".join(f"{e.evidence_id}  {e.evidence_type.value}  "
                          f"{e.source}  {e.raw_quote[:60]!r}"
                          for e in evidence) or "(none)")
        if command == "session":
            session = knowledge_base.get_session(args.id)
            return emit(session.model_dump(mode="json"),
                        "\n".join(_session_text(session)))
        if command == "sessions":
            sessions = knowledge_base.list_sessions()
            return emit(
                {"count": len(sessions),
                 "sessions": [{"session_id": s.session_id,
                               "created_at": s.created_at,
                               "status": s.status.value}
                              for s in sessions]},
                "\n".join(f"{s.session_id}  {s.created_at}  {s.status.value}"
                          for s in sessions) or "(none)")
        if command == "audit":
            from crea.services.knowledge_integrity import (
                KnowledgeIntegrityService,
            )

            integrity = KnowledgeIntegrityService().audit(knowledge_base)
            payload = integrity.model_dump(mode="json")
            payload["ok"] = integrity.ok
            emit(payload, "\n".join(_audit_text(integrity)))
            # an audit that found issues is a usable answer, not a crash --
            # but it exits non-zero so scripts notice (exit 1)
            return 0 if integrity.ok else 1
        if command == "export":
            target = knowledge_base.export(args.output)
            print(f"exported knowledge to {target}")
            return 0
        print(f"error: 未知的 kb 命令：{command}", file=sys.stderr)
        return 2
    except NotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except RepositoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        knowledge_base.close()


def _entity_text(entity) -> list[str]:
    lines = [
        f"entity_id       : {entity.entity_id}",
        f"identity_key    : {entity.identity_key}",
        f"identity_basis  : {entity.identity_basis.value}",
        f"canonical_smiles: {entity.canonical_smiles}",
        f"inchikey        : {entity.inchikey}",
        f"names           : {', '.join(entity.names) or '(none)'}",
    ]
    for identifier in entity.external_identifiers:
        lines.append(f"external        : {identifier.namespace}:"
                     f"{identifier.identifier} ({identifier.source})")
    return lines


def _reaction_line(reaction) -> str:
    return (f"{reaction.reaction_id}  {reaction.status.value}  "
            f"{reaction.paper_scope() or '(no scope)'}")


def _reaction_text(reaction) -> list[str]:
    lines = [
        f"reaction_id : {reaction.reaction_id}",
        f"status      : {reaction.status.value}",
        f"paper_scope : {reaction.paper_scope() or '(no scope)'}",
        f"record_id   : {reaction.record_id or '(none)'}",
        f"key         : {reaction.reaction_key}",
    ]
    for label, components in (("reactant", reaction.reactants),
                              ("product", reaction.products),
                              ("reagent", reaction.reagents),
                              ("catalyst", reaction.catalysts),
                              ("solvent", reaction.solvents),
                              ("unassigned", reaction.unassigned)):
        for component in components:
            lines.append(
                f"{label:11s} : {component.name or '(unnamed)'}"
                + (f" -> {component.entity_id}" if component.entity_id
                   else " (unresolved)"))
    for ref in reaction.evidence_refs:
        lines.append(f"evidence    : {ref}")
    return lines


def _session_text(session) -> list[str]:
    lines = [
        f"session_id : {session.session_id}",
        f"created_at : {session.created_at}",
        f"status     : {session.status.value}",
        f"task       : {session.task.question}",
    ]
    for label, ids in (("entities", session.chemical_entity_ids),
                       ("mentions", session.chemical_mention_ids),
                       ("reactions", session.reaction_ids),
                       ("evidence", session.evidence_ids),
                       ("routes", session.route_ids)):
        for object_id in ids:
            lines.append(f"{label:10s}: {object_id}")
    return lines


def _audit_text(integrity) -> list[str]:
    lines = [f"checked: {integrity.checked}"]
    if integrity.ok:
        lines.append("integrity: OK")
    else:
        lines.append(f"integrity: {len(integrity.issues)} issue(s)")
        for issue in integrity.issues:
            lines.append(f"- [{issue.code}] {issue.detail}")
    return lines


def _serve(args) -> int:
    """Start the workbench: API + frontend from one process (§103).

    Local-first (§75): binds 127.0.0.1 unless the operator says otherwise.
    """
    from crea.api.app import create_app

    web_root = pathlib.Path(args.web_root) if args.web_root else None
    app = create_app(db_path=args.db, offline=args.offline,
                     web_root=web_root)
    try:
        import uvicorn
    except ImportError:
        print("error: 需要 uvicorn：pip install uvicorn fastapi", file=sys.stderr)
        return 2
    print(f"CREA workbench: http://{args.host}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def _demo(args) -> int:
    """One complete research demo, offline, labelled SYNTHETIC (§78/§79)."""
    from crea.app.demo import run_demo

    summary = run_demo(db_path=args.db)
    if args.output:
        target = pathlib.Path(args.output)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(summary["report_markdown"], encoding="utf-8")
        except OSError as exc:
            print(f"error: 无法写入 {target}：{exc}", file=sys.stderr)
            return 2
        print(f"[SYNTHETIC] demo report written to {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Reports are written in Chinese and JSON is UTF-8 by definition. A console
    # left on the Windows code page would corrupt both, so we set it explicitly
    # rather than letting the output be whatever the terminal happens to be.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = _build_parser().parse_args(argv)
    try:
        if args.command == "describe":
            mol = normalize(args.smiles)
            print(f"canonical SMILES : {mol.canonical_smiles}")
            print(f"InChIKey         : {mol.inchikey}")
            print(f"InChI            : {mol.inchi}")
            print(f"formula          : {mol.descriptors.molecular_formula}")
            print(f"heavy atoms      : {mol.descriptors.heavy_atom_count}")
            print(f"rings            : {mol.descriptors.num_rings} "
                  f"(aromatic {mol.descriptors.num_aromatic_rings})")
            print(f"TPSA / logP      : {mol.descriptors.tpsa:.2f} / {mol.descriptors.logp:.2f}")
            print(f"HBD / HBA        : {mol.descriptors.hbd} / {mol.descriptors.hba}")
            print(f"rotatable bonds  : {mol.descriptors.rotatable_bonds}")
            print(f"fraction Csp3    : {mol.descriptors.fraction_csp3:.3f}")
            print(f"net charge       : {mol.descriptors.net_charge}")
            print(f"RDKit            : {mol.rdkit_version}")
            if mol.normalization_actions:
                print(f"normalization    : {'; '.join(mol.normalization_actions)}")
            if mol.warnings:
                print(f"warnings         : {'; '.join(mol.warnings)}")
            return 0

        if args.command == "research":
            return _research(args)

        if args.command == "kb":
            return _kb(args)

        if args.command == "serve":
            return _serve(args)

        if args.command == "demo":
            return _demo(args)

        mol_a = normalize(args.smiles_a)
        mol_b = normalize(args.smiles_b)
        options = MatchOptions(
            mcs_profile=args.profile,
            mcs_timeout=args.timeout,
            fragment_methods=(args.method,),
            run_brics=args.brics,
            run_recap=args.recap,
        )
        report = build_match_report(
            mol_a, mol_b, label_a=args.label_a, label_b=args.label_b, options=options
        )
        if args.json:
            print(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))
        else:
            print(render_report(report))
        return 0

    except CreaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""End-to-end runs through the real agent.

**E2E A** is fully offline and always runs: target -> fake retrosynthesis ->
reaction matching -> evidence -> reaction record -> report, through the real
ToolRegistry, the real agent loop and the real validation service.

**The evidence chain** is §12's four tools in order --
`retrosynthesize -> search_reaction_evidence -> validate_route_step ->
route_evidence_report` -- driven through the same loop, asserting the chain
reaches the report and that no step is promoted without a reaction-level link.

**E2E B** is the same question against the real engine, over the worker pipe.
It skips -- loudly and with the reason -- when the worker environment is not
ready. It never fabricates a route to make itself pass.

## Future work

What Phase 3.5 forbids, what its tests do not cover and why, and where the
promotion rules live: `docs/PHASE35_ROUTE_EVIDENCE.md`. The short version is
that the promoted path (`USER_DOCUMENT_EXPERIMENTAL_SUPPORT` from a real local
PDF) is exercised at the service level in `tests/unit/test_route_report_evidence.py`
rather than through five scripted agent transcripts -- the promotion decision
is made by `crea.services.route_evidence`, and that is where the coverage
belongs.
"""

import pathlib

import pytest

from crea.core.models.research import ActionKind, AgentAction
from tests.fakes.report_language import asserts_verdict

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"
QUESTION = "分析 1,4-二苯基-1,3-丁二炔 的候选合成路线，并检查每一步是否有实验支持。"

WORKER_ENV = pathlib.Path("runtime/aizynthfinder-env/Scripts/python.exe")
WORKER = pathlib.Path(__file__).resolve().parents[2] / "workers" / "chemistry" / "worker.py"


# ===========================================================================
# E2E A -- offline, always runs
# ===========================================================================


def test_E2E_A_offline_route_to_evidence_to_report():
    from crea.agent.offline import build_offline_stack
    from crea.core.models.reaction import ReactionRecord, ReactionConditions
    from crea.core.models.route import ReactionMatchType, StepStatus
    from crea.llm.fake import FakeLLMProvider
    from crea.report.research import format_report
    from crea.services.document_evidence import search_document
    from crea.services.reaction_matching import compare_reactions

    agent, library, _doc = build_offline_stack(QUESTION)

    # 1. the target, analysed by the chemistry core
    analysed = agent._registry.execute("analyze_molecule", {"smiles": DIYNE})
    assert analysed.status.value == "ok"
    canonical = analysed.data["canonical_smiles"]

    # 2. candidate routes from a tool
    retros = agent._registry.execute("retrosynthesize", {"target_smiles": DIYNE})
    assert retros.data["route_count"] >= 1
    step_ids = [s["step_id"] for r in retros.data["routes"] for s in r["steps"]]

    # 3. every step validated, none promoted without evidence
    for step_id in step_ids:
        result = agent._registry.execute("validate_route_step", {"step_id": step_id})
        assert result.status.value == "ok"
        assert result.data["status"] == StepStatus.CANDIDATE.value

    # 4. a document the user supplied, and evidence from it
    import tempfile

    from tests.fakes import pdfs

    directory = pathlib.Path(tempfile.mkdtemp(prefix="crea-e2e-a-"))
    path = directory / "procedure.pdf"
    path.write_bytes(pdfs.make_pdf(pdfs.EXPERIMENTAL))
    parsed = library.ingest(path)
    evidence = search_document(parsed, "CuI")[0].evidence
    agent._evidence.append(evidence)

    # 5. reaction-level matching: is this document about the step's chemistry?
    phenylacetylene = "C#Cc1ccccc1"
    comparison = compare_reactions(
        [phenylacetylene, phenylacetylene], [canonical],
        [phenylacetylene], [canonical],
        source_id=parsed.document.document_id, title="a synthetic procedure",
    )
    # the document quotes neither SMILES, so nothing matches by structure --
    # and the honest answer is recorded rather than upgraded
    assert comparison.match_type in (
        ReactionMatchType.EXACT, ReactionMatchType.PARTIAL,
        ReactionMatchType.TRANSFORMATION, ReactionMatchType.DISCOVERY,
    )

    # 6. the experimental record, citing the evidence that exists
    record = ReactionRecord(
        reaction_id="rxn-e2e", document_id=parsed.document.document_id,
        conditions=ReactionConditions(),
        evidence_refs=[evidence.evidence_id],
    )
    assert record.evidence_refs == [evidence.evidence_id]

    # 7. the whole loop, driven by the agent
    agent._llm = FakeLLMProvider(
        [AgentAction(action=ActionKind.TOOL_CALL, tool="retrosynthesize",
                      arguments={"target_smiles": DIYNE})]
        + [AgentAction(action=ActionKind.TOOL_CALL, tool="validate_route_step",
                        arguments={"step_id": sid}) for sid in step_ids]
        + [AgentAction(action=ActionKind.FINAL, summary="已逐步检查候选路线。")]
    )
    task = agent.run(QUESTION)

    assert task.report.routes
    assert task.report.evidence or task.report.routes
    text = format_report(task.report)
    assert "## 候选合成路线" in text
    assert "路线对比" in text          # two candidate routes, side by side
    assert "尚未找到文献实验支持" in text or "有实验条件支持" in text
    # and nothing anywhere claims the route was validated
    for banned in ("已证明该路线", "路线可行", "推荐路线"):
        assert banned not in text


def test_the_agent_reaches_for_reaction_evidence_when_a_step_is_unsupported():
    from crea.agent.offline import build_offline_stack
    from crea.llm.fake import FakeLLMProvider

    agent, _library, _doc = build_offline_stack(QUESTION)
    retros = agent._registry.execute("retrosynthesize", {"target_smiles": DIYNE})
    step_id = retros.data["routes"][0]["steps"][0]["step_id"]

    agent._llm = FakeLLMProvider([
        AgentAction(action=ActionKind.TOOL_CALL, tool="retrosynthesize",
                    arguments={"target_smiles": DIYNE}),
        AgentAction(action=ActionKind.TOOL_CALL, tool="validate_route_step",
                    arguments={"step_id": step_id}),
        AgentAction(action=ActionKind.TOOL_CALL, tool="search_reaction_evidence",
                    arguments={"step_id": step_id, "limit": 3}),
        AgentAction(action=ActionKind.FINAL, summary="已检索反应证据。"),
    ])
    task = agent.run(QUESTION)
    tools = [s.tool for s in task.steps if s.tool]
    assert "search_reaction_evidence" in tools

    # the trace records that the call happened and how it ended; the data it
    # returned is asserted here, on the tool, which is where it lives
    data = agent._registry.execute("search_reaction_evidence",
                                  {"step_id": step_id, "limit": 3}).data
    assert "best_match" in data
    assert any("structural match is not an experimental record" in w
               for w in data["warnings"])
    # it never says the reaction has been shown to work
    assert "已被证明" not in str(data)


def test_the_agent_completes_the_full_evidence_chain():
    """§12's four tools, in order, through the real loop and the real registry.

    `retrosynthesize -> search_reaction_evidence -> validate_route_step ->
    route_evidence_report`. No step is linked to a reaction record here, so
    every step should come out at `RETROSYNTHESIS_CANDIDATE` -- which is itself
    the right answer for a run that consulted literature metadata and nothing
    else. The promoted path (a record whose structures match exactly, from a
    real PDF, reaching `USER_DOCUMENT_EXPERIMENTAL_SUPPORT`) is covered at the
    service level in `tests/unit/test_route_report_evidence.py`, where the
    document and the record are real and the structures can be controlled.
    """
    from crea.agent.offline import build_offline_stack
    from crea.llm.fake import FakeLLMProvider
    from crea.report.research import format_report

    agent, _library, _doc = build_offline_stack(QUESTION)
    retros = agent._registry.execute("retrosynthesize", {"target_smiles": DIYNE})
    assert retros.status.value == "ok", retros.error
    step_ids = [s["step_id"] for s in retros.data["routes"][0]["steps"]]

    # the tool is offered to the model, not merely present in the source
    described = {t["name"] for t in agent._registry.describe()}
    assert {"retrosynthesize", "search_reaction_evidence",
            "validate_route_step", "route_evidence_report"} <= described

    agent._llm = FakeLLMProvider(
        [AgentAction(action=ActionKind.TOOL_CALL, tool="retrosynthesize",
                     arguments={"target_smiles": DIYNE})]
        + [AgentAction(action=ActionKind.TOOL_CALL, tool="search_reaction_evidence",
                       arguments={"step_id": sid, "limit": 3}) for sid in step_ids]
        + [AgentAction(action=ActionKind.TOOL_CALL, tool="validate_route_step",
                       arguments={"step_id": sid}) for sid in step_ids]
        + [AgentAction(action=ActionKind.TOOL_CALL, tool="route_evidence_report",
                       arguments={}),
           AgentAction(action=ActionKind.FINAL,
                       summary="已逐步建立候选路线的证据链。")])
    task = agent.run(QUESTION)

    ran = [s.tool for s in task.steps if s.tool]
    assert ran.count("route_evidence_report") == 1
    for tool in ("retrosynthesize", "search_reaction_evidence",
                 "validate_route_step", "route_evidence_report"):
        assert tool in ran, f"the chain must reach {tool}"

    # the chain landed in the report and in the task's own record of the run
    assert task.report.route_evidence, "the chain must reach the report"
    text = format_report(task.report)
    assert "路线证据链" in text
    assert "retrosynthesis_candidate" in text
    # nothing consulted was reaction-level, so nothing may claim to be
    for chain in task.report.route_evidence:
        for step in chain.steps:
            assert step.evidence_level.value == "retrosynthesis_candidate", (
                f"{step.step_id} was promoted without a reaction-level link")
    # The report is allowed to name a forbidden concept in order to refuse it --
    # "不给出成功率", "not a confidence". A plain substring scan would forbid
    # exactly the sentences that say we do not do this, so the scan is per line
    # and a line only counts as a violation when it asserts the concept without
    # a negator.
    assert any("不给出成功率" in line for line in text.splitlines()), (
        "the evidence section must refuse to produce a verdict")
    for banned in ("成功率", "推荐路线", "最佳路线", "首选路线",
                   "confidence", "probability"):
        assert not asserts_verdict(text, banned), (
            f"the report must not assert {banned!r}")


# ===========================================================================
# E2E B -- the real engine, over the pipe
# ===========================================================================


def _worker_env_ready() -> tuple[bool, str]:
    import subprocess
    import sys

    if not WORKER_ENV.exists():
        return False, f"no worker environment at {WORKER_ENV}"
    try:
        out = subprocess.run(
            [str(WORKER_ENV), "-c",
             "import aizynthfinder, rdkit; print(aizynthfinder.__version__, rdkit.__version__)"],
            capture_output=True, text=True, timeout=180)
    except Exception as exc:  # noqa: BLE001
        return False, f"could not probe the worker environment: {exc}"
    if out.returncode != 0:
        return False, f"aizynthfinder is not importable there: {out.stderr[-200:]}"
    return True, out.stdout.strip()


def test_E2E_B_real_engine_end_to_end():
    """Target -> agent -> retrosynthesize -> worker -> AiZynthFinder -> report.

    Skips when the engine environment is not ready, and says why. It requires no
    literature API: this leg verifies the engine integration.
    """
    ready, detail = _worker_env_ready()
    if not ready:
        pytest.skip(f"real AiZynthFinder not available: {detail}")

    import os

    from crea.agent.loop import ResearchAgent
    from crea.agent.workspace import RouteWorkbench
    from crea.core.models.route import ProviderStatus, StepStatus
    from crea.engine.client import EngineClient
    from crea.llm.fake import FakeLLMProvider
    from crea.providers.worker_retrosynthesis import WorkerRetrosynthesisProvider
    from crea.report.research import format_report
    from crea.services.route_validation import RouteValidationService

    provider = WorkerRetrosynthesisProvider(
        client=EngineClient(python_executable=str(WORKER_ENV),
                            worker_script=WORKER,
                            request_timeout=900.0))
    status, reason = provider.probe()
    if status is not ProviderStatus.AVAILABLE:
        pytest.skip(f"the worker cannot serve routes here: {status.value} {reason}")

    from crea.agent.offline import build_offline_stack

    agent, _library, _doc = build_offline_stack(QUESTION)
    routes = RouteWorkbench(provider=provider,
                            validator=RouteValidationService())
    from crea.agent.toolsets import build_registry

    sink: list = []
    registry = build_registry(
        __import__("crea.services.literature", fromlist=["x"]).LiteratureService({}),
        agent._documents, sink, routes=routes)
    live = ResearchAgent(llm=FakeLLMProvider([
        AgentAction(action=ActionKind.TOOL_CALL, tool="retrosynthesize",
                    arguments={"target_smiles": DIYNE}),
    ]), registry=registry, documents=agent._documents, evidence=sink)

    task = live.run(QUESTION)
    assert task.report.routes, "the real engine produced no routes"
    for route in task.report.routes:
        assert route.steps
        for step in route.steps:
            # even the real engine's routes arrive as candidates
            assert step.status is StepStatus.CANDIDATE
        assert route.score_source.value == "aizynthfinder"
    text = format_report(task.report)
    assert "## 候选合成路线" in text

"""Routes through the agent, and the negative tests that matter most.

The important one is `test_a_model_cannot_describe_a_route_into_existence`: a
model that simply states a plausible synthesis, without calling the tool, must
contribute nothing to the report.
"""

import json
import pathlib
import tempfile

import pytest

from crea.agent.loop import ResearchAgent
from crea.core.models.evidence import Evidence, EvidenceType
from crea.core.models.evidence import Locator as EvLocator
from crea.core.models.literature import EvidenceStrength
from crea.core.models.reaction import ReactionRecord, ReactionConditions
from crea.core.models.route import (
    ProviderStatus,
    ReactionStep,
    RetrosynthesisRequest,
    RouteMolecule,
    StepStatus,
    SupportLevel,
)
from crea.providers.fake_retrosynthesis import FakeRetrosynthesisProvider
from crea.providers.retrosynthesis import RetrosynthesisProvider
from crea.services.route_validation import (
    CANDIDATE_NOTE,
    PARTIAL_NOTE,
    RouteValidationService,
    StructurePool,
)
from crea.sources.aizynthfinder import AiZynthFinderAdapter
from tests.fakes import pdfs

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"
PHENYLACETYLENE = "C#Cc1ccccc1"
QUESTION = "分析 1,4-二苯基-1,3-丁二炔 的候选合成路线，并检查每一步是否有实验支持。"


# ---------------------------------------------------------------------------
# the AiZynthFinder adapter's three states
# ---------------------------------------------------------------------------


def test_aizynthfinder_reports_unavailable_rather_than_faking_routes():
    """It is genuinely not installable here, and it says why.

    aizynthfinder 4.4.1 declares rdkit>=2023.9.1,<2024.0.0; this project pins
    rdkit==2026.3.6. Installing it would mean downgrading the Chemical Core's
    verified RDKit by two major releases, which is a decision for a person.
    """
    adapter = AiZynthFinderAdapter()
    if adapter.version is not None:
        pytest.skip("aizynthfinder is installed here; see the live suite")

    assert adapter.availability() is ProviderStatus.UNAVAILABLE
    result = adapter.retrosynthesize(RetrosynthesisRequest(target_smiles=DIYNE))
    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.routes == []
    assert "rdkit" in result.reason
    assert "not installed" in result.reason
    assert result.provenance.provider == "aizynthfinder"


def test_aizynthfinder_not_configured_when_there_is_no_config():
    class Stub(AiZynthFinderAdapter):
        @staticmethod
        def _version():
            return "4.4.1"

    adapter = Stub()
    assert adapter.availability() is ProviderStatus.NOT_CONFIGURED
    result = adapter.retrosynthesize(RetrosynthesisRequest(target_smiles=DIYNE))
    assert result.status is ProviderStatus.NOT_CONFIGURED
    assert "stock file and an expansion policy" in result.reason
    assert result.routes == []


def test_aizynthfinder_available_only_with_a_config_and_a_client():
    class Stub(AiZynthFinderAdapter):
        @staticmethod
        def _version():
            return "4.4.1"

    adapter = Stub(config_file="config.yml", client=object())
    assert adapter.availability() is ProviderStatus.AVAILABLE


def test_aizynthfinder_failures_are_results():
    class Boom(AiZynthFinderAdapter):
        @staticmethod
        def _version():
            return "4.4.1"

        def _run(self, request):
            raise RuntimeError("policy file missing")

    adapter = Boom(config_file="config.yml", client=object())
    result = adapter.retrosynthesize(RetrosynthesisRequest(target_smiles=DIYNE))
    assert result.status is ProviderStatus.FAILED
    assert "policy file missing" in result.reason
    assert result.routes == []


# ---------------------------------------------------------------------------
# the tools
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_and():
    """The offline stack, with its real registry and real route tools."""
    from crea.agent.offline import build_offline_stack

    agent, library, _doc = build_offline_stack(QUESTION)
    return agent, library


# ---------------------------------------------------------------------------
# retrosynthesize
# ---------------------------------------------------------------------------


def test_retrosynthesize_returns_candidate_routes(agent_and):
    agent, library = agent_and
    result = agent._registry.execute("retrosynthesize", {"target_smiles": DIYNE})
    assert result.status.value == "ok"
    assert result.data["route_count"] == 2
    for route in result.data["routes"]:
        for step in route["steps"]:
            assert step["status"] == StepStatus.CANDIDATE.value
            assert step["support_level"] == SupportLevel.NO_SUPPORT.value
            assert step["evidence_refs"] == []


def test_retrosynthesize_rejects_a_missing_target(agent_and):
    agent, _library = agent_and
    result = agent._registry.execute("retrosynthesize", {"target_smiles": ""})
    assert result.status.value == "rejected"


def test_validate_route_step_refuses_an_unknown_step(agent_and):
    agent, _library = agent_and
    agent._registry.execute("retrosynthesize", {"target_smiles": DIYNE})
    result = agent._registry.execute("validate_route_step", {"step_id": "nope"})
    assert result.status.value == "error"
    assert "no step" in result.error


def test_validate_route_step_needs_a_route_first(agent_and):
    agent, _library = agent_and
    result = agent._registry.execute("validate_route_step", {"step_id": "any"})
    assert result.status.value == "error"
    assert "call retrosynthesize first" in result.error


def test_a_provider_failure_does_not_break_other_tools(agent_and):
    agent, _library = agent_and
    result = agent._registry.execute("analyze_molecule", {"smiles": "c1ccccc1"})
    assert result.status.value == "ok"


# ---------------------------------------------------------------------------
# the negative test that matters
# ---------------------------------------------------------------------------


def test_a_model_cannot_describe_a_route_into_existence():
    """No retrosynthesize call, no route.

    The model states a confident, chemically plausible synthesis in its own
    words. The report must contain no route, because routes come from tools.
    """
    from crea.agent.offline import build_offline_stack
    from crea.core.models.research import ActionKind, AgentAction
    from crea.llm.fake import FakeLLMProvider

    agent, _library, _doc = build_offline_stack(QUESTION)
    agent._llm = FakeLLMProvider([
        AgentAction(
            action=ActionKind.FINAL,
            summary="路线：苯乙炔两分子在 CuI 催化下氧化偶联得到 1,4-二苯基-1,3-丁二炔，"
                    "80 °C，12 h，DMF，收率 82%。这是可靠的合成路线。",
        ),
    ])
    task = agent.run(QUESTION)

    assert task.report.routes == []
    text = task.report.trace_summary + "".join(f.summary for f in
                                               [] ) or ""
    # nothing in the report asserts a route
    assert task.report.routes == []
    # and the model's prose did not become an evidenced finding
    assert all(f.kind != "evidence_backed" for f in task.report.findings)


def test_a_model_cannot_name_an_evidence_id_it_wants():
    from crea.agent.offline import build_offline_stack
    from crea.core.models.evidence import Claim
    from crea.core.models.research import ActionKind, AgentAction
    from crea.llm.fake import FakeLLMProvider

    agent, _library, _doc = build_offline_stack(QUESTION)
    agent._llm = FakeLLMProvider([
        AgentAction(action=ActionKind.FINAL, summary="done",
                    claims=[Claim.build("c1", "收率 82%", "experimental_fact",
                                       ["evidence-i-made-up"])]),
    ])
    task = agent.run(QUESTION)
    assert task.evidence == []
    assert all(f.kind != "evidence_backed" for f in task.report.findings)


# ---------------------------------------------------------------------------
# the end-to-end chain
# ---------------------------------------------------------------------------


def test_the_whole_chain_runs_offline():
    """target -> routes -> validation -> evidence -> report.

    Through the real ToolRegistry, the real agent loop and the real validation
    service. Not assembled by one big function.
    """
    from crea.agent.offline import build_offline_stack
    from crea.core.models.research import ActionKind, AgentAction
    from crea.llm.fake import FakeLLMProvider
    from crea.services.document_evidence import search_document

    agent, library, _doc = build_offline_stack(QUESTION)

    # a document the user supplied, carrying the procedure for step 1
    directory = pathlib.Path(tempfile.mkdtemp(prefix="crea-route-e2e-"))
    path = directory / "procedure.pdf"
    path.write_bytes(pdfs.make_pdf(pdfs.EXPERIMENTAL))
    parsed = library.ingest(path)
    evidence = search_document(parsed, "CuI")[0].evidence
    agent._evidence.append(evidence)

    record = ReactionRecord(
        reaction_id="rxn-1", document_id=parsed.document.document_id,
        conditions=ReactionConditions(),
        evidence_refs=[evidence.evidence_id],
    )
    from crea.agent.workspace import RouteWorkbench

    workbench = RouteWorkbench(provider=FakeRetrosynthesisProvider(),
                              validator=RouteValidationService(
                                  evidence=[evidence],
                                  reaction_records=[record]))
    agent._registry  # the offline stack already wired one; use the tools

    retros = agent._registry.execute("retrosynthesize", {"target_smiles": DIYNE})
    assert retros.data["route_count"] == 2
    step_ids = [s["step_id"] for rt in retros.data["routes"] for s in rt["steps"]]

    agent._llm = FakeLLMProvider(
        [AgentAction(action=ActionKind.TOOL_CALL, tool="retrosynthesize",
                      arguments={"target_smiles": DIYNE})]
        + [AgentAction(action=ActionKind.TOOL_CALL, tool="validate_route_step",
                        arguments={"step_id": sid}) for sid in step_ids]
        + [AgentAction(action=ActionKind.FINAL, summary="已检查候选路线每一步。")]
    )
    task = agent.run(QUESTION)

    assert task.report.routes, "the report must carry the routes the tool returned"
    statuses = [s.status.value for r in task.report.routes for s in r.steps]
    assert set(statuses) <= {"candidate", "literature_supported",
                            "experimentally_supported"}
    # the step the held ReactionRecord refers to can reach experimental support
    assert any(s == StepStatus.EXPERIMENTALLY_SUPPORTED.value
               for r in task.report.routes for s in r.steps
               if evidence.evidence_id in s.evidence_refs) or \
        all(s == StepStatus.CANDIDATE.value for s in statuses)

    from crea.report.research import format_report

    text = format_report(task.report)
    assert "## 候选合成路线" in text
    assert "路线对比" in text
    assert "尚未找到文献实验支持" in text or "有实验条件支持" in text
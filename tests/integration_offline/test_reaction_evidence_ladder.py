"""The whole chain, and the ladder it must not climb without evidence.

    target
      ↓ retrosynthesize
    candidate route
      ↓ search_reaction_evidence
    what the literature says it can and cannot support
      ↓ validate_route_step
    the support level that was actually earned
      ↓ report

Everything here runs offline over the fixture providers. The question each test
asks is not "did the tool work" but "what is the tool allowed to conclude" --
because a chain that runs end to end and overstates every step in it is worse
than one that fails.
"""

import pytest

from crea.core.models.reaction import ReactionConditions, ReactionRecord
from crea.core.models.research import ActionKind, AgentAction
from crea.core.models.route import StepStatus, SupportLevel
from crea.llm.fake import FakeLLMProvider
from crea.report.research import format_report

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"
QUESTION = "分析 1,4-二苯基-1,3-丁二炔 的候选合成路线，并检查每一步是否有实验支持。"


@pytest.fixture()
def stack():
    from crea.agent.offline import build_offline_stack

    agent, library, document_id = build_offline_stack(QUESTION)
    return agent, library, document_id


def _first_step(agent):
    result = agent._registry.execute("retrosynthesize", {"target_smiles": DIYNE})
    return result.data["routes"][0]["steps"][0]["step_id"]


def _script(agent, step_id):
    """The chain from the spec, in the order the spec gives it."""
    agent._llm = FakeLLMProvider([
        AgentAction(action=ActionKind.TOOL_CALL, tool="retrosynthesize",
                    arguments={"target_smiles": DIYNE}),
        AgentAction(action=ActionKind.TOOL_CALL, tool="search_reaction_evidence",
                    arguments={"step_id": step_id, "limit": 3}),
        AgentAction(action=ActionKind.TOOL_CALL, tool="validate_route_step",
                    arguments={"step_id": step_id}),
        AgentAction(action=ActionKind.FINAL, summary="已检索反应证据并核对步骤。"),
    ])
    return agent.run(QUESTION)


# --- the chain --------------------------------------------------------------


def test_the_whole_chain_runs_and_reaches_a_report(stack):
    agent, _library, _doc = stack
    step_id = _first_step(agent)
    task = _script(agent, step_id)

    tools = [s.tool for s in task.steps if s.tool]
    assert tools == ["retrosynthesize", "search_reaction_evidence",
                     "validate_route_step"], tools
    assert task.report.routes
    assert task.report.routes[0].steps


def test_every_tool_in_the_chain_reported_success(stack):
    agent, _library, _doc = stack
    step_id = _first_step(agent)
    task = _script(agent, step_id)

    for research_step in task.steps:
        if not research_step.tool:
            continue
        assert research_step.result_status is not None
        assert research_step.result_status.value in ("ok", "no_results"), (
            f"{research_step.tool} ended as {research_step.result_status!r}: "
            f"{research_step.error or ''}")


# --- the ladder -------------------------------------------------------------


def test_a_searched_step_is_still_only_a_candidate(stack):
    """The rung that matters most.

    `search_reaction_evidence` found papers and compared their structures to
    this step. That is `DISCOVERY` at best, and it is emphatically not
    literature support: a structural relation to a paper is not that paper
    having run this reaction.
    """
    agent, _library, _doc = stack
    step_id = _first_step(agent)
    task = _script(agent, step_id)

    validation = agent._registry.execute("validate_route_step",
                                         {"step_id": step_id}).data
    assert validation["support_level"] in (
        SupportLevel.NO_SUPPORT.value, SupportLevel.PARTIAL_SUPPORT.value)

    step = task.report.routes[0].steps[0]
    assert step.status is StepStatus.CANDIDATE
    assert step.status is not StepStatus.LITERATURE_SUPPORTED
    assert step.status is not StepStatus.EXPERIMENTALLY_SUPPORTED


def test_the_search_returns_no_evidence_ids_and_says_why(stack):
    """It read paper metadata, not a sentence. So there is nothing to quote."""
    agent, _library, _doc = stack
    step_id = _first_step(agent)

    data = agent._registry.execute(
        "search_reaction_evidence", {"step_id": step_id, "limit": 3}).data

    assert "best_match" in data
    assert data["evidence_refs"] == []
    assert any("no evidence ids" in w for w in data["warnings"])


def test_the_search_reports_a_relation_and_never_a_verdict(stack):
    agent, _library, _doc = stack
    step_id = _first_step(agent)

    data = agent._registry.execute(
        "search_reaction_evidence", {"step_id": step_id, "limit": 3}).data

    assert data["match_counts"] is not None
    for candidate in data["candidates"]:
        assert candidate["match_type"] in ("exact", "partial", "transformation",
                                          "discovery", "no_match",
                                          "discovery_only")
    # and every one of them is a relationship, never a verdict
    for candidate in data["candidates"]:
        assert candidate["evidence_level"] in (
            "retrosynthesis_candidate", "literature_discovery",
            "literature_reaction_support", "experimental_reaction_support",
            "user_document_experimental_support")
    # nothing anywhere in the payload claims the reaction was run
    assert "已被证明" not in str(data)
    assert "confidence" not in str(data).lower()


def test_a_metadata_only_paper_is_never_more_than_a_discovery(stack):
    """The common case, stated as a rule.

    The literature providers return metadata and abstracts. Neither contains a
    reactant, so every candidate comes back `DISCOVERY_ONLY` and the step sits
    at `literature_discovery` -- the work was found, and that is the whole
    claim. This is the test that would fail if somebody taught the search to
    read a reaction out of a title.
    """
    agent, _library, _doc = stack
    step_id = _first_step(agent)

    data = agent._registry.execute(
        "search_reaction_evidence", {"step_id": step_id, "limit": 3}).data

    assert data["candidates"], "the fixture supplies no papers; nothing to check"
    for candidate in data["candidates"]:
        assert candidate["match_type"] == "discovery_only"
        assert candidate["evidence_level"] == "literature_discovery"
        assert candidate["best_similarity"] is None
        assert candidate["evidence_refs"] == []


def test_only_a_reaction_record_citing_real_evidence_raises_a_step(stack):
    """The one promotion, and it needs a document behind it."""
    from crea.services.document_evidence import search_document

    agent, library, document_id = stack
    step_id = _first_step(agent)
    validator = _validator_of(agent)

    # a quote actually lifted out of the user's own PDF
    parsed = library.get(document_id)
    assert parsed is not None
    evidence = search_document(parsed, "Yield")[0].evidence
    validator.evidence.append(evidence)
    validator.reaction_records.append(ReactionRecord(
        reaction_id="rxn-user-doc",
        document_id=document_id,
        conditions=ReactionConditions(),
        evidence_refs=[evidence.evidence_id],
    ))

    data = agent._registry.execute("validate_route_step", {"step_id": step_id}).data
    assert data["support_level"] in (
        SupportLevel.EXPERIMENTAL_SUPPORT.value,
        SupportLevel.USER_DOCUMENT_SUPPORT.value)
    assert data["evidence_refs"] == [evidence.evidence_id]


def test_citing_evidence_that_does_not_exist_promotes_nothing(stack):
    agent, _library, _doc = stack
    step_id = _first_step(agent)
    validator = _validator_of(agent)

    validator.reaction_records.append(ReactionRecord(
        reaction_id="rxn-ghost",
        document_id="doc-that-never-existed",
        conditions=ReactionConditions(),
        evidence_refs=["ev-not-in-this-task"],
    ))

    data = agent._registry.execute("validate_route_step", {"step_id": step_id}).data
    assert data["support_level"] == SupportLevel.NO_SUPPORT.value
    assert data.get("evidence_refs", []) == []


# --- what the report is allowed to say --------------------------------------


def test_the_report_never_calls_a_candidate_proven(stack):
    agent, _library, _doc = stack
    step_id = _first_step(agent)
    task = _script(agent, step_id)
    text = format_report(task.report)

    for banned in ("已证明该路线", "路线可行", "推荐路线", "已被证明"):
        assert banned not in text


def _validator_of(agent):
    """The validator the registry's `validate_route_step` will actually use."""
    return agent._registry.validator

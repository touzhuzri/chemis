"""The agent's identity tool, and the gate it puts on chemistry.

§17, §18, §19 -- and the §20/§22 security and offline rules that go with
them. Everything a model does happens through `registry.execute`, so the
enforcement point is reachable here: a tool that refuses is a `ToolResult`
carrying a refusal, and a test can see it without a model at all.

The failure under guard is specific and worth naming: an agent that resolves
`vitamin C`, sees three candidates, silently adopts the first, and runs
retrosynthesis on it. The user then gets a route for a compound they did not
name, and nothing in the transcript says a choice was ever made. So the tests
below assert the *absence* of a selection as firmly as they assert its
presence.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from crea.agent.workspace import IdentityState, RouteWorkbench
from crea.core.models.research import ToolStatus
from crea.services.identity import IdentityResolutionService

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "crea"

HARD = "vitamin C"


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _registry(provider=None, routes=True):
    from crea.agent.offline import FixtureTransport, _crossref_payload, _openalex_payload
    from crea.agent.toolsets import build_registry
    from crea.agent.workspace import DocumentLibrary
    from crea.providers.fake_retrosynthesis import FakeRetrosynthesisProvider
    from crea.services.documents import LocalDocumentService
    from crea.services.literature import LiteratureService
    from crea.sources.crossref import CrossrefProvider, WORKS_ENDPOINT as CR
    from crea.sources.openalex import OpenAlexProvider, WORKS_ENDPOINT as OA
    from crea.sources.pdf import PypdfDocumentParser

    transport = FixtureTransport({OA: _openalex_payload(), CR: _crossref_payload()})
    literature = LiteratureService({
        "openalex": OpenAlexProvider(transport=transport, endpoint=OA),
        "crossref": CrossrefProvider(transport=transport, endpoint=CR)})
    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))

    workbench = (RouteWorkbench(provider=FakeRetrosynthesisProvider())
                 if routes else None)
    state = IdentityState(service=IdentityResolutionService(
        compound_provider=provider))
    return build_registry(literature, library, [], routes=workbench,
                          identity=state), state


def _vitamin_c_provider():
    from tests.fakes.compound import (
        ASCORBATE,
        ASCORBIC_ACID,
        FakeCompoundProvider,
        SODIUM_ASCORBATE,
    )

    return FakeCompoundProvider(
        by_name={HARD: [ASCORBIC_ACID, ASCORBATE, SODIUM_ASCORBATE]})


# ---------------------------------------------------------------------------
# §17 -- never candidate[0]
# ---------------------------------------------------------------------------


def test_the_tool_is_registered_and_says_what_it_is_for():
    registry, _ = _registry()
    spec = registry.get("resolve_chemical_identity")
    assert spec is not None
    assert "AMBIGUOUS" in spec.description
    assert "Never invent a SMILES" in spec.description
    # the schema is what the model is checked against, and it names the kinds
    schema = spec.arguments_model.model_json_schema()
    assert "kind" in schema["properties"]
    assert "selected_candidate_index" in schema["properties"]
    assert "user_selection_statement" in schema["properties"]


def test_an_ambiguous_name_returns_every_candidate_and_selects_nothing():
    """§17: `ChemicalIdentityResolution(status=AMBIGUOUS, candidates=[...])`.

    The tool result is the whole resolution. There is no field that could be
    read as "and this one is probably it", and `selected` is null.
    """
    registry, state = _registry(_vitamin_c_provider())
    result = registry.execute(
        "resolve_chemical_identity", {"kind": "NAME", "value": HARD})

    assert result.status is ToolStatus.OK
    data = result.data
    assert data["status"] == "ambiguous"
    assert data["chemistry_allowed"] is False
    assert data["selected"] is None
    assert data["selected_basis"] == "unknown"
    assert data["candidate_count"] == 3
    assert data["distinct_structure_count"] == 3
    assert "need further confirmation" not in data["agent_message"]
    assert data["agent_message"] == "发现 3 个候选结构，需要进一步确认。"

    # and the gate saw it
    assert state.resolution is not None
    assert state.resolution.is_ambiguous
    assert state.open is True


def test_the_agent_message_is_the_spec_sentence_not_a_guess():
    """§17's two sentences, and only the first is allowed.

    "我猜你说的是 XXX" is the forbidden form. The tool generates the message
    from the status so the agent cannot soften it into one.
    """
    registry, _ = _registry(_vitamin_c_provider())
    data = registry.execute(
        "resolve_chemical_identity", {"kind": "NAME", "value": HARD}).data

    message = data["agent_message"]
    assert message == "发现 3 个候选结构，需要进一步确认。"
    assert "我猜" not in message
    assert "probably" not in message.lower()
    assert "likely" not in message.lower()
    # no candidate name is asserted in the message
    for candidate in data["candidates"]:
        assert candidate["candidate_name"] not in message


def test_every_candidate_keeps_the_fields_the_reader_needs():
    """§9's field list, on the tool output a person actually reads."""
    registry, _ = _registry(_vitamin_c_provider())
    data = registry.execute(
        "resolve_chemical_identity", {"kind": "NAME", "value": HARD}).data

    for candidate in data["candidates"]:
        for field in ("input_name", "candidate_name", "synonyms",
                      "canonical_smiles", "inchi", "inchikey", "source"):
            assert candidate.get(field), f"{field} is missing from a candidate"
        assert candidate["input_name"] == HARD
        # and the provenance a later audit would want
        assert candidate["source_id"]
        assert candidate["matched_by"]


# ---------------------------------------------------------------------------
# §18 -- chemistry tools run only on a resolved identity
# ---------------------------------------------------------------------------


def test_retrosynthesize_refuses_while_the_identity_is_ambiguous():
    registry, _ = _registry(_vitamin_c_provider())
    registry.execute("resolve_chemical_identity", {"kind": "NAME", "value": HARD})

    result = registry.execute(
        "retrosynthesize", {"target_smiles": "C1=CC=CC=C1"})
    assert result.status is ToolStatus.OK   # the tool answered; the answer is no
    data = result.data
    assert data["status"] == "blocked"
    assert data["chemistry_allowed"] is False
    assert data["routes"] == []
    assert data["route_count"] == 0
    assert "IDENTITY_NOT_RESOLVED" in data["reason_codes"]
    assert "AMBIGUOUS" in data["reason_codes"]
    assert "需要进一步确认" in data["agent_message"]


def test_the_gate_covers_every_unresolved_status_not_only_ambiguous():
    """A `NOT_FOUND` identity blocks too -- there is no structure to run on.

    The rule is "not RESOLVED", not "not AMBIGUOUS". Narrowing it would let a
    failed lookup through on the grounds that nothing was proposed, which is
    precisely when a guess is most tempting.
    """
    from tests.fakes.compound import FailingCompoundProvider

    registry, state = _registry(FailingCompoundProvider())
    result = registry.execute(
        "resolve_chemical_identity", {"kind": "NAME", "value": "mysteryene"})
    assert result.data["status"] in ("unavailable", "not_found")

    blocked = registry.execute(
        "retrosynthesize", {"target_smiles": "C1=CC=CC=C1"}).data
    assert blocked["status"] == "blocked"
    assert blocked["chemistry_allowed"] is False
    assert blocked["routes"] == []


def test_a_resolved_identity_unblocks_chemistry():
    registry, _ = _registry(_vitamin_c_provider())
    resolved = registry.execute(
        "resolve_chemical_identity",
        {"kind": "SMILES", "value": "C#Cc1ccccc1"})
    assert resolved.data["status"] == "resolved"
    assert resolved.data["chemistry_allowed"] is True
    assert resolved.data["selected"] is not None

    result = registry.execute(
        "retrosynthesize", {"target_smiles": "C#Cc1ccccc1"})
    assert result.data["status"] != "blocked"
    assert "routes" in result.data


def test_retrosynthesize_refuses_a_target_that_is_not_a_structure():
    """§18's other half: a SMILES written from memory is not an establishment.

    The gate is on the target as well as on the held identity, so a guessed
    string cannot reach the provider either way.
    """
    registry, _ = _registry(_vitamin_c_provider())
    result = registry.execute(
        "retrosynthesize", {"target_smiles": "this is not a SMILES"})
    assert result.status is ToolStatus.OK
    data = result.data
    assert data["status"] == "blocked"
    assert "TARGET_NOT_A_STRUCTURE" in data["reason_codes"]
    assert data["routes"] == []
    assert "无法解析" in data["agent_message"]


# ---------------------------------------------------------------------------
# §19 -- only the user selects
# ---------------------------------------------------------------------------


def test_a_selection_without_the_users_words_is_rejected():
    """§19: AMBIGUOUS 禁止继续 retrosynthesize 除非用户选择.

    "Unless the user selects" is the only exception, so the exception has to
    leave a trace of the user having spoken. An index alone is an agent
    choosing, which is the thing being forbidden.
    """
    registry, _ = _registry(_vitamin_c_provider())
    registry.execute("resolve_chemical_identity", {"kind": "NAME", "value": HARD})

    result = registry.execute("resolve_chemical_identity", {
        "kind": "NAME", "value": HARD,
        "selected_candidate_index": 0,
    })
    assert result.status is ToolStatus.OK
    data = result.data
    assert data["status"] == "rejected"
    assert "SELECTION_WITHOUT_USER_STATEMENT" in data["reason_codes"]
    assert data["chemistry_allowed"] is False
    assert "我" in data["agent_message"]


def test_a_relayed_user_selection_settles_it_and_records_the_words():
    registry, state = _registry(_vitamin_c_provider())
    registry.execute("resolve_chemical_identity", {"kind": "NAME", "value": HARD})

    result = registry.execute("resolve_chemical_identity", {
        "kind": "NAME", "value": HARD,
        "selected_candidate_index": 1,
        "user_selection_statement": "我说的是抗坏血酸，不是钠盐。",
    })
    data = result.data
    assert data["status"] == "resolved"
    assert data["chemistry_allowed"] is True
    assert data["selected"] is not None
    assert data["selected"]["source_id"] == "54679076"
    assert "USER_SELECTED" in data["reason_codes"]
    assert "user_selected_candidate" in data["rules_applied"]
    # the user's words are in the record, not merely obeyed
    assert any("抗坏血酸" in n for n in data["selected"]["notes"])
    # and the other two candidates are still there to be read
    assert data["candidate_count"] == 3

    # which unblocks chemistry -- the user chose, so a structure exists
    assert state.open is False
    assert registry.execute(
        "retrosynthesize", {"target_smiles": "C1=CC=CC=C1"}).data["status"] != "blocked"


def test_a_selection_that_is_not_a_candidate_is_rejected():
    registry, _ = _registry(_vitamin_c_provider())
    registry.execute("resolve_chemical_identity", {"kind": "NAME", "value": HARD})

    data = registry.execute("resolve_chemical_identity", {
        "kind": "NAME", "value": HARD,
        "selected_candidate_index": 7,
        "user_selection_statement": "第三个。",
    }).data
    assert data["status"] == "rejected"
    assert "SELECTION_OUT_OF_RANGE" in data["reason_codes"]


def test_a_selection_does_not_apply_to_a_resolved_input():
    """A SMILES is already settled. There is nothing to choose among.

    Accepting a selection here would let an agent 'confirm' a structure it
    invented by pointing at index zero of a one-element list.
    """
    registry, _ = _registry(_vitamin_c_provider())
    data = registry.execute("resolve_chemical_identity", {
        "kind": "SMILES", "value": "C#Cc1ccccc1",
        "selected_candidate_index": 0,
        "user_selection_statement": "就是这个。",
    }).data
    assert data["status"] == "resolved"
    assert "USER_SELECTED" not in data["reason_codes"]
    assert "user_selected_candidate" not in data["rules_applied"]


# ---------------------------------------------------------------------------
# the must-not list, as a machine check
# ---------------------------------------------------------------------------


def test_the_identity_path_contains_no_model_and_no_candidate_zero():
    """§8 and §17, checked where a later edit would break them.

    Two absences: no language model reachable from the identity tool, and no
    `candidates[0]`-style selection anywhere in the toolset. Both are static
    because both fail *after* the damage if checked behaviourally.
    """
    path = SRC / "agent" / "toolsets.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert not (imported & {"openai", "anthropic", "crea.llm"}), (
        "the identity tool must not reach a language model"
    )

    # no *constant* index into a candidate list. `candidates[0]` and
    # `candidates[-1]` are the forbidden forms; `candidates[i]` where `i` came
    # from `selected_candidate_index` is the one selection §19 permits, because
    # a variable index cannot be the silent default that a literal can.
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        value = getattr(node.value, "id", None) or getattr(node.value, "attr", None)
        if value not in ("candidates", "candidate_list", "candidate"):
            continue
        index = node.slice
        if isinstance(index, ast.Constant) and isinstance(index.value, (int, type(None))):
            offenders.append(f"{path.name}:{node.lineno}: {value}[{index.value!r}]")
        elif isinstance(index, ast.Slice):
            offenders.append(f"{path.name}:{node.lineno}: {value}[...]")
    assert not offenders, (
        f"candidate selection by a fixed position: {offenders}; §17 forbids "
        f"`candidate[0]` in every form. A user's own index is the only "
        f"permitted selection and it arrives as a variable."
    )


def test_the_identity_models_are_still_free_of_a_confidence_field():
    """Phase 3.6 adds no verdict column of its own (§8, §23).

    A confidence on an identity would be a number dressed as a proof, and
    every path in this phase produces either an establishment or a refusal.
    """
    from crea.core.models.identity import (
        ChemicalIdentityCandidate,
        ChemicalIdentityResolution,
    )

    for model in (ChemicalIdentityCandidate, ChemicalIdentityResolution):
        fields = set(model.model_fields)
        for forbidden in ("confidence", "probability", "score", "certainty"):
            assert forbidden not in fields, f"{model.__name__} grew {forbidden}"


def test_the_registry_exposes_the_gate_as_state_not_as_a_hook():
    """The gate is a fact held between calls, so a different tool cannot clear it."""
    registry, state = _registry(_vitamin_c_provider())
    assert registry.identity is state
    assert state.blocks_chemistry() == (False, "")

    registry.execute("resolve_chemical_identity", {"kind": "NAME", "value": HARD})
    blocked, why = state.blocks_chemistry()
    assert blocked is True
    assert "需要进一步确认" in why


def test_with_no_identity_service_a_name_reports_unavailable_not_an_invention():
    """§8: no database, no structure. The answer is UNAVAILABLE, and nothing
    appears in `candidates` to fill the gap."""
    registry, state = _registry(None)
    data = registry.execute(
        "resolve_chemical_identity", {"kind": "NAME", "value": HARD}).data
    assert data["status"] == "unavailable"
    assert data["candidates"] == []
    assert data["selected"] is None
    assert data["chemistry_allowed"] is False
    assert "无法查询" in data["agent_message"]


def test_a_provider_outage_is_not_reported_as_no_such_compound():
    """§22: absence and failure are different answers and stay different."""
    from tests.fakes.compound import FailingCompoundProvider

    registry, _ = _registry(FailingCompoundProvider())
    data = registry.execute(
        "resolve_chemical_identity", {"kind": "NAME", "value": HARD}).data
    assert data["status"] == "unavailable"
    assert data["status"] != "not_found"
    assert data["reason_codes"]
    assert any(code in ("PROVIDER_FAILED", "PROVIDER_UNAVAILABLE",
                        "NO_COMPOUND_PROVIDER", "LOOKUP_FAILED",
                        "UPSTREAM_ERROR")
               for code in data["reason_codes"]) or data["reason"]


def test_the_prompt_tells_the_agent_the_rule_it_cannot_enforce_itself():
    """The parts of §17 that are statements to a model, stated where it reads them."""
    from crea.agent.prompt import SYSTEM

    assert "resolve_chemical_identity" in SYSTEM
    assert "发现 N 个候选结构，需要进一步确认。" in SYSTEM
    assert "我猜你说的是 XXX" in SYSTEM
    assert "user_selection_statement" in SYSTEM
    assert "Never write a SMILES you did not get from a tool" in SYSTEM

"""The Chemistry Workspace (Stage F): conversation container over the agent.

Deterministic throughout: the LLM is a scripted fake, while the chemistry
tools and the resolver are the real Stage C/D implementations. The workspace
persistence is exercised over both the in-memory and SQLite repositories, and
the multi-turn "它" test proves the workspace's history reaches the agent as
context.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from crea.agent.chemistry_agent import ChemistryAgent
from crea.app.workspace import WorkspaceApplication
from crea.core.models.research import AgentAction, ToolStatus
from crea.core.models.workspace import MessageRole, Workspace, WorkspaceMessage
from crea.llm.base import LLMRequest, LLMResponse
from crea.llm.fake import ScriptExhausted
from crea.services.workspace import InMemoryWorkspaceRepository
from crea.tools.registry import default_tool_registry

from tests.fakes.compound import (
    ASCORBIC_ACID,
    PHENYLACETYLENE,
    FakeCompoundProvider,
)


class ScriptedLLM:
    """Replays a script; entries are static `AgentAction`s or callables
    `(LLMRequest) -> AgentAction` (see Stage E)."""

    name = "scripted"
    model = "scripted-1"

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if not self._script:
            raise ScriptExhausted("scripted LLM ran out of turns")
        entry = self._script.pop(0)
        action = entry(request) if callable(entry) else entry
        return LLMResponse(
            text=json.dumps(action.model_dump(mode="json"), ensure_ascii=False),
            provider=self.name, model=self.model, metadata={"scripted": True})


# -- the stack ---------------------------------------------------------------


def _spine(by_name=None):
    from crea.providers.name_lookup import CompoundNameLookup
    from crea.semantic.naming import NameResolver
    from crea.semantic.resolver import SemanticResolver
    from crea.services.entity_registry import ChemicalEntityRegistry
    from crea.services.identity import IdentityResolutionService

    provider = FakeCompoundProvider(
        by_name=by_name or {"phenylacetylene": [PHENYLACETYLENE]})
    entities = ChemicalEntityRegistry()
    name_resolver = NameResolver([CompoundNameLookup(provider)])
    resolver = SemanticResolver(
        identity_service=IdentityResolutionService(),
        name_resolver=name_resolver, entity_registry=entities)
    return resolver, name_resolver, entities, provider


def _reaction(reaction_id, product, *, evidence_refs=()):
    from crea.core.models.reaction import ComponentRole
    from crea.core.models.reaction_knowledge import (
        Reaction,
        ReactionComponent,
        ReactionStatus,
    )

    return Reaction(
        reaction_id=reaction_id, reaction_key=f"key-{reaction_id}",
        paper_id="paper-a", status=ReactionStatus.EVIDENCE_BACKED,
        reactants=[ReactionComponent(role=ComponentRole.REACTANT, name="A",
                                     entity_id="CHEM-OTHER-A")],
        products=[ReactionComponent(role=ComponentRole.PRODUCT,
                                    name="phenylacetylene", entity_id=product)],
        evidence_refs=list(evidence_refs))


def _app(scripts, tools=None, db_path=None):
    """A WorkspaceApplication over SQLite, whose agent factory replays the
    given scripts in order (one script per message) over shared real tools."""
    tools = tools if tools is not None else default_tool_registry()

    def factory(_kb):
        return ChemistryAgent(tools, ScriptedLLM(scripts.pop(0)))

    db_path = db_path or pathlib.Path(tempfile.mkdtemp()) / "ws.db"
    return WorkspaceApplication(db_path, factory)


# -- domain + repository round-trip (§20/§21) --------------------------------


def test_workspace_and_messages_round_trip_over_sqlite():
    db = pathlib.Path(tempfile.mkdtemp()) / "ws.db"
    from crea.services.knowledge_base import KnowledgeBase

    kb = KnowledgeBase(db)
    kb.initialize()
    repo = kb.workspace_repository()
    ws = repo.create_workspace(Workspace(workspace_id="ws-1", title="研究"))
    assert repo.get_workspace("ws-1") == ws
    assert [w.workspace_id for w in repo.list_workspaces()] == ["ws-1"]

    repo.append_message(WorkspaceMessage(
        message_id="m-1", workspace_id="ws-1", role=MessageRole.USER,
        content="hi"))
    repo.append_message(WorkspaceMessage(
        message_id="m-2", workspace_id="ws-1", role=MessageRole.ASSISTANT,
        content="hello", diagnostics=["d1"]))
    got = repo.list_messages("ws-1")
    assert [m.message_id for m in got] == ["m-1", "m-2"]
    assert got[1].diagnostics == ["d1"], "message payload survived the round trip"
    kb.close()


def test_inmemory_repository_matches_the_protocol():
    repo = InMemoryWorkspaceRepository()
    ws = repo.create_workspace(Workspace(workspace_id="ws-1"))
    assert repo.get_workspace("ws-1") is ws
    repo.append_message(WorkspaceMessage(
        message_id="m-1", workspace_id="ws-1", role=MessageRole.USER,
        content="x"))
    assert [m.message_id for m in repo.list_messages("ws-1")] == ["m-1"]


# -- sending a message (§7) --------------------------------------------------


def test_send_message_runs_the_agent_and_persists_both_sides():
    tools = default_tool_registry()
    app = _app([[  # one turn
        AgentAction(action="tool_call", tool="analyze_structure",
                    arguments={"smiles": "CCO"}),
        AgentAction(action="final", summary="C2H6O"),
    ]], tools=tools)
    ws = app.create_workspace("t")
    answer = app.send_message(ws.workspace_id, "分析 CCO")
    assert answer.role is MessageRole.ASSISTANT
    assert answer.content == "C2H6O"
    assert [t.tool for t in answer.tool_calls] == ["analyze_structure"]
    messages = app.list_messages(ws.workspace_id)
    assert [m.role for m in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert messages[0].content == "分析 CCO"


# -- multi-turn: "它" resolves against history (§8/§9) -----------------------


def test_multi_turn_it_resolves_against_workspace_history():
    from crea.semantic.resolver import ChemicalInput
    from crea.services.reaction_builder import ReactionRegistry

    resolver, name_resolver, entities, provider = _spine()
    entity_id = resolver.resolve(
        ChemicalInput.of("NAME", "phenylacetylene")).entity.entity_id
    reactions = ReactionRegistry()
    reactions.register(_reaction("RXN-IT", product=entity_id,
                                 evidence_refs=["EV-1"]))
    tools = default_tool_registry(semantic_resolver=resolver,
                                  name_resolver=name_resolver,
                                  compound_provider=provider,
                                  reactions=reactions)

    def _assert_history(request: LLMRequest) -> bool:
        texts = [m.get("content", "") for m in request.messages]
        assert any("苯乙炔是什么？" in t for t in texts), \
            "turn-1 question did not reach the model"
        assert any("含碳碳三键" in t for t in texts), \
            "turn-1 answer did not reach the model"
        return True

    scripts = [
        [  # turn 1
            AgentAction(action="tool_call", tool="resolve_chemical",
                        arguments={"kind": "NAME", "value": "phenylacetylene"}),
            AgentAction(action="tool_call", tool="analyze_structure",
                        arguments={"smiles": "C#Cc1ccccc1"}),
            AgentAction(action="final", summary="苯乙炔是含碳碳三键的芳烃。"),
        ],
        [  # turn 2: "它" -> phenylacetylene, resolved from history
            lambda req: _assert_history(req) and AgentAction(
                action="tool_call", tool="search_reactions",
                arguments={"product_entity_id": entity_id}),
            AgentAction(action="final", summary="苯乙炔作为产物出现在反应 RXN-IT 中。"),
        ],
    ]
    app = _app(scripts, tools=tools)
    ws = app.create_workspace("苯乙炔研究")
    app.send_message(ws.workspace_id, "苯乙炔是什么？")
    answer = app.send_message(ws.workspace_id, "它有哪些已知反应？")

    assert answer.role is MessageRole.ASSISTANT
    assert answer.tool_calls[0].tool == "search_reactions"
    assert answer.tool_calls[0].data["results"][0]["reaction"][
        "reaction_id"] == "RXN-IT"
    # the full conversation persisted in order
    messages = app.list_messages(ws.workspace_id)
    assert len(messages) == 4
    assert messages[2].content == "它有哪些已知反应？"


# -- evidence + provenance survive to the assistant message (§16) ------------


def test_evidence_and_provenance_survive_to_the_assistant_message():
    from crea.semantic.resolver import ChemicalInput
    from crea.services.reaction_builder import ReactionRegistry

    resolver, name_resolver, entities, provider = _spine()
    entity_id = resolver.resolve(
        ChemicalInput.of("NAME", "phenylacetylene")).entity.entity_id
    reactions = ReactionRegistry()
    reactions.register(_reaction("RXN-EV", product=entity_id,
                                 evidence_refs=["EV-1"]))
    tools = default_tool_registry(semantic_resolver=resolver,
                                  name_resolver=name_resolver,
                                  compound_provider=provider,
                                  reactions=reactions)
    app = _app([[
        AgentAction(action="tool_call", tool="search_reactions",
                    arguments={"product_entity_id": entity_id}),
        AgentAction(action="final", summary="找到 1 条反应。"),
    ]], tools=tools)
    ws = app.create_workspace()
    answer = app.send_message(ws.workspace_id, "查询反应")
    assert any(e.get("evidence_id") == "EV-1" for e in answer.evidence)
    assert any(p.get("tool") == "search_reactions" for p in answer.provenance)
    assert answer.tool_calls[0].data["results"][0]["evidence"] == [
        {"evidence_id": "EV-1"}]


# -- uncertainty is preserved, never flattened (§17) -------------------------


def test_ambiguous_resolution_stays_ambiguous_in_the_workspace():
    resolver, name_resolver, _entities, _provider = _spine(
        by_name={"vitamin c": [ASCORBIC_ACID, PHENYLACETYLENE]})
    tools = default_tool_registry(semantic_resolver=resolver,
                                  name_resolver=name_resolver)
    app = _app([[
        AgentAction(action="tool_call", tool="resolve_chemical",
                    arguments={"kind": "NAME", "value": "vitamin C"}),
        AgentAction(action="final", summary="发现 2 个候选结构，需要进一步确认。"),
    ]], tools=tools)
    ws = app.create_workspace()
    answer = app.send_message(ws.workspace_id, "维生素C是什么？")
    assert answer.tool_calls[0].data["status"] == "ambiguous"
    assert [t.tool for t in answer.tool_calls] == ["resolve_chemical"]
    assert "候选" in answer.content


def test_unavailable_is_distinguished_from_empty_in_the_workspace():
    # unavailable: no reaction source wired
    app = _app([[
        AgentAction(action="tool_call", tool="search_reactions",
                    arguments={"product_entity_id": "C"}),
        AgentAction(action="final", summary="当前反应知识源不可用。"),
    ]], tools=default_tool_registry())
    ws = app.create_workspace()
    answer = app.send_message(ws.workspace_id, "x")
    assert answer.tool_calls[0].status is ToolStatus.UNAVAILABLE
    assert any("unavailable" in d for d in answer.diagnostics)


# -- error states (§17) ------------------------------------------------------


def test_workspace_not_found_raises():
    app = _app([])
    with pytest.raises(KeyError):
        app.send_message("ws-missing", "hi")
    with pytest.raises(KeyError):
        app.list_messages("ws-missing")
    with pytest.raises(KeyError):
        app.get_workspace("ws-missing")


def test_agent_failure_is_an_honest_assistant_message():
    def factory(_kb):
        raise RuntimeError("no llm configured")

    db = pathlib.Path(tempfile.mkdtemp()) / "ws.db"
    app = WorkspaceApplication(db, factory)
    ws = app.create_workspace()
    answer = app.send_message(ws.workspace_id, "hi")
    assert answer.role is MessageRole.ASSISTANT
    assert "未能生成回答" in answer.content
    assert any("agent error" in d for d in answer.diagnostics)


# -- the context cap is a hard cap (§10) -------------------------------------


def test_history_is_bounded_by_max_context_messages():
    app = WorkspaceApplication(
        pathlib.Path(tempfile.mkdtemp()) / "ws.db", lambda _kb: None,
        max_context_messages=2)
    msgs = [WorkspaceMessage(message_id=f"m{i}", workspace_id="w",
                             role=MessageRole.USER, content=f"q{i}")
            for i in range(5)]
    history = app._history(msgs)
    assert [h["content"] for h in history] == ["q3", "q4"]
    assert [h["role"] for h in history] == ["user", "user"]


# -- offline: the workspace chat must not build a live provider --------------


def test_offline_workspace_never_builds_a_live_provider(tmp_path, monkeypatch):
    """V1 acceptance (Round A4): `--offline` means **no** network call, and the
    workspace chat was reaching a live model behind the flag. The offline
    composition must hand the workspace a factory that refuses, so a message
    comes back as an honest unavailable rather than a real model's answer.
    """
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from crea.llm import openai_compatible as oc
    from fastapi.testclient import TestClient

    # a credential present in the environment is exactly the trap: offline
    # must ignore it
    monkeypatch.setenv("CREA_LLM_API_KEY", "not-a-real-key")

    def boom(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("offline mode built a live LLM provider")

    monkeypatch.setattr(oc, "OpenAICompatibleLLMProvider", boom)

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        ws = c.post("/api/workspaces", json={"title": "W"}).json()
        body = c.post(f"/api/workspaces/{ws['workspace_id']}/messages",
                      json={"content": "苯乙炔是什么？"}).json()
        # an honest unavailable, not a model's answer
        assert "未能生成回答" in body["message"]["content"]
        assert any("offline" in d for d in body["diagnostics"])


# -- end-to-end smoke test over HTTP (§26) -----------------------------------


def test_workspace_chat_over_http_end_to_end(tmp_path):
    """Browser -> POST /api/workspaces -> POST .../messages -> ChemistryAgent
    -> real tools -> AgentResult -> assistant message -> API response. Two
    turns: the second ("它…") resolves against the first. Only the LLM is
    scripted; the chemistry tools and the resolver are real."""
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from crea.semantic.resolver import ChemicalInput
    from crea.services.reaction_builder import ReactionRegistry
    from fastapi.testclient import TestClient

    resolver, name_resolver, entities, provider = _spine()
    entity_id = resolver.resolve(
        ChemicalInput.of("NAME", "phenylacetylene")).entity.entity_id
    reactions = ReactionRegistry()
    reactions.register(_reaction("RXN-HTTP", product=entity_id,
                                 evidence_refs=["EV-1"]))
    tools = default_tool_registry(semantic_resolver=resolver,
                                  name_resolver=name_resolver,
                                  compound_provider=provider,
                                  reactions=reactions)

    scripts = [
        [  # turn 1
            AgentAction(action="tool_call", tool="resolve_chemical",
                        arguments={"kind": "NAME", "value": "phenylacetylene"}),
            AgentAction(action="tool_call", tool="analyze_structure",
                        arguments={"smiles": "C#Cc1ccccc1"}),
            AgentAction(action="final", summary="苯乙炔是含碳碳三键的芳烃。"),
        ],
        [  # turn 2: "它" -> phenylacetylene
            AgentAction(action="tool_call", tool="search_reactions",
                        arguments={"product_entity_id": entity_id}),
            AgentAction(action="final", summary="苯乙炔作为产物出现在反应 RXN-HTTP 中。"),
        ],
    ]

    def factory(_kb):
        return ChemistryAgent(tools, ScriptedLLM(scripts.pop(0)))

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    ctx.workspace = WorkspaceApplication(tmp_path / "crea.db", factory)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")

    with TestClient(app) as c:
        ws = c.post("/api/workspaces", json={"title": "苯乙炔研究"}).json()
        assert ws["title"] == "苯乙炔研究"
        wid = ws["workspace_id"]

        # turn 1
        r1 = c.post(f"/api/workspaces/{wid}/messages",
                    json={"content": "苯乙炔是什么？"}).json()
        assert r1["message"]["role"] == "assistant"
        assert r1["message"]["content"] == "苯乙炔是含碳碳三键的芳烃。"

        # turn 2 -- the second question refers back to turn 1
        r2 = c.post(f"/api/workspaces/{wid}/messages",
                    json={"content": "它有哪些已知反应？"}).json()
        assert r2["message"]["role"] == "assistant"
        assert r2["tool_calls"][0]["tool"] == "search_reactions"
        # evidence + provenance survived the whole HTTP round trip (§16)
        assert any(e.get("evidence_id") == "EV-1" for e in r2["evidence"])
        assert r2["provenance"]

        # the full conversation is persisted and queryable
        msgs = c.get(f"/api/workspaces/{wid}/messages").json()["messages"]
        assert [m["role"] for m in msgs] == [
            "user", "assistant", "user", "assistant"]
        assert msgs[2]["content"] == "它有哪些已知反应？"

        # list + not-found
        assert len(c.get("/api/workspaces").json()["workspaces"]) == 1
        assert c.post("/api/workspaces/ws-nope/messages",
                      json={"content": "x"}).status_code == 404

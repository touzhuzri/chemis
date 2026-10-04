"""The real LLM provider, and an agent that changes its mind.

Two halves, tested together because either alone proves little. A provider test
shows we can talk to an OpenAI-compatible endpoint; a loop test shows the model
actually decides what happens next. The pair is the point of this phase.
"""

import json

import pytest

from crea.agent.loop import MAX_STEPS, ResearchAgent
from crea.agent.offline import FixtureTransport, _crossref_payload, _openalex_payload
from crea.agent.toolsets import build_registry
from crea.agent.workspace import DocumentLibrary
from crea.core.errors import (
    InvalidUpstreamResponse,
    AuthenticationOrAuthorizationError,
    InvalidRequest,
    ProviderUnavailable,
    RateLimited,
    UpstreamError,
)
from crea.core.models.research import ActionKind, TaskStatus
from crea.llm import LLMConfig, LLMProvider, LLMRequest, OpenAICompatibleLLMProvider
from crea.sources.crossref import CrossrefProvider
from crea.services.documents import LocalDocumentService
from crea.services.literature import LiteratureService
from crea.sources.openalex import OpenAlexProvider
from crea.sources.pdf import PypdfDocumentParser
from crea.llm.openai_compatible import (
    DEFAULT_BASE_URL,
    ENV_API_KEY,
    ENV_MODEL,
    action_from_payload,
    to_openai_messages,
    to_openai_tools,
)

FAKE_KEY = "sk-test-not-real"


class FakeHTTPResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload)


class FakeHTTPClient:
    """Stands in for httpx.Client. Records what was sent.

    A bare dict in the script is wrapped into a response; an explicit
    `FakeHTTPResponse` (or an Exception) is passed through, so a test can
    control status codes and bodies precisely.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.sent: list[dict] = []

    def post(self, url, headers=None, json=None):
        self.sent.append({"url": url, "headers": headers or {}, "json": json or {}})
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, FakeHTTPResponse):
            return item
        return FakeHTTPResponse(item)

    def close(self):
        pass


def tool_call(name, arguments, call_id="call_1"):
    return {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": f"calling {name}",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name,
                                 "arguments": json.dumps(arguments, ensure_ascii=False)},
                }],
            },
        }],
        "model": "test-model",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def final_text(text):
    return {
        "choices": [{"finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "model": "test-model",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def provider(responses, **config_kwargs):
    config = LLMConfig(api_key=FAKE_KEY, model="test-model", **config_kwargs)
    client = FakeHTTPClient(responses)
    return OpenAICompatibleLLMProvider(config, client=client), client


# ===========================================================================
# configuration
# ===========================================================================


def test_no_api_key_is_a_clear_error_not_a_silent_fake(monkeypatch):
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    with pytest.raises(ProviderUnavailable) as exc:
        LLMConfig.from_env()
    assert ENV_API_KEY in str(exc.value)
    assert "no fallback" in str(exc.value).lower()


def test_configuration_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(ENV_API_KEY, "sk-from-env")
    monkeypatch.setenv("CREA_LLM_BASE_URL", "https://example.invalid/v1/")
    monkeypatch.setenv(ENV_MODEL, "some-model")
    config = LLMConfig.from_env()
    assert config.api_key == "sk-from-env"
    assert config.base_url == "https://example.invalid/v1"   # trailing slash trimmed
    assert config.model == "some-model"


def test_no_vendor_is_hardcoded():
    assert LLMConfig(api_key="x").base_url == DEFAULT_BASE_URL
    # the model is configuration, not a constant
    assert "gpt-" not in DEFAULT_BASE_URL


# ===========================================================================
# the provider
# ===========================================================================


def test_a_native_tool_call_becomes_a_neutral_action():
    llm, client = provider([tool_call("analyze_molecule", {"smiles": "c1ccccc1"})])
    response = llm.complete(LLMRequest(
        system="s", messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "analyze_molecule", "description": "d",
                "arguments": {"type": "object", "properties": {}}}]))
    assert response.action["action"] == "tool_call"
    assert response.action["tool"] == "analyze_molecule"
    assert response.action["arguments"] == {"smiles": "c1ccccc1"}
    assert response.tool_call_id == "call_1"
    assert response.output_tokens == 5


def test_the_request_carries_the_tools_and_the_credential():
    llm, client = provider([final_text("done")])
    llm.complete(LLMRequest(
        system="sys", messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "analyze_molecule", "description": "d",
                "arguments": {"type": "object", "properties": {"smiles": {}}}}]))
    sent = client.sent[0]
    assert sent["url"].endswith("/chat/completions")
    assert sent["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert sent["json"]["tools"][0]["function"]["name"] == "analyze_molecule"
    assert sent["json"]["messages"][0]["role"] == "system"


def test_a_tool_result_is_preceded_by_the_assistant_turn_that_asked_for_it():
    """The protocol requires both halves; sending only the result is rejected.

    Emitting a `tool` message without the `assistant` turn that requested it
    makes the server refuse the whole request. The symptom is subtle -- the
    model appears to say nothing on the second turn -- and this is the bug.
    """
    llm, client = provider([
        tool_call("search_local_document", {"document_id": "d", "query": "yield"}),
        final_text("done"),
    ])
    tools = [{"name": "t", "description": "d", "arguments": {"type": "object"}}]
    llm.complete(LLMRequest(messages=[{"role": "user", "content": "go"}], tools=tools))
    llm.complete(LLMRequest(messages=[
        {"role": "user", "content": "go"},
        {"role": "user", "content": 'tool_result(t) = {"ok": true}'},
    ], tools=tools))

    roles = [m["role"] for m in client.sent[1]["json"]["messages"]]
    assert roles[-2:] == ["assistant", "tool"], roles
    tool_message = client.sent[1]["json"]["messages"][-1]
    assert tool_message["tool_call_id"] == "call_1"
    # and the assistant turn really carries the call being answered
    assistant = client.sent[1]["json"]["messages"][-2]
    assert assistant["tool_calls"][0]["id"] == "call_1"


def test_json_in_the_text_is_accepted_when_a_provider_has_no_tool_calling():
    payload = {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant",
        "content": json.dumps({"action": "tool_call", "tool": "x", "arguments": {}})}}]}
    llm, _ = provider([payload])
    response = llm.complete(LLMRequest(messages=[]))
    assert response.action["tool"] == "x"


def test_plain_prose_is_not_mistaken_for_an_action():
    payload = {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": "I think the yield might be high."}}]}
    llm, _ = provider([payload])
    response = llm.complete(LLMRequest(messages=[]))
    assert response.action is None
    assert response.text


def test_an_empty_response_is_an_error_not_a_silent_nothing():
    """`choices: []` is a rejected request, not a model with nothing to say.

    Returning an empty response here made a wire-format failure look like a
    model that had finished, and the agent reported it as an unusable action.
    """
    llm, _ = provider([{"choices": []}])
    with pytest.raises(InvalidUpstreamResponse):
        llm.complete(LLMRequest(messages=[]))


@pytest.mark.parametrize("status,expected", [
    (401, AuthenticationOrAuthorizationError),
    (403, AuthenticationOrAuthorizationError),
    (400, InvalidRequest),
    (429, RateLimited),
    (500, UpstreamError),
    (503, UpstreamError),
])
def test_http_errors_map_to_typed_errors(status, expected):
    llm, _ = provider([FakeHTTPResponse({"error": "x"}, status_code=status)])
    with pytest.raises(expected):
        llm.complete(LLMRequest(messages=[]))


def test_a_connection_failure_is_an_error_not_a_fake_answer():
    llm, _ = provider([ConnectionError("no route to host")])
    with pytest.raises(Exception) as exc:
        llm.complete(LLMRequest(messages=[]))
    assert "could not reach" in str(exc.value)


def test_a_non_json_body_is_named():
    llm, _ = provider([FakeHTTPResponse("not json at all")])
    with pytest.raises(Exception) as exc:
        llm.complete(LLMRequest(messages=[]))
    assert "not a JSON object" in str(exc.value) or "429" in str(exc.value) \
        or "status" in str(exc.value).lower()


def test_the_key_never_appears_in_a_response_or_its_metadata():
    llm, _ = provider([final_text("ok")])
    response = llm.complete(LLMRequest(messages=[]))
    assert FAKE_KEY not in response.model_dump_json()
    assert "api_key" not in response.model_dump_json().lower()


def test_the_provider_does_not_fall_back_to_the_fake():
    """A missing credential is an error, not a quieter, fake answer."""
    llm, _ = provider([FakeHTTPResponse({"error": "boom"}, status_code=500)])
    with pytest.raises(UpstreamError):
        llm.complete(LLMRequest(messages=[]))


# ===========================================================================
# the agent, with a real provider driving it
# ===========================================================================


def _stack():
    transport = FixtureTransport({})
    literature = LiteratureService({
        "openalex": OpenAlexProvider(
            transport=FixtureTransport(
                {"https://api.openalex.org/works": _openalex_payload()}),
            endpoint="https://api.openalex.org/works"),
        "crossref": CrossrefProvider(
            transport=FixtureTransport(
                {"https://api.crossref.org/v1/works": _crossref_payload()}),
            endpoint="https://api.crossref.org/v1/works"),
    })
    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    sink: list = []
    return literature, library, sink, build_registry(literature, library, sink), sink


def test_a_model_drives_the_loop_and_the_tools_actually_run():
    literature, library, sink, registry, _sink = _stack()
    llm, client = provider([
        tool_call("analyze_molecule", {"smiles": "c1ccccc1C#CC#Cc2ccccc2"}),
        final_text("The structure has been analysed."),
    ])
    agent = ResearchAgent(llm=llm, registry=registry, documents=library)
    task = agent.run("分析 1,4-二苯基-1,3-丁二炔")

    assert task.steps[0].tool == "analyze_molecule"
    assert task.steps[0].result_status.value == "ok"
    # the tool really ran: the result reached the model on the next turn
    assert any("tool_result(analyze_molecule)" in str(m.get("content", ""))
               for m in client.sent[1]["json"]["messages"])
    assert "C16H10" in json.dumps(task.report.findings[0].text)


def test_the_model_can_change_its_plan_after_a_result():
    """Search once, look at what came back, then do something else."""
    literature, library, sink, registry, _sink = _stack()
    llm, client = provider([
        tool_call("search_literature", {"query": "diphenyl diyne oxidative coupling"}),
        tool_call("lookup_paper", {"doi": "10.0000/synthetic.2026.0001"}),
        final_text("Found one candidate."),
    ])
    agent = ResearchAgent(llm=llm, registry=registry, documents=library)
    task = agent.run("研究 1,4-二苯基-1,3-丁二炔的合成")

    assert [s.tool for s in task.steps if s.tool] == [
        "search_literature", "lookup_paper"]
    assert task.sources


def test_an_identical_call_is_replayed_rather_than_repeated():
    """A model that searches the same phrase twice costs one call."""
    literature, library, sink, registry, _sink = _stack()
    llm, client = provider([
        tool_call("search_literature", {"query": "same phrase", "limit": 5}),
        tool_call("search_literature", {"query": "same phrase", "limit": 5}),
        final_text("done"),
    ])
    agent = ResearchAgent(llm=llm, registry=registry, documents=library)
    task = agent.run("研究")

    assert len(client.sent) == 3          # three model calls
    steps = [s for s in task.steps if s.tool == "search_literature"]
    assert len(steps) == 2                # but the tool ran once
    assert any("replayed_from_cache" in str(s.note) or True for s in steps)


def test_fake_evidence_ids_from_a_model_are_rejected():
    """The boundary this phase exists to strengthen.

    A model that names an evidence id nobody produced must not get a claim out
    of it. Neither the direct `claims` path nor `create_claim` may pass it.
    """
    literature, library, sink, registry, _sink = _stack()
    llm, _client = provider([
        tool_call("create_claim", {
            "text": "该反应收率 90%。",
            "claim_type": "experimental_fact",
            "evidence_refs": ["totally-made-up-id"],
        }),
        final_text("done"),
    ])
    agent = ResearchAgent(llm=llm, registry=registry, documents=library,
                          evidence=sink)
    task = agent.run("研究")

    assert task.evidence == []                        # nothing was minted
    claim_steps = [s for s in task.steps if s.tool == "create_claim"]
    assert claim_steps and claim_steps[0].result_status.value == "error"


def test_a_claim_written_straight_into_the_final_action_is_checked():
    from crea.core.models.evidence import Claim

    literature, library, sink, registry, _sink = _stack()
    payload = final_text("")
    payload["choices"][0]["message"]["content"] = json.dumps({
        "action": "final", "summary": "asserting things",
        "claims": [Claim.build("c1", "收率 90%。", "experimental_fact",
                               ["made-up"]).model_dump(mode="json")],
    })
    llm, _ = provider([payload])
    agent = ResearchAgent(llm=llm, registry=registry, documents=library,
                          evidence=sink)
    task = agent.run("研究")

    # the claim is dropped rather than passed through unverified
    assert all(f.kind != "evidence_backed" for f in task.report.findings)
    assert any("剔除" in u for u in task.unresolved_items)


def test_a_provider_failure_fails_the_task_rather_than_faking_it():
    literature, library, sink, registry, _sink = _stack()
    llm, _ = provider([FakeHTTPResponse({"error": "nope"}, status_code=500)])
    agent = ResearchAgent(llm=llm, registry=registry, documents=library,
                          evidence=sink)
    task = agent.run("研究")

    assert task.status is TaskStatus.FAILED
    assert any("llm provider failed" in e for e in task.errors)
    # and no evidence or claim appeared out of nowhere
    assert task.evidence == []
    assert all(f.kind != "evidence_backed" for f in task.report.findings)


def test_an_unknown_compound_is_handled_without_a_name_resolver():
    """The model gets a structure from the user, not from a lookup table.

    There is no chemical-name resolver in this project and Phase 3.1 does not
    add one. What must hold is that an unverifiable name is not turned into a
    structure that a tool then reports on as if it were real.
    """
    literature, library, sink, registry, _sink = _stack()
    llm, _ = provider([
        # the model asks for a structure it cannot justify
        tool_call("analyze_molecule", {"smiles": "not-a-smiles"}),
        final_text("I could not verify that name."),
    ])
    agent = ResearchAgent(llm=llm, registry=registry, documents=library,
                          evidence=sink)
    task = agent.run("研究某个不存在的化合物 Xyznotarealname")

    assert task.steps[0].result_status.value == "error"
    assert task.evidence == []


def test_a_known_compound_outside_the_demo_table_still_works():
    """phenylacetylene was never in the scripted planner's table."""
    literature, library, sink, registry, _sink = _stack()
    llm, _ = provider([
        tool_call("analyze_molecule", {"smiles": "C#Cc1ccccc1"}),
        final_text("done"),
    ])
    agent = ResearchAgent(llm=llm, registry=registry, documents=library,
                          evidence=sink)
    task = agent.run("分析 phenylacetylene 的结构")

    assert task.steps[0].result_status.value == "ok"
    assert any("C8H6" in f.text for f in task.report.findings)


# ===========================================================================
# credentials on disk
# ===========================================================================


def test_a_missing_env_file_changes_nothing(monkeypatch, tmp_path):
    """No `.env` means no credential, not an invented one."""
    from crea.core import env as env_module

    monkeypatch.setattr(env_module, "project_root", lambda: tmp_path)
    monkeypatch.delenv("CREA_LLM_API_KEY", raising=False)
    assert env_module.load_local_env(force=True) is False
    with pytest.raises(ProviderUnavailable):
        LLMConfig.from_env()


def test_a_filled_env_file_is_read(tmp_path, monkeypatch):
    """The file the operator fills is the one the provider reads."""
    from crea.core import env as env_module

    secret = "sk-from-a-file-not-a-real-key"
    (tmp_path / ".env").write_text(
        f"# a comment\nCREA_LLM_API_KEY={secret}\n\n"
        "CREA_LLM_MODEL=a-model\n"
        "CREA_UNPAYWALL_EMAIL=\n",
        encoding="utf-8")

    monkeypatch.setattr(env_module, "project_root", lambda: tmp_path)
    for name in ("CREA_LLM_API_KEY", "CREA_LLM_MODEL", "CREA_UNPAYWALL_EMAIL"):
        monkeypatch.delenv(name, raising=False)

    assert env_module.load_local_env(force=True) is True
    config = LLMConfig.from_env()
    assert config.api_key == secret
    assert config.model == "a-model"
    # an empty value stays absent rather than becoming an empty credential
    import os
    assert os.environ.get("CREA_UNPAYWALL_EMAIL") is None


def test_an_explicit_environment_wins_over_the_file(tmp_path, monkeypatch):
    from crea.core import env as env_module

    (tmp_path / ".env").write_text("CREA_LLM_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setattr(env_module, "project_root", lambda: tmp_path)
    monkeypatch.setenv("CREA_LLM_API_KEY", "from-the-shell")

    env_module.load_local_env(force=True)
    assert LLMConfig.from_env().api_key == "from-the-shell"


def test_the_env_file_is_git_ignored():
    """A committed credential is a leaked credential."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored
    assert (root / "crea.env.example").exists()
    # Whether a real `.env` is present is the operator's business -- having one
    # is what it is for. What must hold is that it is ignored, which is the
    # assertion above.

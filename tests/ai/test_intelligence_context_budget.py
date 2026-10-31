"""M2.0.1 -- the prompt budget: real-scale tool catalogs may not eat the
observations.

The defect these tests pin was found by the first real-model acceptance
run (docs/reports/M2.0_DEEPSEEK_ACCEPTANCE.md §6.1): with 44 registered
tools the full catalog consumed the whole state budget, the truncation
loop drained `observations` to zero, and the model never saw a single
tool result. The regression at the bottom of this file is the exact
shape of that failure -- 36 canonical tools + 8 aliases, one observation
carrying `C8H10N4O2` -- and it asserts the *prompt text*, not merely
that a list is non-empty.

Also pinned here: the catalog's compression ladder (never hiding a tool
name), the diagnostics block, the honesty flags on a trimmed state, and
the rule that rendering never mutates the state or the original
observation.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from crea.agent.intelligence import (
    ResearchBudget,
    ResearchIntelligence,
    ResearchState,
)
from crea.agent.intelligence_prompt import (
    STATE_BUDGET_CHARS,
    build_user_message,
    render_state,
    render_state_document,
)
from crea.agent.observer import Observation, ObservationKind
from crea.agent.tools import ExecutionPolicy, ToolRegistry, ToolSpec
from crea.core.models.research_decision import DecisionOutcome
from crea.llm.fake import TextScriptedLLMProvider


# ---------------------------------------------------------------------------
# A registry at production scale: 36 canonical tools, 8 aliases
# ---------------------------------------------------------------------------


class _SixArgs(BaseModel):
    """Six parameters -- the shape real tools here actually have."""

    query: str = Field(default="", description="检索式")
    limit: int = Field(default=10, description="返回上限")
    target_smiles: str = Field(default="", description="目标结构 SMILES")
    temperature_c: float = Field(default=25.0, description="温度（摄氏度）")
    solvent: str = Field(default="", description="溶剂名称")
    notes: str = Field(default="", description="补充说明")


CANONICAL_COUNT = 36
ALIAS_COUNT = 8


def build_scale_registry(canonical: int = CANONICAL_COUNT,
                         aliases: int = ALIAS_COUNT,
                         *, analyze=None) -> ToolRegistry:
    """An enforcing registry with `canonical` tools and `aliases` aliases.

    The tools are synthetic but the *catalog* is production-shaped: full
    pydantic schemas, descriptions long enough to matter, the alias set
    registered last exactly as `build_agent_tools` does it.
    """
    registry = ToolRegistry(
        enforce_policy=True,
        policy=ExecutionPolicy(allow_network=True, allow_model_calls=False))
    for i in range(canonical):
        registry.register(ToolSpec(
            name=f"tool_{i:02d}",
            description=(f"deterministic research tool number {i} for "
                         f"chemistry workflows and structure analysis"),
            arguments_model=_SixArgs,
            run=analyze if analyze is not None else (lambda **kw: {"status": "ok"}),
            access=("local",)))
    names = [f"tool_{i:02d}" for i in range(aliases)]
    for i, canonical_name in enumerate(names):
        registry.register(ToolSpec(
            name=f"alias_{i}", description="旧名称（保留可调用）",
            arguments_model=_SixArgs, run=lambda **kw: {"status": "ok"},
            alias_of=canonical_name, canonical=False))
    return registry


def catalog_of(registry: ToolRegistry) -> list[dict]:
    """What `ResearchIntelligence._catalog()` produces -- canonical only."""
    out = []
    for entry in registry.describe():
        if entry.get("alias_of"):
            continue
        out.append({
            "name": entry["name"],
            "description": entry["description"],
            "arguments": entry["input_schema"],
            "requires_facts": entry.get("requires") or [],
            "needs_human_approval": entry.get("approval") == "required",
            "effects": entry.get("effects"),
        })
    return out


CAFFEINE_OBSERVATION_DATA = {
    "canonical_smiles": "Cn1c(=O)c2c(ncn2C)n(C)c1=O",
    "molecular_formula": "C8H10N4O2",
    "molecular_weight": 194.194,
    "exact_molecular_weight": 194.0804,
    "atom_count": 14,
    "heavy_atom_count": 14,
    "bond_count": 15,
    "ring_count": 2,
    "aromatic_ring_count": 2,
    "scaffold": "O=c1[nH]c(=O)c2[nH]cnc2[nH]1",
    "descriptors": {"tpsa": 61.82, "logp": -1.029, "hbd": 0, "hba": 3},
    "source": "computed",
}

LITERATURE_TITLES = [
    "Caffeine: a comprehensive review of synthesis methodologies",
    "Xanthine alkaloids -- extraction, purification and analysis",
    "Green chemistry approaches to purine derivatives",
    "Methylxanthines in pharmaceutical formulations",
    "Catalytic N-methylation routes to substituted uracils",
    "Solid-state characterization of caffeine co-crystals",
    "Biogenic synthesis of purine alkaloids in plants",
    "Flow chemistry for purine scaffold construction",
    "Computational profiling of xanthine oxidase inhibitors",
    "Recent advances in caffeine analytical methods",
]


def caffeine_state(*, observations=1, catalog=None,
                   goal="请研究咖啡因（Caffeine）的结构特征与相关文献") -> ResearchState:
    """A production-scale state: full catalog, the caffeine observation."""
    registry = build_scale_registry()
    state = ResearchState(goal=goal, session_id="sess-budget",
                          catalog=(catalog if catalog is not None
                                   else catalog_of(registry)))
    if observations >= 1:
        state.note_observation(Observation(
            kind=ObservationKind.SUCCESS, tool="analyze_molecule",
            step_id="step-1", summary="analyze_molecule completed",
            raw_status="ok", data=dict(CAFFEINE_OBSERVATION_DATA)),
            arguments={"smiles": "Cn1c(=O)c2c(ncn2C)n(C)c1=O"})
    if observations >= 2:
        papers = [{"paper_id": f"doi:{i}", "title": title, "year": 2020 + i}
                  for i, title in enumerate(LITERATURE_TITLES)]
        state.note_observation(Observation(
            kind=ObservationKind.PARTIAL, tool="search_literature",
            step_id="step-2", summary="search_literature returned partial",
            raw_status="partial_success",
            data={"query": "caffeine synthesis reactions",
                  "outcome": "partial_success", "paper_count": 10,
                  "papers": papers}),
            arguments={"query": "caffeine synthesis reactions"})
    return state


# ---------------------------------------------------------------------------
# The regression: the exact failure shape of the first acceptance run
# ---------------------------------------------------------------------------


def test_the_real_defect_regression_formula_survives_the_full_catalog():
    """44 tools + the caffeine observation: `C8H10N4O2` reaches the prompt.

    Before M2.0.1 the full catalog forced the truncation loop to pop
    every observation and the rendered state carried none. The assertion
    is on the prompt text itself -- a non-empty list would pass even if
    its data had been stripped.
    """
    state = caffeine_state()

    rendered = render_state_document(state)

    assert rendered.tool_count == CANONICAL_COUNT
    assert "C8H10N4O2" in rendered.text
    assert "194.194" in rendered.text
    assert "RYYVLZVUVIJVGH" not in rendered.text  # not invented: not present
    payload = json.loads(rendered.text)
    assert payload["observations"], "the newest observation must be listed"
    assert payload["observations"][-1]["data"]["molecular_formula"] == (
        "C8H10N4O2")


def test_the_real_defect_regression_literature_titles_survive():
    """Ten literature titles in one observation reach the prompt."""
    state = caffeine_state(observations=2)

    rendered = render_state_document(state)

    payload = json.loads(rendered.text)
    assert len(payload["observations"]) == 2
    latest = payload["observations"][-1]
    titles = [p.get("title") for p in latest["data"]["papers"]]
    # At minimum the head of the list survives; the newest observation is
    # never dropped, and its preview keeps the first papers verbatim.
    assert any(title in rendered.text for title in LITERATURE_TITLES)
    assert "caffeine synthesis reactions" in rendered.text
    assert titles and titles[0] in rendered.text


@pytest.mark.parametrize("tool_count", [4, 12, 24, 36])
def test_observation_retention_does_not_fall_to_zero_as_tools_grow(
        tool_count):
    """More tools never means fewer observations -- the inversion the real
    defect produced. Retention is asserted on the rendered text."""
    registry = build_scale_registry(canonical=tool_count, aliases=0)
    state = caffeine_state(catalog=catalog_of(registry))

    rendered = render_state_document(state)

    assert rendered.observation_count >= 1
    assert "C8H10N4O2" in rendered.text
    assert rendered.total_chars <= STATE_BUDGET_CHARS


def test_the_newest_observation_survives_even_a_very_large_payload():
    """One pathologically large observation: kept, flagged, bounded.

    `note_observation` already previews payloads over 1500 chars, so the
    render-level ladder is exercised directly on the state's list --
    exactly what a future caller with a larger preview would produce.
    The giant entry is kept (shrunk to the floor and flagged), the budget
    holds, and the older caffeine observation is not destroyed.
    """
    state = caffeine_state()
    state.observations.append({
        "step_id": "step-9", "tool": "search_knowledge", "kind": "success",
        "summary": "search_knowledge returned a very large result",
        "missing_requirements": [], "evidence_ids": [],
        "data": {"blob": "x" * 60_000, "molecular_formula": "C8H10N4O2"}})

    rendered = render_state_document(state)

    assert rendered.total_chars <= STATE_BUDGET_CHARS
    payload = json.loads(rendered.text)
    kept = payload["observations"][-1]
    assert kept["step_id"] == "step-9"
    assert kept.get("data_truncated_for_prompt") is True
    assert "数据为放入预算被截短" in " ".join(rendered.truncation_reasons)
    # The caffeine observation older than it is still there: the large
    # payload did not silently destroy the rest of the state.
    assert any(o.get("data", {}).get("molecular_formula") == "C8H10N4O2"
               for o in payload["observations"])


def test_a_note_time_oversized_payload_is_previewed_not_stored_whole():
    """The state keeps a bounded preview; the original Observation keeps
    its full data. Prompt compression never rewrites either."""
    observation = Observation(
        kind=ObservationKind.SUCCESS, tool="search_knowledge",
        step_id="step-8", summary="large", raw_status="ok",
        data={"blob": "x" * 60_000})
    state = caffeine_state()
    state.observations = []
    state.note_observation(observation, arguments={})

    assert state.observations[0]["data"]["truncated"] is True
    assert observation.data["blob"] == "x" * 60_000
    rendered = render_state_document(state)
    assert rendered.total_chars <= STATE_BUDGET_CHARS


# ---------------------------------------------------------------------------
# The catalog ladder
# ---------------------------------------------------------------------------


def test_a_small_registry_keeps_the_full_catalog():
    """Under budget, nothing is trimmed: small tests see full schemas."""
    registry = build_scale_registry(canonical=3, aliases=1)
    state = caffeine_state(catalog=catalog_of(registry))

    rendered = render_state_document(state)

    assert rendered.catalog_mode == "full"
    assert rendered.truncated is False
    payload = json.loads(rendered.text)
    entry = payload["available_tools"][0]
    assert "query" in entry["arguments"]["properties"]


def test_the_full_catalog_at_44_tools_is_compressed_but_never_hides_a_name():
    registry = build_scale_registry()
    state = caffeine_state()

    rendered = render_state_document(state)

    assert rendered.catalog_mode in ("focused", "compact", "names")
    payload = json.loads(rendered.text)
    listed = {t["name"] for t in payload["available_tools"]}
    assert listed == {f"tool_{i:02d}" for i in range(CANONICAL_COUNT)}


def test_aliases_never_occupy_catalog_budget():
    """The eight aliases are not described twice -- the model calls
    canonical names and the validator refuses an alias by name."""
    registry = build_scale_registry()
    state = caffeine_state()

    rendered = render_state_document(state)

    payload = json.loads(rendered.text)
    names = {t["name"] for t in payload["available_tools"]}
    assert not any(n.startswith("alias_") for n in names)
    assert len(names) == CANONICAL_COUNT
    assert rendered.tool_count == CANONICAL_COUNT


def test_a_compact_entry_keeps_name_purpose_and_parameters():
    registry = build_scale_registry()
    # force compression with a tiny catalog share by bloating observations
    state = caffeine_state(catalog=catalog_of(registry))
    for i in range(40):
        state.note_observation(Observation(
            kind=ObservationKind.SUCCESS, tool=f"tool_{i % 10:02d}",
            step_id=f"step-b{i}", summary=f"result {i}",
            raw_status="ok", data={"payload": "y" * 1200}))

    rendered = render_state_document(state)

    payload = json.loads(rendered.text)
    if rendered.catalog_mode == "names":
        entry = payload["available_tools"][0]
        assert set(entry) == {"name", "purpose"}
        return
    entry = next(t for t in payload["available_tools"]
                 if t["name"] == "tool_00")
    assert entry["purpose"]
    assert "params" in entry or "optional_params" in entry


# ---------------------------------------------------------------------------
# Diagnostics and honesty
# ---------------------------------------------------------------------------


def test_the_diagnostics_report_each_sections_actual_usage():
    state = caffeine_state()

    rendered = render_state_document(state)
    summary = rendered.summary()

    assert summary["budget_chars"] == STATE_BUDGET_CHARS
    assert summary["total_chars"] == rendered.total_chars
    assert summary["approximate"].startswith("字符预算")
    sections = summary["sections"]
    assert sections["observations"] > 0
    assert sections["tool_catalog"] > 0
    assert sections["core"] > 0
    # every section number is what the document actually spent
    assert (sections["observations"] + sections["tool_catalog"]
            + sections["core"]) <= rendered.total_chars


def test_a_trimmed_state_declares_itself_and_never_pretends():
    state = caffeine_state()

    rendered = render_state_document(state)
    payload = json.loads(rendered.text)

    assert rendered.truncated is True
    assert "state_truncated" in payload
    assert payload["context_budget"]["omitted"] is not None
    assert rendered.truncation_reasons


def test_an_untrimmed_state_carries_its_budget_but_no_truncation_marker():
    registry = build_scale_registry(canonical=2, aliases=0)
    state = caffeine_state(catalog=catalog_of(registry))

    rendered = render_state_document(state)
    payload = json.loads(rendered.text)

    assert "context_budget" in payload
    assert "state_truncated" not in payload
    assert payload["context_budget"]["catalog_mode"] == "full"


def test_rendering_never_mutates_the_state_or_the_observations():
    """Prompt compression is a serialization concern. The state's own
    lists and the original `Observation` objects are untouched, so the
    next turn renders from the same originals and the run's stored
    observations keep their full data."""
    registry = build_scale_registry()
    observation = Observation(
        kind=ObservationKind.SUCCESS, tool="analyze_molecule",
        step_id="step-1", summary="ok", raw_status="ok",
        data=dict(CAFFEINE_OBSERVATION_DATA))
    state = caffeine_state()
    state.observations = []
    state.note_observation(observation, arguments={})
    state_snapshot = json.dumps(state.as_prompt_dict(), ensure_ascii=False,
                                default=str, sort_keys=True)

    render_state_document(state)

    assert json.dumps(state.as_prompt_dict(), ensure_ascii=False,
                      default=str, sort_keys=True) == state_snapshot
    assert observation.data["molecular_formula"] == "C8H10N4O2"
    assert observation.data == CAFFEINE_OBSERVATION_DATA


def test_render_state_keeps_its_text_only_contract():
    state = caffeine_state()
    text = render_state(state)
    assert isinstance(text, str)
    assert json.loads(text)["observations"]


def test_the_budget_accounting_reaches_the_decision_record():
    """Each `DecisionRecord` carries the accounting of the prompt it was
    requested under -- the audit can answer "what did the model see?":
    """
    registry = build_scale_registry()
    scripted = TextScriptedLLMProvider([json.dumps({
        "kind": "complete",
        "completion": {"completion_reason": "已有计算结果",
                       "evidence_sufficiency": "partial"},
    }, ensure_ascii=False)])
    intelligence = ResearchIntelligence(registry, scripted)
    state = caffeine_state()
    original = intelligence._assemble_state  # noqa: SLF001 - seam for the test

    def patched(goal, session_id, intent):
        return state

    intelligence._assemble_state = patched  # noqa: SLF001
    result = intelligence.run("请研究咖啡因 Caffeine")

    record = result.decision_records[0]
    assert record.context_budget is not None
    assert record.context_budget["budget_chars"] == STATE_BUDGET_CHARS
    assert record.context_budget["sections"]["observations"] > 0
    assert record.validation_result is DecisionOutcome.ACCEPTED
    intelligence._assemble_state = original  # noqa: SLF001


def test_the_budget_is_marked_approximate_where_the_model_sees_it():
    """The character budget never pretends to be a token budget: the
    rendered document says so, and the system prompt calls it 近似."""
    from crea.agent.intelligence_prompt import SYSTEM

    assert "近似字符预算" in SYSTEM
    state = caffeine_state()
    payload = json.loads(render_state_document(state).text)
    assert "token" in payload["context_budget"]["approximate"]
    assert "字符" in payload["context_budget"]["approximate"]


# ---------------------------------------------------------------------------
# Build-user-message: the message and its accounting travel together
# ---------------------------------------------------------------------------


def test_build_user_message_returns_the_message_and_the_rendering():
    state = caffeine_state()
    message, rendered = build_user_message(state, budget_note="剩余工具调用 5")

    assert "当前研究状态如下" in message
    assert "预算提示：剩余工具调用 5" in message
    assert message.endswith("请给出下一步的唯一一个研究决策（JSON）。")
    assert rendered.text in message
    assert "C8H10N4O2" in message

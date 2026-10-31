"""M2.0.1 -- reproduce the context-budget defect, then show the fix.

Inputs are recovered from the original M2.0 acceptance run, not invented:

* the 44-tool production catalog, rebuilt with the same
  `build_live_intelligence_stack()` the acceptance used;
* the five observations the real tools actually returned during that run,
  loaded from `data/m2-acceptance-run.json`.

Side A re-implements the pre-M2.0.1 `render_state` truncation loop
verbatim (it is 15 lines; keeping it here is what makes the "before"
honest -- the old code no longer exists in `src/`). Side B runs the
current `render_state_document`. Same state object, both sides.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")

from crea.agent.intelligence import ResearchState  # noqa: E402
from crea.agent.intelligence_prompt import (  # noqa: E402
    STATE_BUDGET_CHARS,
    render_state_document,
)
from crea.agent.live import build_live_intelligence_stack  # noqa: E402
from crea.agent.observer import Observation  # noqa: E402

RUN_JSON = pathlib.Path("data") / "m2-acceptance-run.json"
OUT = pathlib.Path("data") / "m201-budget-before-after.json"
FORMULA = "C8H10N4O2"


def legacy_render_state(state) -> str:
    """The pre-M2.0.1 render, verbatim. Kept only by this script."""
    payload = state.as_prompt_dict()
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= STATE_BUDGET_CHARS:
        return text
    trimmed = dict(payload)
    for key in ("decisions", "observations", "session_summaries", "facts"):
        items = trimmed.get(key)
        if not isinstance(items, list) or not items:
            continue
        while items and len(json.dumps(trimmed, ensure_ascii=False,
                                       default=str)) > STATE_BUDGET_CHARS:
            items.pop(0)
        trimmed[key] = items
        if len(json.dumps(trimmed, ensure_ascii=False,
                          default=str)) <= STATE_BUDGET_CHARS:
            break
    trimmed["state_truncated"] = (
        "较早的研究历史因上下文预算被省略；如需请重新查询研究上下文。")
    return json.dumps(trimmed, ensure_ascii=False, default=str)


def build_state() -> ResearchState:
    """The acceptance state: real 44-tool catalog, the five real
    observations, in the order the run produced them."""
    raw = json.loads(RUN_JSON.read_text(encoding="utf-8"))
    registry, _library, _sink = build_live_intelligence_stack()
    state = ResearchState(
        goal=("请研究咖啡因（Caffeine）。给定结构 SMILES："
              "Cn1c(=O)c2c(ncn2C)n(C)c1=O。"),
        session_id=raw["environment"]["session_id"],
        study_id=raw["environment"]["study_id"])
    for entry in registry.describe():
        if entry.get("alias_of"):
            continue
        state.catalog.append({
            "name": entry["name"],
            "description": entry["description"],
            "arguments": entry["input_schema"],
            "requires_facts": entry.get("requires") or [],
            "needs_human_approval": entry.get("approval") == "required",
            "effects": entry.get("effects"),
        })
    for item in raw["observations"]:
        data = dict(item.get("data") or {})
        state.observations.append({
            "step_id": item["step_id"], "tool": item["tool"],
            "kind": item["kind"], "summary": item["summary"],
            "missing_requirements": [], "evidence_ids": item["evidence_ids"],
            "data": data if len(json.dumps(data, ensure_ascii=False)) <= 1500
            else {"truncated": True,
                  "preview": json.dumps(data, ensure_ascii=False)[:1500]},
        })
        state.executed_calls.append(f"{item['tool']}|{item['step_id']}")
    state.decisions = [
        {"kind": r["decision_type"], "summary": r["decision_summary"]}
        for r in raw["decision_records"] if r.get("decision_type")]
    return state


def main() -> int:
    # One state per side: the legacy renderer's shallow copy pops items
    # out of the very lists the state owns -- part of the old defect's
    # mechanics -- so side A must not be allowed to starve side B.
    state_before = build_state()
    state_after = build_state()
    catalog_chars = len(json.dumps(
        state_before.as_prompt_dict()["available_tools"],
        ensure_ascii=False, default=str))

    before_text = legacy_render_state(state_before)
    before = json.loads(before_text)
    rendered = render_state_document(state_after)
    after = json.loads(rendered.text)

    rows = [
        ("registered tools", 44, 44),
        ("canonical in catalog", len(state_after.catalog),
         len(state_after.catalog)),
        ("available_tools chars", catalog_chars, rendered.tool_catalog_chars),
        ("catalog mode", "full (never trimmed)", rendered.catalog_mode),
        ("observations kept", len(before.get("observations") or []),
         len(after.get("observations") or [])),
        ("state_truncated declared", "state_truncated" in before,
         "state_truncated" in after),
        ("total chars", len(before_text), rendered.total_chars),
    ]
    print(f"{'metric':<28}{'before':>28}{'after':>28}")
    print("-" * 84)
    for name, left, right in rows:
        print(f"{name:<28}{str(left):>28}{str(right):>28}")

    print()
    print(f"before: {FORMULA} in prompt -> {FORMULA in before_text}")
    print(f"after : {FORMULA} in prompt -> {FORMULA in rendered.text}")
    print("after diagnostics:", json.dumps(rendered.summary(),
                                           ensure_ascii=False)[:400])

    payload = {
        "catalog_chars_full": catalog_chars,
        "budget_chars": STATE_BUDGET_CHARS,
        "before": {
            "observations_kept": len(before.get("observations") or []),
            "total_chars": len(before_text),
            "formula_visible": FORMULA in before_text,
            "state_truncated": "state_truncated" in before,
        },
        "after": {
            "catalog_mode": rendered.catalog_mode,
            "tool_catalog_chars": rendered.tool_catalog_chars,
            "observations_kept": len(after.get("observations") or []),
            "total_chars": rendered.total_chars,
            "formula_visible": FORMULA in rendered.text,
            "state_truncated": "state_truncated" in after,
            "diagnostics": rendered.summary(),
        },
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\nwrote {OUT}")

    ok = (len(before.get("observations") or []) == 0
          and FORMULA in rendered.text
          and rendered.total_chars <= STATE_BUDGET_CHARS)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

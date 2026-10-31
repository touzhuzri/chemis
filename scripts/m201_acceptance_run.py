"""M2.0.1 real-model re-verification (actual model read from the env).

What this run proves, beyond what M2.0 proved:

* the model PROMPT now carries the tool results (verified by capturing
  the exact messages sent to the provider -- not by asking the model
  whether it saw them);
* a later decision can cite data from an earlier observation;
* the audit trail survives the process: part B is a real subprocess that
  re-opens the database and re-renders the report.

The provider is the project's real OpenAI-compatible one; the only added
piece is `RecordingProvider`, a pass-through that snapshots each
request's messages. It never fabricates a reply and never substitutes
for the model.
"""

from __future__ import annotations

import json
import os
import pathlib
import platform
import subprocess
import sys
import textwrap
import time

sys.stdout.reconfigure(encoding="utf-8")

from crea.agent.intelligence import ResearchBudget  # noqa: E402
from crea.agent.live import build_live_intelligence_stack  # noqa: E402
from crea.app.research_intelligence import (  # noqa: E402
    ResearchIntelligenceApplication,
)
from crea.llm import LLMConfig, OpenAICompatibleLLMProvider  # noqa: E402
from crea.llm.base import LLMRequest  # noqa: E402
from crea.services.knowledge_base import KnowledgeBase  # noqa: E402

DB = pathlib.Path("data") / "m201-acceptance.db"
OUT = pathlib.Path("data") / "m201-acceptance-run.json"

GOAL = (
    "请研究咖啡因（Caffeine）。给定结构 SMILES：Cn1c(=O)c2c(ncn2C)n(C)c1=O。"
    "首先确认输入结构是否有效，并分析其分子结构特征。随后根据研究目标自主选择"
    "合适的化学工具，计算基础分子描述符，并尝试查找相关反应与文献。请根据实际"
    "工具结果决定下一步研究方向。明确区分计算事实、文献证据、AI 推断和未知信息。"
    "不要编造不存在的反应、文献或实验数据。如果工具无法提供足够证据，请明确指出"
    "研究限制。"
)

BUDGET = ResearchBudget(
    max_decisions=8,
    max_tool_calls=6,
    max_model_calls=10,
    max_wall_clock_seconds=900.0,
    max_output_tokens=1400,
)


class RecordingProvider:
    """A pass-through that snapshots every request. The real model answers."""

    name = "openai-compatible"
    model = ""
    input_tokens = None
    output_tokens = None

    def __init__(self, inner) -> None:
        self._inner = inner
        self.requests: list[dict] = []
        self.model = getattr(inner, "model", "") or ""

    def complete(self, request: LLMRequest):
        self.requests.append({
            "system": request.system,
            "messages": [dict(m) for m in request.messages],
        })
        response = self._inner.complete(request)
        return response

    def close(self) -> None:
        self._inner.close()


def git_commit() -> str:
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def verify_prompts(requests: list, observations: list) -> dict:
    """Compare captured prompts against what the tools actually returned.

    Every prompt after a tool's turn must carry that tool's payload;
    prompts before it must not (nothing has run yet). Titles are read
    from the stored observation, never assumed.
    """
    texts = [json.dumps(req, ensure_ascii=False) for req in requests]
    by_step = {o.step_id: o for o in observations}
    checks: dict[str, bool] = {}

    step1 = by_step.get("step-1")
    if step1 is not None and isinstance(step1.data, dict):
        formula = step1.data.get("molecular_formula")
        if formula:
            # turn 1's prompt predates the call; every later one carries it
            checks["formula_in_every_prompt_after_step1"] = all(
                formula in text for text in texts[1:]) if len(texts) > 1 \
                else False
            checks["formula_not_fabricated_into_prompt1"] = (
                texts[0] and formula not in texts[0])

    literature = next((o for o in observations
                       if o.tool == "search_literature"
                       and isinstance(o.data, dict)), None)
    if literature is not None:
        titles = [p.get("title") for p in (literature.data.get("papers")
                                           or []) if p.get("title")]
        index = next((i for i, o in enumerate(observations)
                      if o.step_id == literature.step_id), None)
        if titles and index is not None and len(texts) > index:
            later = "\n".join(texts[index + 1:])
            checks["real_literature_titles_in_later_prompts"] = any(
                t in later for t in titles)
            checks["literature_titles_not_invented_before_search"] = (
                all(t not in texts[index] for t in titles)
                if index < len(texts) else True)

    checks["observations_nonempty_from_turn2"] = all(
        '"observations": []' not in text for text in texts[1:]
    ) if len(texts) > 1 else True
    return checks


def main() -> int:
    if "--verify-only" in sys.argv:
        # Re-check an existing dump against its own captured prompts.
        # No model call, no cost: the verification is a pure function of
        # the evidence the run already wrote.
        data = json.loads(OUT.read_text(encoding="utf-8"))
        from crea.agent.observer import Observation

        observations = [Observation.model_validate(o)
                        for o in data["observations"]]
        checks = verify_prompts(data["prompts"], observations)
        for name, ok in checks.items():
            print(f"{'PASS' if ok else 'FAIL'}  {name}")
        return 0 if all(checks.values()) else 1

    if DB.exists():
        DB.unlink()

    cfg = LLMConfig.from_env()
    if not (os.environ.get("CREA_LLM_API_KEY", "").strip()):
        print("NOT_TESTED: CREA_LLM_API_KEY is not configured")
        return 2
    inner = OpenAICompatibleLLMProvider(cfg)
    llm = RecordingProvider(inner)

    kb = KnowledgeBase(DB)
    kb.initialize()

    application = ResearchIntelligenceApplication(
        db_path=DB,
        llm_factory=lambda: llm,
        stack_factory=lambda document_ids=None: build_live_intelligence_stack(
            knowledge_base=kb))

    started = time.time()
    print(f"model={cfg.model} base_url={cfg.base_url}", flush=True)
    serialized = application.start(GOAL, budget=BUDGET)
    elapsed = time.time() - started
    session_id = serialized["session_id"]
    study_id = serialized["study_id"]
    live = application.live_result(session_id)

    print(f"\n=== outcome={serialized['outcome']} "
          f"status={serialized['status']} mode={serialized['mode']} ===")
    print(f"reason: {serialized['reason']}")
    print(f"duration: {elapsed:.1f}s  session: {session_id}  study: {study_id}")
    print(f"decisions={live.decisions_used} tool_calls={live.tool_calls_used} "
          f"model_calls={live.model_calls_used}")
    print(f"tokens in/out: {live.input_tokens}/{live.output_tokens}")

    # -- did the prompts actually carry the tool results? -------------------
    # Data-driven: the checked strings are read from the observations the
    # tools really returned, never from a hardcoded expectation. A prompt
    # may only carry a payload after the turn that produced it ran.
    checks = verify_prompts(llm.requests, live.observations)
    print("\n=== prompt verification (captured at the provider) ===")
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")

    # -- did any interpretation actually cite observation data? -------------
    cited = []
    for reading in live.interpretations:
        text = reading.interpretation
        if "C8H10N4O2" in text or "194.19" in text or "InChIKey" in text:
            cited.append({"step_id": reading.step_id, "text": text[:300]})
    print(f"\ninterpretations citing concrete observation data: {len(cited)}")
    for item in cited:
        print(f"  {item['step_id']}: {item['text']}")

    # -- the audit trail must be on disk ------------------------------------
    with KnowledgeBase(DB) as fresh:
        runs = fresh.list_ai_research_runs(study_id)
        turns = fresh.list_ai_research_turns(study_id, session_id)
    print(f"\npersisted: runs={len(runs)} turns={len(turns)}")
    print("turn order:",
          [(t.record.turn, t.record.decision_type.value if t.record.decision_type else None,
            t.tool_name) for t in turns])

    payload = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "git_commit": git_commit(),
            "provider": "openai-compatible",
            "model": cfg.model,
            "base_url": cfg.base_url,
            "db": str(DB),
            "study_id": study_id,
            "session_id": session_id,
            "mode": serialized["mode"],
            "duration_s": round(elapsed, 1),
            "budget": BUDGET.model_dump(mode="json"),
            "knowledge_base_injected": True,
        },
        "prompt_verification": checks,
        "interpretations_citing_data": cited,
        "prompt_char_counts": [
            {"turn": i + 1,
             "chars": len(r["system"]) + sum(len(m.get("content") or "")
                                             for m in r["messages"]),
             "has_formula": "C8H10N4O2" in json.dumps(r, ensure_ascii=False),
             "input_tokens": (live.decision_records[i].input_tokens
                              if i < len(live.decision_records) else None)}
            for i, r in enumerate(llm.requests)],
        "result": serialized,
        "decision_records": [
            r.model_dump(mode="json") for r in live.decision_records],
        "observations": [
            o.model_dump(mode="json", exclude={"raw"})
            for o in live.observations],
        "interpretations": [
            i.model_dump(mode="json") for i in live.interpretations],
        "tool_results": [
            t.model_dump(mode="json") for t in live.tool_results],
        "prompts": llm.requests,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                              default=str), encoding="utf-8")
    print(f"\nwrote {OUT}")

    # -- part B: a real second process reads the audit trail -----------------
    reader = textwrap.dedent(f"""
        import json
        from pathlib import Path
        from crea.app.research_intelligence import ResearchIntelligenceApplication
        app = ResearchIntelligenceApplication(db_path=Path(r"{DB}"))
        decisions = app.decisions({session_id!r})
        report = app.report({session_id!r})
        print(json.dumps({{
            "decisions": len(decisions),
            "types": [d["decision_type"] for d in decisions],
            "tools": [d["selected_tool"] for d in decisions],
            "report_head": report.splitlines()[0],
            "report_has_formula_section": "研究过程" in report,
        }}, ensure_ascii=False))
        """)
    done = subprocess.run([sys.executable, "-c", reader],
                          capture_output=True, text=True, timeout=180)
    print("\n=== cross-process audit read (subprocess) ===")
    if done.returncode == 0:
        print(done.stdout.strip().splitlines()[-1])
    else:
        print("FAILED:", done.stderr[-600:])

    llm.close()
    kb.close()
    passed = all(checks.values())
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

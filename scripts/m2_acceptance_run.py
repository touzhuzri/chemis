"""M2.0 real-model acceptance run (MiMo, OpenAI-compatible).

One controlled run through the formal AI Research Intelligence entry.
Small budget on purpose: this is a capability probe, not a literature
survey. Writes its own database so the operator's existing knowledge base
is untouched, and dumps every record it produced.
"""

from __future__ import annotations

import json
import pathlib
import platform
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from crea.agent.intelligence import ResearchBudget  # noqa: E402
from crea.agent.live import build_live_intelligence_stack  # noqa: E402
from crea.app.research_intelligence import (  # noqa: E402
    ResearchIntelligenceApplication,
)
from crea.llm import LLMConfig, OpenAICompatibleLLMProvider  # noqa: E402

DB = pathlib.Path("data") / "m2-acceptance.db"
OUT = pathlib.Path("data") / "m2-acceptance-run.json"

GOAL = (
    "请研究咖啡因（Caffeine）。给定结构 SMILES：Cn1c(=O)c2c(ncn2C)n(C)c1=O。"
    "首先确认输入结构是否有效，并分析其分子结构特征。随后根据研究目标自主选择"
    "合适的化学工具，计算基础分子描述符，并尝试查找相关反应与文献。请根据实际"
    "工具结果决定下一步研究方向。明确区分计算事实、文献证据、AI 推断和未知信息。"
    "不要编造不存在的反应、文献或实验数据。如果工具无法提供足够证据，请明确指出"
    "研究限制。"
)

BUDGET = ResearchBudget(
    max_decisions=6,
    max_tool_calls=5,
    max_model_calls=8,
    max_wall_clock_seconds=600.0,
    max_output_tokens=1400,
)


def git_commit() -> str:
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def main() -> int:
    if DB.exists():
        DB.unlink()

    cfg = LLMConfig.from_env()
    llm = OpenAICompatibleLLMProvider(cfg)
    application = ResearchIntelligenceApplication(
        db_path=DB,
        llm_factory=lambda: OpenAICompatibleLLMProvider(cfg),
        stack_factory=lambda document_ids=None: build_live_intelligence_stack(),
    )

    started = time.time()
    print(f"model={cfg.model} base_url={cfg.base_url}", flush=True)
    print(f"budget: decisions<={BUDGET.max_decisions} "
          f"tools<={BUDGET.max_tool_calls} models<={BUDGET.max_model_calls}",
          flush=True)

    serialized = application.start(GOAL, budget=BUDGET)
    elapsed = time.time() - started
    session_id = serialized["session_id"]
    live = application.live_result(session_id)

    print(f"\n=== outcome={serialized['outcome']} "
          f"status={serialized['status']} mode={serialized['mode']} ===")
    print(f"reason: {serialized['reason']}")
    print(f"duration: {elapsed:.1f}s")
    print(f"session: {session_id}  study: {serialized['study_id']}")
    print(f"decisions={live.decisions_used} tool_calls={live.tool_calls_used} "
          f"model_calls={live.model_calls_used}")
    print(f"tokens in/out: {live.input_tokens}/{live.output_tokens}")
    print(f"awaiting: {serialized['awaiting']}")

    payload = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "git_commit": git_commit(),
            "provider": live.model_provider,
            "model": live.model_name,
            "base_url": cfg.base_url,
            "db": str(DB),
            "study_id": serialized["study_id"],
            "session_id": session_id,
            "mode": serialized["mode"],
            "run_mode": "live / real model",
            "duration_s": round(elapsed, 1),
            "budget": BUDGET.model_dump(mode="json"),
        },
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
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                              default=str), encoding="utf-8")
    print(f"\nwrote {OUT}")
    llm.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

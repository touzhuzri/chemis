"""The Chemistry Agent (CREA 4.0 Stage E): orchestration, nothing more.

```
User message
   |
   v
LLM / decision layer  ->  AgentAction (a tool name and JSON arguments)
   |
   v
ToolRegistry          ->  validate, execute, ToolResult   <- the only path to a fact
   |
   v
tool results appended to the thread
   |
   v
LLM                   ->  ... or a final answer
   |
   v
AgentResult           (answer + tool_calls + evidence + provenance + diagnostics)
```

This is the Stage E MVP, deliberately thin. It owns NO chemistry: every fact
arrives as a `ToolResult` from the registry, and the agent only relays the
thread to the model, accumulates evidence/provenance/diagnostics, and stops.
The registry is an injected abstraction (`ToolRegistryLike`) -- the agent
never imports RDKit, a resolver, a provider, a repository or a chemistry
service (§5 of Stage E). The tool schemas come from `registry.describe()` --
one single source of truth, no hand-maintained second list (§7).

Two invariants the loop enforces rather than asks for:

* **Bounded.** `MAX_ITERATIONS` is a hard ceiling. Reaching it yields
  `AgentResult.status == "limit_reached"` with whatever the tools produced.
* **Nothing enters the result that a tool did not produce.** The thread the
  model sees is assembled from real `ToolResult`s; `evidence`, `provenance`
  and `diagnostics` are lifts of what the tools returned -- never the model's
  account of them. Uncertainty is preserved, not resolved: an `unavailable`
  tool stays unavailable, and an `ambiguous` resolution is relayed as
  ambiguous, never silently narrowed to `candidates[0]`.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from crea.agent.prompt import CHEMISTRY_SYSTEM
from crea.core.models.research import (
    ActionKind,
    AgentAction,
    AgentResult,
    ToolResult,
    ToolStatus,
)
from crea.llm.base import LLMProvider, LLMRequest, redact

#: The hard ceiling on one run (§8). Purpose: prevent `tool A -> tool B ->
#: tool A -> ...` from running forever. Reaching it is a stated outcome, not
#: a retry storm.
MAX_ITERATIONS = 8


@runtime_checkable
class ToolRegistryLike(Protocol):
    """What the agent needs from a tool registry (§5/§12).

    Two methods, both already implemented by `crea.tools.registry.
    ChemistryToolRegistry`. The agent depends on this abstraction, never on a
    concrete registry or on any chemistry object behind it.
    """

    def describe(self) -> list[dict]: ...
    def invoke(self, name: str, arguments: dict[str, Any] | None = None,
               step_id: str = "", **kwargs: Any) -> ToolResult: ...


class ChemistryAgent:
    """A minimal tool-calling agent over a tool registry and an LLM.

    `run(user_message)` is the whole interface. The agent does not decide
    chemistry; it decides nothing at all beyond the loop mechanics -- the
    model selects tools, the registry executes them, and the agent carries
    the honest books (evidence, provenance, diagnostics) forward.
    """

    def __init__(self, registry: ToolRegistryLike, llm: LLMProvider, *,
                 max_iterations: int = MAX_ITERATIONS) -> None:
        self._registry = registry
        self._llm = llm
        self._max_iterations = max_iterations

    # -- entry point --------------------------------------------------------

    def run(self, user_message: str, *,
            history: list[dict] | None = None) -> AgentResult:
        """Answer one message through the registry, bounded and honest.

        `history` is the prior conversation as neutral ``{"role", "content"}``
        messages (``user``/``assistant``), supplied by a caller that keeps a
        workspace. It seeds the thread before the current message so the model
        can resolve "它" against what came before (Stage F §8). The agent
        itself knows nothing about workspaces -- it receives a plain message
        list, never a repository (§30).
        """
        messages: list[dict] = list(history or [])
        messages.append({"role": "user", "content": user_message})
        tool_calls: list[ToolResult] = []
        evidence: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        diagnostics: list[str] = []
        iterations = 0

        for _ in range(self._max_iterations):
            iterations += 1
            try:
                response = self._llm.complete(LLMRequest(
                    system=CHEMISTRY_SYSTEM,
                    messages=self._thread(messages),
                    tools=self._tool_schemas()))
            except Exception as exc:  # noqa: BLE001 - a dead model is a run outcome
                diagnostics.append(
                    f"llm provider failed: {type(exc).__name__}: {exc}")
                return AgentResult(
                    answer="", tool_calls=tool_calls, evidence=evidence,
                    provenance=provenance, diagnostics=diagnostics,
                    iterations=iterations, status="model_failed")

            action = self._parse(response)
            if action is None:
                # Plain prose is a final answer, not a tool call.
                return AgentResult(
                    answer=response.text, tool_calls=tool_calls,
                    evidence=evidence, provenance=provenance,
                    diagnostics=diagnostics, iterations=iterations,
                    status="answered")

            if action.action is ActionKind.FINAL:
                return AgentResult(
                    answer=(action.summary or response.text).strip(),
                    tool_calls=tool_calls, evidence=evidence,
                    provenance=provenance, diagnostics=diagnostics,
                    iterations=iterations, status="answered")

            if action.action is ActionKind.CLARIFY:
                return AgentResult(
                    answer=(action.question or response.text).strip(),
                    tool_calls=tool_calls, evidence=evidence,
                    provenance=provenance, diagnostics=diagnostics,
                    iterations=iterations, status="answered")

            if action.action is ActionKind.TOOL_CALL:
                result = self._registry.invoke(
                    action.tool or "", action.arguments or {})
                tool_calls.append(result)
                self._absorb(result, evidence, provenance, diagnostics)
                messages.append(self._tool_result_message(result))
                continue

            diagnostics.append(f"unrecognized action kind: {action.action}")
            return AgentResult(
                answer="", tool_calls=tool_calls, evidence=evidence,
                provenance=provenance, diagnostics=diagnostics,
                iterations=iterations, status="model_failed")

        diagnostics.append(
            f"iteration limit reached after {iterations} tool turns "
            f"(MAX_ITERATIONS={self._max_iterations})")
        return AgentResult(
            answer="", tool_calls=tool_calls, evidence=evidence,
            provenance=provenance, diagnostics=diagnostics,
            iterations=iterations, status="limit_reached")

    # -- helpers ------------------------------------------------------------

    def _tool_schemas(self) -> list[dict]:
        """The declared tool surfaces, straight from the registry (§7)."""
        return self._registry.describe()

    def _thread(self, messages: list[dict]) -> list[dict]:
        """The model sees the tool catalogue and the real tool results.

        The catalogue is included in the thread so a provider without native
        tool calling still sees what exists; a native-calling provider also
        receives it through `LLMRequest.tools`. Nothing else -- never an
        account of a tool result written by the model.
        """
        catalogue = json.dumps(self._registry.describe(), ensure_ascii=False)
        head = [
            {"role": "user",
             "content": "Registered tools (JSON Schema each): " + catalogue},
        ]
        return head + messages

    @staticmethod
    def _tool_result_message(result: ToolResult) -> dict:
        """One tool result as a neutral thread message.

        The `tool_result(name) = ...` prefix is the established convention
        (see `crea.llm.openai_compatible.to_openai_messages`), so the neutral
        thread translates cleanly to a provider's native tool-message shape.
        """
        payload = redact(result.model_dump(mode="json"))
        return {
            "role": "user",
            "content": (f"tool_result({result.tool}) = "
                        + json.dumps(payload, ensure_ascii=False)),
        }

    @staticmethod
    def _parse(response) -> AgentAction | None:
        """Prefer a structured action; fall back to JSON in the text.

        A provider with native tool calling hands us `response.action`; the
        scripted provider puts JSON in `response.text`. Neither is privileged,
        and plain prose is `None` -- a final answer, not an error.
        """
        payload = response.action
        if payload is None:
            try:
                payload = json.loads(response.text or "")
            except (TypeError, ValueError):
                return None
        if not isinstance(payload, dict):
            return None
        try:
            return AgentAction.model_validate(payload)
        except Exception:  # noqa: BLE001 - an unusable action is no action
            return None

    @staticmethod
    def _absorb(result: ToolResult, evidence: list, provenance: list,
                diagnostics: list) -> None:
        """Lift what a tool actually returned into the aggregates (§11).

        Uncertainty is kept distinct on purpose:
        * `unavailable` / `error` / `rejected` get an explicit diagnostic --
          "could not query", never mistaken for "found nothing".
        * the tool's own `diagnostics` are forwarded (an empty `search_reactions`
          result carries "no known reaction matches" -- an honest miss, which
          is a different fact from an outage).
        """
        if result.provenance:
            provenance.append({"tool": result.tool, **(result.provenance or {})})

        if result.status is ToolStatus.UNAVAILABLE:
            diagnostics.append(
                f"{result.tool}: unavailable ({result.error or result.error_type})")
        elif result.status is ToolStatus.ERROR:
            diagnostics.append(
                f"{result.tool}: error ({result.error or result.error_type})")
        elif result.status is ToolStatus.REJECTED:
            diagnostics.append(
                f"{result.tool}: rejected ({result.error or result.error_type})")
        diagnostics.extend(result.diagnostics or [])

        for evidence_id in result.evidence_ids:
            entry = {"evidence_id": evidence_id, "tool": result.tool}
            if entry not in evidence:
                evidence.append(entry)
        if isinstance(result.data, dict):
            for item in result.data.get("results") or []:
                if not isinstance(item, dict):
                    continue
                for ev in item.get("evidence") or []:
                    if isinstance(ev, dict):
                        entry = {"tool": result.tool, **ev}
                        if entry not in evidence:
                            evidence.append(entry)


__all__ = ["MAX_ITERATIONS", "ChemistryAgent", "ToolRegistryLike"]

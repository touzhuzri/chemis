"""The evidence-aware agent (CREA Stage 6.4).

A bounded, read-only loop:

    Question -> LLM (choose a tool or answer) -> ToolRegistry (read-only tool)
        -> structured result -> ... -> grounded AgentAnswer

Hard bounds (§三): `max_iterations`, `max_tool_calls` and `max_execution_time`.
Every fact enters through a tool (§四/§五); the agent never touches a database,
a file, SQL or the network itself. The final answer is grounded: every id a
`fact` claim cites must have been returned by a tool in this run (§二十九);
anything the data cannot establish is a HYPOTHESIS, not a fact (§三十一/§三十二).

Results are transient (§十) -- nothing is persisted.
"""

from __future__ import annotations

import json
import time

from crea.agent.evidence_prompt import SYSTEM
from crea.agent.observer import observe_from_registry
from crea.agent.tools import ToolRegistry
from crea.core.ids import content_hash
from crea.core.models.agent_run import (
    AgentAnswer,
    AgentClaim,
    AgentStatus,
    ClaimType,
    EvidenceAgentResult,
    StopReason,
    ToolCallRecord,
)
from crea.core.models.modification import ModelMetadata
from crea.core.json_parse import parse_json_object
from crea.core.models.research import ToolStatus
from crea.llm.base import LLMRequest, redact

#: The hard bounds (§三). Explicit, configurable, tested.
MAX_ITERATIONS = 6
MAX_TOOL_CALLS = 12
MAX_EXECUTION_SECONDS = 60.0
#: How much of one tool result is shown back to the model (§四十八/§四十九).
MAX_TOOL_RESULT_CHARS = 4000


class EvidenceAgent:
    """A bounded, read-only, tool-using agent with grounded answers."""

    def __init__(self, registry: ToolRegistry, llm, *,
                 max_iterations: int = MAX_ITERATIONS,
                 max_tool_calls: int = MAX_TOOL_CALLS,
                 max_execution_seconds: float = MAX_EXECUTION_SECONDS,
                 temperature: float | None = 0.0) -> None:
        self._registry = registry
        self._llm = llm
        self._max_iterations = max_iterations
        self._max_tool_calls = max_tool_calls
        self._max_seconds = max_execution_seconds
        self._temperature = temperature

    def run(self, question: str, *, context: str = "") -> EvidenceAgentResult:
        """Answer one question through the read-only tool registry."""
        started = time.monotonic()
        run_id = content_hash(f"{question}|{self._llm.name}|{time.time_ns()}", 16)
        messages: list[dict] = [{"role": "user", "content": self._prompt(question, context)}]
        tool_calls: list[ToolCallRecord] = []
        gathered = _Gathered()
        warnings: list[str] = []
        seen_calls: set[str] = set()
        iterations = 0

        for _ in range(self._max_iterations):
            if time.monotonic() - started > self._max_seconds:
                return self._finish(run_id, question, AgentStatus.PARTIAL,
                                    StopReason.TIMEOUT, AgentAnswer(), tool_calls,
                                    iterations, gathered, warnings, started)
            iterations += 1
            response = self._ask(messages, warnings)
            if response is None:
                return self._finish(run_id, question, AgentStatus.FAILED,
                                    StopReason.PROVIDER_ERROR, AgentAnswer(),
                                    tool_calls, iterations, gathered, warnings,
                                    started)

            action = parse_json_object(response, required_key="action")
            if action is None:
                warnings.append("the model did not return a usable action")
                return self._finish(run_id, question, AgentStatus.FAILED,
                                    StopReason.INVALID_ACTION, AgentAnswer(),
                                    tool_calls, iterations, gathered, warnings,
                                    started)

            kind = action.get("action")
            if kind == "final":
                answer = self._ground(action.get("answer") or {}, gathered)
                status = AgentStatus.COMPLETED if self._all_grounded(answer) \
                    else AgentStatus.PARTIAL
                return self._finish(run_id, question, status,
                                    StopReason.FINAL_ANSWER, answer, tool_calls,
                                    iterations, gathered, warnings, started)

            if kind != "tool_call":
                warnings.append(f"unrecognized action {kind!r}")
                return self._finish(run_id, question, AgentStatus.FAILED,
                                    StopReason.INVALID_ACTION, AgentAnswer(),
                                    tool_calls, iterations, gathered, warnings,
                                    started)

            if len(tool_calls) >= self._max_tool_calls:
                return self._finish(run_id, question, AgentStatus.LIMIT_REACHED,
                                    StopReason.MAX_TOOL_CALLS, AgentAnswer(),
                                    tool_calls, iterations, gathered, warnings,
                                    started)

            tool = str(action.get("tool") or "")
            arguments = action.get("arguments") or {}
            key = f"{tool}|{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"
            if key in seen_calls:
                warnings.append(f"repeated tool call: {tool}")
                return self._finish(run_id, question, AgentStatus.PARTIAL,
                                    StopReason.REPEATED_TOOL_CALL, AgentAnswer(),
                                    tool_calls, iterations, gathered, warnings,
                                    started)
            seen_calls.add(key)

            record, observation = self._call_tool(tool, arguments, gathered)
            tool_calls.append(record)
            messages.append({"role": "user", "content": observation})

        return self._finish(run_id, question, AgentStatus.LIMIT_REACHED,
                            StopReason.MAX_ITERATIONS, AgentAnswer(), tool_calls,
                            iterations, gathered, warnings, started)

    # -- pieces -------------------------------------------------------------

    def _ask(self, messages: list[dict], warnings: list[str]) -> str | None:
        catalogue = json.dumps(
            [{"name": t["name"], "description": t["description"],
              "arguments": t["arguments"]} for t in self._registry.describe()],
            ensure_ascii=False)
        try:
            response = self._llm.complete(LLMRequest(
                system=SYSTEM, temperature=self._temperature,
                messages=[{"role": "user", "content": "Registered tools: " + catalogue}]
                + messages))
        except Exception as exc:  # noqa: BLE001 - a dead model is a run outcome
            warnings.append(f"llm provider failed: {type(exc).__name__}: {exc}")
            return None
        return response.text

    def _call_tool(self, tool: str, arguments: dict, gathered: "_Gathered"
                   ) -> tuple[ToolCallRecord, str]:
        result = self._registry.execute(tool, arguments)
        status = result.status.value
        output_ids = _ids_in(result.data)
        gathered.add(output_ids)
        summary = _summarise(result.data)
        record = ToolCallRecord(
            tool_call_id=content_hash(f"{tool}|{time.time_ns()}", 12),
            tool_name=tool, arguments=redact(arguments), status=status,
            output_summary=summary,
            output_ids=sorted({i for ids in output_ids.values() for i in ids}))
        # The observation is what the model reads: kind, needs-input reasons
        # and diagnostics travel with the payload, so a rejected call is not
        # mistaken for a search that came back empty.
        observed = observe_from_registry(self._registry, tool, result)
        payload = observed.to_context()
        payload["data"] = _bounded(payload.get("data"))
        observation = json.dumps(payload, ensure_ascii=False)[:MAX_TOOL_RESULT_CHARS]
        return record, f"tool_result({tool}) = {observation}"

    # -- grounding (§二十九) ------------------------------------------------

    def _ground(self, raw: dict, gathered: "_Gathered") -> AgentAnswer:
        claims: list[AgentClaim] = []
        for item in raw.get("claims") or []:
            if not isinstance(item, dict):
                continue
            claim = _claim(item, gathered)
            claims.append(claim)
        return AgentAnswer(
            summary=str(raw.get("summary") or ""),
            claims=claims,
            limitations=[str(x) for x in (raw.get("limitations") or []) if x],
            sources=_sources(gathered))

    @staticmethod
    def _all_grounded(answer: AgentAnswer) -> bool:
        return all(c.grounded for c in answer.claims)

    # -- finish -------------------------------------------------------------

    def _finish(self, run_id: str, question: str, status: AgentStatus,
                stop_reason: StopReason, answer: AgentAnswer,
                tool_calls: list[ToolCallRecord], iterations: int,
                gathered: "_Gathered", warnings: list[str],
                started: float) -> EvidenceAgentResult:
        return EvidenceAgentResult(
            run_id="arun-" + run_id, question=question, status=status,
            stop_reason=stop_reason, answer=answer, tool_calls=list(tool_calls),
            iterations=iterations, gathered=gathered.as_dict(),
            warnings=list(warnings),
            model_metadata=ModelMetadata(
                provider=getattr(self._llm, "name", ""),
                model=getattr(self._llm, "model", ""),
                temperature=self._temperature, request_id=run_id))

    @staticmethod
    def _prompt(question: str, context: str) -> str:
        if context:
            return f"Context:\n{context}\n\nQuestion: {question}"
        return f"Question: {question}"


class _Gathered:
    """The ids the run actually saw, so grounding is decidable (§二十九)."""

    def __init__(self) -> None:
        self.reaction: set[str] = set()
        self.evidence: set[str] = set()
        self.literature: set[str] = set()
        self.route: set[str] = set()
        self.molecule: set[str] = set()

    def add(self, ids: dict[str, set[str]]) -> None:
        self.reaction |= ids["reaction"]
        self.evidence |= ids["evidence"]
        self.literature |= ids["literature"]
        self.route |= ids["route"]
        self.molecule |= ids["molecule"]

    def as_dict(self) -> dict:
        return {"reaction_template_ids": sorted(self.reaction),
                "evidence_ids": sorted(self.evidence),
                "literature_ids": sorted(self.literature),
                "route_ids": sorted(self.route),
                "molecule_ids": sorted(self.molecule)}


#: Keys in a tool result that name an id of that kind.
_ID_KEYS = {
    "reaction_template_id": "reaction",
    "reaction_template_ids": "reaction",
    "evidence_id": "evidence",
    "evidence_ids": "evidence",
    "literature_id": "literature",
    "paper_id": "literature",
    "route_id": "route",
    "molecule_id": "molecule",
    "entity_id": "molecule",
    "inchikey": "molecule",
}


def _ids_in(data) -> dict[str, set[str]]:
    out = {k: set() for k in ("reaction", "evidence", "literature", "route", "molecule")}
    _collect_ids(data, out)
    return out


def _collect_ids(value, out: dict[str, set[str]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            kind = _ID_KEYS.get(key)
            if kind and isinstance(item, str) and item:
                out[kind].add(item)
            elif kind and isinstance(item, list):
                for entry in item:
                    if isinstance(entry, str) and entry:
                        out[kind].add(entry)
            else:
                _collect_ids(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect_ids(item, out)


def _bounded(data):
    """A bounded view of a tool result: sizes capped, ids always kept (§四十九)."""
    if not isinstance(data, dict):
        return data
    out = {}
    for key, value in data.items():
        if isinstance(value, list) and len(value) > 20:
            out[key] = value[:20]
            out[f"{key}_truncated"] = len(value) - 20
        else:
            out[key] = value
    return out


def _summarise(data) -> str:
    if not isinstance(data, dict):
        return str(data)[:120]
    status = data.get("status", "")
    count = data.get("count")
    if count is not None:
        return f"{status}: {count} record(s)"
    if "candidates" in data:
        return f"{status}: {len(data['candidates'])} candidate(s)"
    return f"{status}" if status else "ok"


def _sources(gathered: "_Gathered") -> list[dict]:
    sources: list[dict] = []
    for rid in sorted(gathered.reaction):
        sources.append({"kind": "reaction", "id": rid})
    for pid in sorted(gathered.literature):
        sources.append({"kind": "literature", "id": pid})
    for eid in sorted(gathered.evidence):
        sources.append({"kind": "evidence", "id": eid})
    return sources


def _claim(item: dict, gathered: "_Gathered") -> AgentClaim:
    reaction = [str(x) for x in (item.get("reaction_ids") or [])]
    evidence = [str(x) for x in (item.get("evidence_ids") or [])]
    literature = [str(x) for x in (item.get("literature_ids") or [])]
    routes = [str(x) for x in (item.get("route_ids") or [])]
    try:
        claim_type = ClaimType(str(item.get("claim_type") or "fact"))
    except ValueError:
        claim_type = ClaimType.INTERPRETATION

    # keep only ids the run actually gathered; a fact with none is not grounded
    reaction = [x for x in reaction if x in gathered.reaction]
    evidence = [x for x in evidence if x in gathered.evidence]
    literature = [x for x in literature if x in gathered.literature]
    routes = [x for x in routes if x in gathered.route]
    cited = bool(reaction or evidence or literature or routes)
    grounded = claim_type is not ClaimType.FACT or cited
    # a hypothesis is never experimentally established
    not_verified = claim_type is not ClaimType.FACT
    return AgentClaim(
        text=str(item.get("text") or ""), claim_type=claim_type,
        not_experimentally_verified=not_verified,
        reaction_ids=reaction, evidence_ids=evidence,
        literature_ids=literature, route_ids=routes,
        rationale=str(item.get("rationale") or ""), grounded=grounded)


__all__ = [
    "MAX_EXECUTION_SECONDS",
    "MAX_ITERATIONS",
    "MAX_TOOL_CALLS",
    "EvidenceAgent",
]

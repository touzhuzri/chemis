"""M1.2 -- the observer: what actually happened in one tool call.

A `ToolResult` already carries the raw evidence -- status, data, error,
diagnostics, provenance. What it does not carry is a *reading*: was that an
answer, an honest miss, a missing prerequisite, an outage, a partial result or
a real failure? Every caller that interprets `status == "ok"` with empty data
as "found nothing" ends up telling the user something false.

`Observation` is that reading. It is derived deterministically from machine
signals already present -- `ToolStatus`, the tool's declared `ToolSpec`, the
policy rejection code, and the structured `status` / `outcome` vocabulary the
tools themselves emit. There is no model call here, no retry, no replanning,
and no second status enum: the observer is a pure function of the result.

The six kinds are the only vocabulary:

* `SUCCESS`     -- ran, and nothing says the answer is empty or degraded
* `NO_RESULT`   -- ran, and the query matched nothing
* `NEEDS_INPUT` -- arguments, identity, prerequisite, approval or policy
                   stopped the work; a person must act before it can continue
* `UNAVAILABLE` -- a provider, database or capability could not be reached
* `PARTIAL`     -- some reliable results exist, but a branch is missing
* `ERROR`       -- an exception or an unclassifiable execution failure

`NO_RESULT` and `UNAVAILABLE` are kept apart on purpose: "no paper matched" and
"the literature provider could not be asked" are different facts, and the whole
project depends on not showing the second as the first.
"""

from __future__ import annotations

from collections.abc import Container
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from crea.core.models.research import ToolResult, ToolStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from crea.agent.tools import ToolSpec


class ObservationKind(str, Enum):
    """The one vocabulary for what a tool call produced."""

    SUCCESS = "success"
    NO_RESULT = "no_result"
    NEEDS_INPUT = "needs_input"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"
    ERROR = "error"


#: Policy rejections emitted by the M1.1 execution guard. All of them mean
#: "a person or the operator has to change something", never "the tool failed".
_POLICY_CODES = frozenset({
    "APPROVAL_REQUIRED", "PRECONDITION_NOT_MET", "NETWORK_NOT_ALLOWED",
    "MODEL_NOT_ALLOWED",
})

#: The structured `status`/`outcome` vocabulary the tools already emit.
#: Deliberately explicit: an unrecognized token is not guessed at.
_STATUS_KINDS: dict[str, ObservationKind] = {
    # success
    "ok": ObservationKind.SUCCESS,
    "success": ObservationKind.SUCCESS,
    "available": ObservationKind.SUCCESS,
    "resolved": ObservationKind.SUCCESS,
    "merged": ObservationKind.SUCCESS,
    "generated": ObservationKind.SUCCESS,
    "complete": ObservationKind.SUCCESS,
    # honest miss
    "no_results": ObservationKind.NO_RESULT,
    "no_result": ObservationKind.NO_RESULT,
    "no_match": ObservationKind.NO_RESULT,
    "not_found": ObservationKind.NO_RESULT,
    "no_route": ObservationKind.NO_RESULT,
    "discovery_only": ObservationKind.NO_RESULT,
    "empty": ObservationKind.NO_RESULT,
    # capability absent or unreachable
    "unavailable": ObservationKind.UNAVAILABLE,
    "not_configured": ObservationKind.UNAVAILABLE,
    "no_knowledge_base": ObservationKind.UNAVAILABLE,
    "provider_unavailable": ObservationKind.UNAVAILABLE,
    "all_providers_failed": ObservationKind.UNAVAILABLE,
    # partial
    "partial": ObservationKind.PARTIAL,
    "partial_success": ObservationKind.PARTIAL,
    "degraded": ObservationKind.PARTIAL,
    # blocked on a person or on arguments
    "rejected": ObservationKind.NEEDS_INPUT,
    "blocked": ObservationKind.NEEDS_INPUT,
    "invalid_query": ObservationKind.NEEDS_INPUT,
    "invalid": ObservationKind.NEEDS_INPUT,
    "ambiguous": ObservationKind.NEEDS_INPUT,
    "unresolved": ObservationKind.NEEDS_INPUT,
    "no_structures": ObservationKind.NEEDS_INPUT,
    "candidate": ObservationKind.NEEDS_INPUT,
    "input": ObservationKind.NEEDS_INPUT,
    "selection_required": ObservationKind.NEEDS_INPUT,
    # a real failure
    "error": ObservationKind.ERROR,
    "failed": ObservationKind.ERROR,
}

#: Keys whose value is a count of what a query returned.
_COUNT_KEYS = ("count", "paper_count", "route_count", "record_count",
               "hit_count", "candidate_count")

#: Keys whose value is a list of results. An answer that carries only empty
#: lists is an honest miss, not a success.
_RESULT_LIST_KEYS = frozenset({
    "papers", "reactions", "results", "routes", "matches", "hits", "records",
    "entities", "candidates", "evidence", "gaps", "findings", "items",
    "claims", "observations", "conflicts", "templates", "transformations",
    "complete", "blocked", "paths", "nodes", "edges", "links",
})

#: `validate_route_step` reports the *step's* status (candidate, unverified,
#: conflicting ...), which says how supported a step is -- not how the call
#: went. Reading it as a tool outcome would turn every graded step into a
#: "needs input".
_STEP_STATUS_TOOLS = frozenset({"validate_route_step"})


def _attribute(spec: Any, name: str, default: Any) -> Any:
    return getattr(spec, name, default) if spec is not None else default


def _token_kind(token: str) -> ObservationKind | None:
    return _STATUS_KINDS.get(token.strip().lower())


class Observation(BaseModel):
    """One tool call, read honestly.

    Everything the `ToolResult` carried is retained: `data` is the original
    payload untouched, `diagnostics`/`evidence_ids`/`provenance` are copied
    as-is, and `raw` holds the whole result (excluded from serialization
    because `data` and the metadata fields already carry it).
    """

    kind: ObservationKind
    tool: str
    #: The canonical tool when `tool` was an alias; equal to `tool` otherwise.
    canonical_tool: str = ""
    step_id: str = ""
    summary: str = ""
    #: The raw `ToolStatus` value, kept for traceability.
    raw_status: str = ""
    #: The machine-readable reason: a policy code, the tool's own structured
    #: status token, or an exception class name.
    error_code: str | None = None
    error_type: str | None = None
    error: str | None = None
    data: Any = None
    diagnostics: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] | None = None
    duration_ms: int = 0
    #: Output was shortened by the registry's budget. Truncation is not failure.
    truncated: bool = False
    original_chars: int | None = None
    retryable: bool = False
    needs_user_input: bool = False
    #: What has to be supplied before the call can succeed, when known:
    #: missing fact names, "approval", "network_access", "valid_arguments" ...
    missing_requirements: list[str] = Field(default_factory=list)
    raw: ToolResult | None = Field(default=None, exclude=True)

    # -- construction -------------------------------------------------------

    @classmethod
    def from_tool_result(cls, result: ToolResult, *, spec: "ToolSpec | None" = None,
                         facts: Container[str] | None = None) -> "Observation":
        """Read one `ToolResult` without executing anything.

        `spec` supplies the declared policy (preconditions, retryability,
        alias) and `facts` the session facts; both are optional so legacy
        callers keep working with a bare result.
        """
        data = result.data
        kind, code = _classify(result, spec, data)
        diagnostics = list(result.diagnostics or [])
        error_text = _error_text(result, data, kind)
        missing = _missing_requirements(kind, code, spec, facts, data)
        canonical = _attribute(spec, "alias_of", None) or result.tool

        observation = cls(
            kind=kind,
            tool=result.tool,
            canonical_tool=canonical,
            step_id=result.step_id,
            summary=_summarise(kind, result.tool, data, error_text, missing),
            raw_status=result.status.value,
            error_code=code,
            error_type=result.error_type,
            error=error_text,
            data=data,
            diagnostics=diagnostics,
            evidence_ids=list(result.evidence_ids or []),
            provenance=result.provenance,
            duration_ms=result.duration_ms,
            truncated=bool(isinstance(data, dict) and data.get("truncated") is True),
            original_chars=_original_chars(data),
            retryable=_retryable(kind, spec),
            needs_user_input=kind is ObservationKind.NEEDS_INPUT,
            missing_requirements=missing,
            raw=result,
        )
        return observation

    # -- views --------------------------------------------------------------

    @property
    def succeeded(self) -> bool:
        return self.kind is ObservationKind.SUCCESS

    def to_context(self) -> dict[str, Any]:
        """The observation as the model sees it.

        The full payload is included: a summary is never a substitute for what
        the tool returned, and an id that disappeared here would be an id the
        model could not cite correctly later.
        """
        out: dict[str, Any] = {
            "tool": self.tool,
            "kind": self.kind.value,
            "raw_status": self.raw_status,
            "summary": self.summary,
            "needs_user_input": self.needs_user_input,
            "missing_requirements": list(self.missing_requirements),
            "retryable": self.retryable,
            "error_code": self.error_code,
            "error_type": self.error_type,
            "error": self.error,
            "truncated": self.truncated,
            "original_chars": self.original_chars,
            "data": self.data,
            "diagnostics": list(self.diagnostics),
            "evidence_ids": list(self.evidence_ids),
        }
        if self.canonical_tool and self.canonical_tool != self.tool:
            out["canonical_tool"] = self.canonical_tool
        if self.provenance:
            out["provenance"] = self.provenance
        if self.step_id:
            out["step_id"] = self.step_id
        if self.duration_ms:
            out["duration_ms"] = self.duration_ms
        return out


def observe(result: ToolResult, *, spec: "ToolSpec | None" = None,
            facts: Container[str] | None = None) -> Observation:
    """Read a result. A module-level alias for the classmethod."""
    return Observation.from_tool_result(result, spec=spec, facts=facts)


def observe_from_registry(registry: Any, name: str,
                          result: ToolResult) -> Observation:
    """Read a result using whatever the registry declares about the tool.

    `ToolRegistry` exposes `get()` and `facts`; `ChemistryToolRegistry`
    exposes `get()` only. A registry that exposes neither still works -- the
    observation just carries no declared policy.
    """
    getter = getattr(registry, "get", None)
    spec = getter(name) if callable(getter) else None
    facts = getattr(registry, "facts", None)
    return Observation.from_tool_result(result, spec=spec, facts=facts)


# ---------------------------------------------------------------------------
# Classification: deterministic, priority ordered, no keyword prose matching
# ---------------------------------------------------------------------------


def _classify(result: ToolResult, spec: Any,
              data: Any) -> tuple[ObservationKind, str | None]:
    status = result.status

    if status is ToolStatus.REJECTED:
        # Policy rejections and schema failures mean the tool never ran.
        code = result.error_type or ""
        if code in _POLICY_CODES or code == "InvalidArguments":
            return ObservationKind.NEEDS_INPUT, code or None
        # A rejection we cannot classify is not a success and not a policy
        # block; it is an unreadable failure.
        return ObservationKind.ERROR, code or None

    if status is ToolStatus.UNAVAILABLE:
        return ObservationKind.UNAVAILABLE, result.error_type or None

    if status is ToolStatus.ERROR:
        return ObservationKind.ERROR, result.error_type or None

    # ToolStatus.OK from here on: the tool ran, but its payload may still
    # say the work did not complete. The payload wins over the envelope.
    signal = _data_signal(result.tool, data)
    if signal is not None:
        kind, token = signal
        if kind is ObservationKind.SUCCESS:
            empty = _empty_signal(result.tool, data)
            if empty is not None:
                return empty, "no_results"
        return kind, token

    if isinstance(data, dict) and data.get("partial") is True:
        return ObservationKind.PARTIAL, "partial"

    empty = _empty_signal(result.tool, data)
    if empty is not None:
        return empty, "no_results"

    return ObservationKind.SUCCESS, None


def _data_signal(tool: str, data: Any) -> tuple[ObservationKind, str] | None:
    """Read the payload's own structured status/outcome vocabulary."""
    if not isinstance(data, dict):
        return None

    if tool == "get_reaction_details":
        # The extractor's `rejected` means "nothing survived extraction", not
        # "the model supplied bad arguments".
        outcome = data.get("outcome")
        if isinstance(outcome, str) and outcome.strip():
            kind = _token_kind(outcome)
            if kind is ObservationKind.NEEDS_INPUT:
                return ObservationKind.NO_RESULT, outcome.strip()
            if kind is None:
                return ObservationKind.ERROR, outcome.strip()
            return kind, outcome.strip()
        return None

    if tool in _STEP_STATUS_TOOLS:
        # `status` is the step's support status, not this call's outcome.
        return None

    for key in ("outcome", "status"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        token = value.strip()
        kind = _token_kind(token)
        if kind is None:
            # An explicit but unclassified status: report it as an error
            # rather than letting it fall through to SUCCESS.
            return ObservationKind.ERROR, token
        return kind, token

    if data.get("chemistry_allowed") is False:
        # Identity gate: no chemistry may run on an unresolved identity.
        return ObservationKind.NEEDS_INPUT, "chemistry_not_allowed"
    if data.get("available") is False:
        return ObservationKind.UNAVAILABLE, "unavailable"
    return None


def _empty_signal(tool: str, data: Any) -> ObservationKind | None:
    """An `OK` result that carries no result at all."""
    if isinstance(data, list):
        return ObservationKind.NO_RESULT if not data else None
    if not isinstance(data, dict):
        return None
    if data.get("found") is False:
        return ObservationKind.NO_RESULT
    if _positive(data):
        return None

    for key in _COUNT_KEYS:
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value == 0:
            return ObservationKind.NO_RESULT

    present = [k for k in _RESULT_LIST_KEYS
               if k in data and isinstance(data[k], list)]
    if present and all(not data[k] for k in present):
        return ObservationKind.NO_RESULT
    return None


def _positive(data: dict) -> bool:
    """An explicit statement that something *was* established."""
    return data.get("chemistry_allowed") is True or data.get("status") == "resolved"


def _original_chars(data: Any) -> int | None:
    if isinstance(data, dict):
        value = data.get("original_chars")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _retryable(kind: ObservationKind, spec: Any) -> bool:
    declared = bool(_attribute(spec, "retryable", True))
    if kind in (ObservationKind.SUCCESS, ObservationKind.NO_RESULT,
                ObservationKind.NEEDS_INPUT):
        # Repeating an identical call will not change any of these.
        return False
    return declared


def _error_text(result: ToolResult, data: Any,
                kind: ObservationKind) -> str | None:
    if result.error:
        return result.error
    if kind in (ObservationKind.SUCCESS, ObservationKind.NO_RESULT):
        return None
    if isinstance(data, dict):
        for key in ("error", "reason"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _missing_requirements(kind: ObservationKind, code: str | None, spec: Any,
                          facts: Container[str] | None, data: Any) -> list[str]:
    if kind is not ObservationKind.NEEDS_INPUT:
        return []
    missing: list[str] = []

    requires = tuple(_attribute(spec, "requires", ()) or ())
    if facts is None:
        # Without the session facts the only honest statement is the one the
        # registry already made: these preconditions were not met.
        if code == "PRECONDITION_NOT_MET":
            missing.extend(requires)
    else:
        missing.extend(name for name in requires if name not in facts)

    if code == "APPROVAL_REQUIRED":
        missing.append("approval")
    elif code == "NETWORK_NOT_ALLOWED":
        missing.append("network_access")
    elif code == "MODEL_NOT_ALLOWED":
        missing.append("model_access")
    elif code == "InvalidArguments":
        missing.append("valid_arguments")
    elif code == "no_route":
        missing.append("routes_held")
    elif code == "no_structures":
        missing.append("reactants_or_products")
    elif code in ("ambiguous", "unresolved"):
        missing.append("user_selection")
    elif code in ("blocked", "invalid", "input", "candidate",
                  "chemistry_not_allowed"):
        missing.append("resolved_identity")
    elif code == "invalid_query":
        missing.append("valid_query")
    elif code == "rejected":
        codes = data.get("reason_codes") if isinstance(data, dict) else None
        if isinstance(codes, list) and "SELECTION_WITHOUT_USER_STATEMENT" in codes:
            missing.append("user_selection")
        else:
            missing.append("valid_input")

    return list(dict.fromkeys(missing))


# ---------------------------------------------------------------------------
# Summary: deterministic prose, never a substitute for the payload
# ---------------------------------------------------------------------------


def _summarise(kind: ObservationKind, tool: str, data: Any,
               error: str | None, missing: list[str]) -> str:
    reason = _reason(data) or error
    if kind is ObservationKind.SUCCESS:
        detail = _count_detail(data)
        text = f"{tool} completed"
        if detail:
            text += f" ({detail})"
        return _clip(text)
    if kind is ObservationKind.NO_RESULT:
        text = f"{tool} completed but found no matching results"
        return _clip(f"{text}: {reason}" if reason else text)
    if kind is ObservationKind.NEEDS_INPUT:
        requirement = f" (needs: {', '.join(missing)})" if missing else ""
        text = f"{tool} needs user input before the work can continue"
        text += requirement
        return _clip(f"{text}: {reason}" if reason else text)
    if kind is ObservationKind.UNAVAILABLE:
        text = f"{tool} is unavailable"
        return _clip(f"{text}: {reason}" if reason else text)
    if kind is ObservationKind.PARTIAL:
        text = f"{tool} returned partial results"
        return _clip(f"{text}: {reason}" if reason else text)
    text = f"{tool} failed"
    return _clip(f"{text}: {reason}" if reason else text)


def _reason(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in ("reason", "error"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("notes", "limitations"):
        items = data.get(key)
        if isinstance(items, list) and items and isinstance(items[0], str):
            return items[0]
    return None


def _count_detail(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in _COUNT_KEYS:
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return f"{value} result(s)"
    return ""


def _clip(text: str, limit: int = 400) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = [
    "Observation", "ObservationKind", "observe", "observe_from_registry",
]

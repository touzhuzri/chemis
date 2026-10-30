"""The tool registry: the only way anything actually happens.

A model names a registered tool and supplies a JSON object. The registry
validates the arguments against that tool's schema, executes it, and returns a
`ToolResult`. There is no `eval`, no `exec`, no dynamic import, and no path by
which model-supplied text becomes code.

Two boundaries this module exists to hold:

* **A model cannot report a tool result.** `ToolResult` is constructed here,
  from a real return value or a real exception. Nothing writes one from what a
  model said.
* **A model cannot reach the network or the filesystem.** Every path to either
  is behind a registered tool with a declared schema; a document tool only
  reads a `document_id` the operator already put in the library.

M1.1 (CREA V1.1): the contract now declares *what* a tool touches -- access,
effects, approval, timeout, output budget and preconditions -- and the
registry can enforce those declarations. Enforcement is opt-in per registry:
the three legacy registries keep `enforce_policy=False`, so their existing
callers behave exactly as before. `build_agent_tools()` is the enforcing one.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ValidationError

from crea.core.models.research import ToolResult, ToolStatus
from crea.llm.base import redact


class Access(str, Enum):
    """What external resource a tool reaches for.

    A tuple on the spec: a tool can be local *and* optionally use a provider
    (`resolve_chemical_identity` on a name), or local *and* call a model
    (`get_reaction_details` when an extractor is configured).
    """

    LOCAL = "local"
    NETWORK = "network"
    MODEL = "model"


class Effects(str, Enum):
    """What a tool changes. Network access is not an effect."""

    NONE = "none"
    #: The current run's session/workbench only (evidence sink, route ledger).
    TEMPORARY = "temporary"
    #: The knowledge base, evidence store or other durable data.
    PERSISTENT = "persistent"


class Approval(str, Enum):
    NONE = "none"
    REQUIRED = "required"


class PolicyError(str, Enum):
    """Normalized policy rejection codes.

    They travel on `ToolResult.error_type` and in the diagnostics; the
    observer (`crea.agent.observer`) reads them as `NEEDS_INPUT`, so there is
    exactly one status vocabulary and no duplicate enum here."""

    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    NETWORK_NOT_ALLOWED = "NETWORK_NOT_ALLOWED"
    MODEL_NOT_ALLOWED = "MODEL_NOT_ALLOWED"
    PRECONDITION_NOT_MET = "PRECONDITION_NOT_MET"


@dataclass
class ExecutionPolicy:
    """What this registry is allowed to do.

    Defaults are deliberately restrictive: no network, no model calls, and
    persistent writes need an explicit approval on the call. The legacy
    registries are non-enforcing and therefore unaffected.
    """

    allow_network: bool = False
    allow_model_calls: bool = False
    require_approval_for_persistent: bool = True


@dataclass
class SessionFacts:
    """Facts about the current run that a tool precondition may rely on.

    `identity_resolved` and `routes_held` are the first two. A fact is set by
    the tool that establishes it (see `ToolSpec.fact_rule`) or by the caller;
    it is never inferred from a model's prose.
    """

    satisfied: set[str] = field(default_factory=set)

    def add(self, *names: str) -> None:
        self.satisfied.update(n for n in names if n)

    def clear(self, *names: str) -> None:
        self.satisfied.difference_update(n for n in names if n)

    def __contains__(self, name: object) -> bool:
        return name in self.satisfied

    def as_list(self) -> list[str]:
        return sorted(self.satisfied)


class _ToolFailure(Exception):
    """An underlying tool returned a non-OK `ToolResult`.

    The unified surface delegates to the legacy tools; this exception carries
    the original result so the delegating registry can preserve its status,
    error type, diagnostics and evidence ids instead of flattening everything
    into a generic ERROR.
    """

    def __init__(self, result: ToolResult) -> None:
        self.result = result
        super().__init__(result.error or result.status.value)


@dataclass
class ToolSpec:
    """A tool's declared surface and the resources it is allowed to touch.

    `permission` and `mutates_evidence` are the pre-M1.1 fields. They are kept
    and mapped: a tool that mutates evidence is at least `TEMPORARY`; a tool
    declared `write` without a more specific effect is `PERSISTENT`. New code
    should set `access`/`effects`/`approval` explicitly.
    """

    name: str
    description: str
    arguments_model: type[BaseModel]
    run: Callable[..., Any]
    mutates_evidence: bool = False
    permission: str = "read_only"
    #: Resources: any of "local", "network", "model".
    access: tuple[str, ...] = ()
    effects: str = ""
    approval: str = ""
    #: `native` means the backend enforces it (HTTP/provider/worker timeout).
    #: `advisory` means the registry cannot interrupt the call and will only
    #: report an overrun after it returns -- it never claims it stopped it.
    timeout_s: float = 60.0
    timeout_mode: str = "native"
    #: 0 disables truncation (legacy registries). The unified surface sets a
    #: positive budget for every tool.
    output_limit_chars: int = 0
    #: Facts the current run must hold before this tool may run.
    requires: tuple[str, ...] = ()
    retryable: bool = True
    #: Aliases point at a canonical tool; they are not a second implementation.
    alias_of: str | None = None
    canonical: bool = True
    #: Facts established by a successful call.
    sets_facts: tuple[str, ...] = ()
    #: Conditional facts, derived from the result data (e.g. identity RESOLVED).
    fact_rule: Callable[[Any], tuple[str, ...]] | None = None
    network_optional: bool = False
    model_optional: bool = False

    def __post_init__(self) -> None:
        if not self.access:
            self.access = (Access.LOCAL.value,)
        if not self.effects:
            if self.mutates_evidence:
                self.effects = Effects.TEMPORARY.value
            elif self.permission == "write":
                self.effects = Effects.PERSISTENT.value
            else:
                self.effects = Effects.NONE.value
        if not self.approval:
            self.approval = (Approval.REQUIRED.value
                             if self.effects == Effects.PERSISTENT.value
                             else Approval.NONE.value)
        # One vocabulary: `permission` is a compatibility view over effects.
        if self.effects != Effects.NONE.value:
            self.permission = "write"
        elif self.permission not in ("read_only", "write"):
            self.permission = "read_only"

    @property
    def input_schema(self) -> dict:
        return self.arguments_model.model_json_schema()

    @property
    def is_alias(self) -> bool:
        return self.alias_of is not None


class ToolRegistry:
    def __init__(self, *, enforce_policy: bool = False,
                 policy: ExecutionPolicy | None = None,
                 facts: SessionFacts | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self.enforce_policy = enforce_policy
        self.policy = policy if policy is not None else ExecutionPolicy()
        self.facts = facts if facts is not None else SessionFacts()
        self.alias_of: dict[str, str] = {}
        self.canonical_names: list[str] = []

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = spec
        if spec.alias_of is not None:
            self.alias_of[spec.name] = spec.alias_of
        if spec.canonical and spec.alias_of is None:
            self.canonical_names.append(spec.name)
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> list[dict]:
        """What a model is told exists. Descriptions plus declared policy.

        `arguments` is kept for the pre-M1.1 callers; `input_schema` is the
        same schema under the M1.1 name. Everything else is additive.
        """
        out = []
        for spec in sorted(self._tools.values(), key=lambda t: t.name):
            schema = spec.input_schema
            out.append({
                "name": spec.name,
                "description": spec.description,
                "arguments": schema,
                "input_schema": schema,
                "permission": spec.permission,
                "access": list(spec.access),
                "effects": spec.effects,
                "approval": spec.approval,
                "timeout_s": spec.timeout_s,
                "timeout_mode": spec.timeout_mode,
                "output_limit_chars": spec.output_limit_chars,
                "requires": list(spec.requires),
                "retryable": spec.retryable,
                "alias_of": spec.alias_of,
            })
        return out

    def execute(self, name: str, arguments: dict[str, Any] | None = None,
                step_id: str = "", *, approved: bool = False) -> ToolResult:
        """Validate, guard, run, normalize. Never raises for a tool-level
        problem."""
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(
                tool=name, status=ToolStatus.ERROR,
                error_type="UnknownTool",
                error=f"no tool named {name!r}; available: {', '.join(self.names())}",
                step_id=step_id,
            )

        try:
            validated = spec.arguments_model.model_validate(arguments or {})
        except ValidationError as exc:
            return ToolResult(
                tool=name, status=ToolStatus.REJECTED,
                error_type="InvalidArguments",
                error=_readable(exc), step_id=step_id,
            )

        if self.enforce_policy:
            rejection = self._policy_rejection(spec, approved=approved)
            if rejection is not None:
                return ToolResult(tool=name, step_id=step_id, **rejection)

        started = time.perf_counter()
        try:
            payload = spec.run(**validated.model_dump())
        except _ToolFailure as exc:
            return self._from_failure(exc.result, spec, step_id, started)
        except Exception as exc:  # noqa: BLE001 - a tool failing is a result, not a crash
            elapsed = time.perf_counter() - started
            return ToolResult(
                tool=name, status=ToolStatus.ERROR,
                error_type=type(exc).__name__, error=str(exc),
                duration_ms=int(elapsed * 1000),
                step_id=step_id,
                diagnostics=[f"{type(exc).__name__}: {exc}"],
            )

        result = self._normalise(payload, spec, step_id, started)
        if self.enforce_policy:
            result = self._apply_output_limit(result, spec)
            result = self._note_timeout(result, spec, time.perf_counter() - started)
            self._record_facts(spec, result)
        return result

    # -- policy -------------------------------------------------------------

    def _policy_rejection(self, spec: ToolSpec,
                          *, approved: bool) -> dict | None:
        missing = [f for f in spec.requires if f not in self.facts]
        if missing:
            return self._reject(
                PolicyError.PRECONDITION_NOT_MET,
                f"tool {spec.name!r} requires {', '.join(missing)}",
                [f"PRECONDITION_NOT_MET: missing {', '.join(missing)}"])
        access = set(spec.access)
        if ("network" in access and not self.policy.allow_network
                and not spec.network_optional):
            return self._reject(
                PolicyError.NETWORK_NOT_ALLOWED,
                f"tool {spec.name!r} needs network access and the policy "
                f"forbids it",
                ["NETWORK_NOT_ALLOWED: the tool was not executed"])
        if ("model" in access and not self.policy.allow_model_calls
                and not spec.model_optional):
            return self._reject(
                PolicyError.MODEL_NOT_ALLOWED,
                f"tool {spec.name!r} needs a model call and the policy "
                f"forbids it",
                ["MODEL_NOT_ALLOWED: the tool was not executed"])
        if (spec.effects == Effects.PERSISTENT.value
                and self.policy.require_approval_for_persistent
                and not approved):
            return self._reject(
                PolicyError.APPROVAL_REQUIRED,
                f"tool {spec.name!r} changes persistent data and requires "
                f"user approval",
                ["APPROVAL_REQUIRED: the tool was not executed"])
        return None

    @staticmethod
    def _reject(code: PolicyError, message: str,
                diagnostics: list[str]) -> dict:
        return {
            "status": ToolStatus.REJECTED,
            "error_type": code.value,
            "error": message,
            "diagnostics": diagnostics,
            "provenance": {"policy_rejection": code.value},
        }

    # -- normalization ------------------------------------------------------

    def _from_failure(self, failure: ToolResult, spec: ToolSpec,
                      step_id: str, started: float) -> ToolResult:
        elapsed = time.perf_counter() - started
        provenance = dict(failure.provenance or {})
        provenance["delegated_tool"] = failure.tool
        return ToolResult(
            tool=spec.name,
            status=failure.status,
            data=redact(failure.data),
            error_type=failure.error_type,
            error=failure.error,
            duration_ms=int(elapsed * 1000),
            step_id=step_id,
            evidence_ids=list(failure.evidence_ids),
            diagnostics=list(failure.diagnostics),
            provenance=provenance,
        )

    def _normalise(self, payload: Any, spec: ToolSpec, step_id: str,
                   started: float) -> ToolResult:
        elapsed = time.perf_counter() - started
        if isinstance(payload, ToolResult):
            provenance = dict(payload.provenance or {})
            provenance.setdefault("declared_access", list(spec.access))
            provenance.setdefault("declared_effects", spec.effects)
            return payload.model_copy(update={
                "tool": spec.name,
                "step_id": step_id,
                "duration_ms": int(elapsed * 1000),
                "data": redact(payload.data),
                "provenance": provenance,
            })
        data = redact(payload)
        evidence = []
        if isinstance(data, dict):
            evidence = [e for e in (data.get("evidence_ids") or [])
                        if isinstance(e, str)]
        return ToolResult(
            tool=spec.name, status=ToolStatus.OK, data=data,
            duration_ms=int(elapsed * 1000), step_id=step_id,
            evidence_ids=evidence,
            provenance={"declared_access": list(spec.access),
                        "declared_effects": spec.effects},
        )

    def _apply_output_limit(self, result: ToolResult,
                            spec: ToolSpec) -> ToolResult:
        limit = spec.output_limit_chars
        if limit <= 0:
            return result
        bounded, truncated, original = _bound_output(result.data, limit)
        if not truncated:
            return result
        diagnostics = list(result.diagnostics) + [
            f"output truncated to {limit} chars (original {original})"]
        return result.model_copy(update={
            "data": bounded, "diagnostics": diagnostics})

    def _note_timeout(self, result: ToolResult, spec: ToolSpec,
                      elapsed: float) -> ToolResult:
        if spec.timeout_s <= 0 or elapsed <= spec.timeout_s:
            return result
        if spec.timeout_mode == "advisory":
            message = (
                f"timeout exceeded (advisory): {elapsed:.2f}s > "
                f"{spec.timeout_s:.2f}s; the operation was not interrupted")
        else:
            message = (
                f"timeout budget exceeded (native): {elapsed:.2f}s > "
                f"{spec.timeout_s:.2f}s; the backend did not interrupt "
                f"before returning")
        return result.model_copy(update={
            "diagnostics": list(result.diagnostics) + [message]})

    def _record_facts(self, spec: ToolSpec, result: ToolResult) -> None:
        if result.status is not ToolStatus.OK:
            return
        self.facts.add(*spec.sets_facts)
        if spec.fact_rule is not None:
            try:
                self.facts.add(*spec.fact_rule(result.data))
            except Exception:  # noqa: BLE001 - a fact rule is not a tool failure
                pass


def _bound_output(data: Any, limit: int) -> tuple[Any, bool, int]:
    """Shrink a result to `limit` characters without silently dropping keys.

    Lists are halved (and the removed count recorded in `<key>_truncated`),
    then long strings are shortened, then a preview is the last resort. The
    returned object always says `truncated: true` and keeps `original_chars`.
    """
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(data)
    original = len(text)
    if original <= limit:
        return data, False, original

    if isinstance(data, dict):
        out = dict(data)
        out["truncated"] = True
        out["original_chars"] = original

        def size() -> int:
            return len(json.dumps(out, ensure_ascii=False, default=str))

        for _ in range(64):
            if size() <= limit:
                return out, True, original
            lists = [(k, v) for k, v in out.items()
                     if isinstance(v, list) and len(v) > 1
                     and not k.endswith("_truncated")]
            if lists:
                key, value = max(lists, key=lambda kv: len(kv[1]))
                keep = max(1, len(value) // 2)
                out[key] = value[:keep]
                out[f"{key}_truncated"] = len(value) - keep
                continue
            strings = [(k, v) for k, v in out.items()
                       if isinstance(v, str) and len(v) > 200
                       and k not in ("preview",)]
            if strings:
                key, value = max(strings, key=lambda kv: len(kv[1]))
                out[key] = value[:200] + "…[truncated]"
                continue
            break
        if size() <= limit:
            return out, True, original

    return ({"truncated": True, "original_chars": original,
             "preview": text[:max(0, limit - 64)]}, True, original)


def _readable(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(x) for x in error.get("loc", ())) or "arguments"
        parts.append(f"{location}: {error.get('msg')}")
    return "; ".join(parts) or "arguments failed validation"


__all__ = [
    "Access", "Approval", "Effects", "ExecutionPolicy", "PolicyError",
    "SessionFacts", "ToolRegistry", "ToolSpec", "_ToolFailure",
]

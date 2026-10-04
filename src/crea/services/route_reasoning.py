"""Route reasoning service (CREA Stage 6.3).

One bounded call:

    RouteReasoningRequest
        -> RouteExplorerService (deterministic routes, 5.4)
        -> AIContextService (facts, 6.1)
        -> LLM (6.2 abstraction)
        -> grounding (route_id / step_index / reaction_template_id)
        -> RouteReasoningResult

The AI reasons over routes the explorer found; it never runs a loop, never
calls a tool, never searches literature and never changes the routes (§三/§五/
§四十七/§五十二). Facts come from the deterministic layer; the explanation is
the model's.
"""

from __future__ import annotations

from crea.core.errors import RouteReasoningError
from crea.core.ids import content_hash
from crea.core.models.ai_context import AIContextRequest
from crea.core.models.modification import GroundingStatus, ModelMetadata
from crea.core.models.route_reasoning import (
    RouteObservation,
    RouteReasoningRequest,
    RouteReasoningResult,
    RouteStepNote,
    RouteSummary,
)
from crea.core.json_parse import parse_json_object
from crea.services.route_reasoning_prompt import SYSTEM, build_user_message

#: At most one repair attempt on malformed output (§四十).
MAX_REPAIR_ATTEMPTS = 2

_LIMITATIONS = [
    "this is an explanation of the routes the deterministic explorer found, "
    "not a new search and not a prediction of experimental success",
    "a partial route is not complete and a blocked path is not a route",
    "no yield, condition or citation is produced that the data does not contain",
]


class RouteReasoningService:
    """Explain and compare the deterministic candidate routes."""

    def __init__(self, context_service, explorer_service, llm, *,
                 temperature: float | None = 0.0) -> None:
        self._context = context_service
        self._explorer = explorer_service
        self._llm = llm
        self._temperature = temperature

    def reason(self, request: RouteReasoningRequest) -> RouteReasoningResult:
        explored = self._explorer.explore(
            request.start_molecule, request.target_molecule, request.max_steps)
        routes = explored.paths[:request.max_routes]

        context = self._context.build(AIContextRequest(
            molecule_smiles=[request.start_molecule],
            query=request.query))

        payload = self._ask_model(context, routes, request)
        return self._assemble(context, explored, routes, request, payload)

    # -- model call ---------------------------------------------------------

    def _ask_model(self, context, routes, request: RouteReasoningRequest) -> dict:
        from crea.llm.base import LLMRequest

        user = build_user_message(context, routes, request.query,
                                  request.start_molecule, request.target_molecule)
        last_error = "the model did not return a usable JSON object"
        for _ in range(MAX_REPAIR_ATTEMPTS):
            try:
                response = self._llm.complete(LLMRequest(
                    system=SYSTEM, messages=[{"role": "user", "content": user}],
                    temperature=self._temperature))
            except Exception as exc:  # noqa: BLE001 - a dead model is typed
                raise self._provider_error(exc) from exc
            parsed = parse_json_object(response.text, required_key="observations")
            if parsed is not None:
                return parsed
        raise RouteReasoningError("AI_OUTPUT_INVALID", last_error)

    # -- assembly + grounding -----------------------------------------------

    def _assemble(self, context, explored, routes, request, payload
                  ) -> RouteReasoningResult:
        by_id = {r.route_id: r for r in routes}
        observations: list[RouteObservation] = []
        unsupported: list[str] = []
        warnings: list[str] = []

        for raw in payload.get("observations") or []:
            if not isinstance(raw, dict):
                continue
            route_id = str(raw.get("route_id") or "")
            route = by_id.get(route_id)
            if route is None:
                unsupported.append(route_id or "(no route_id)")
                warnings.append(
                    f"observation names route {route_id!r} that the explorer "
                    f"did not find")
                continue
            step_notes, note_reasons = self._ground_steps(raw, route)
            warnings.extend(note_reasons)
            status = GroundingStatus.GROUNDED if not note_reasons \
                else GroundingStatus.PARTIALLY_GROUNDED
            observations.append(RouteObservation(
                route_id=route_id, status=route.status.value,
                summary=str(raw.get("summary") or ""),
                step_notes=step_notes,
                gaps=[str(g) for g in (raw.get("gaps") or [])],
                grounding_status=status))

        warnings.extend(str(n) for n in (payload.get("notes") or []) if n)

        request_id = content_hash(
            f"{context.context_version}|{request.start_molecule}|"
            f"{request.target_molecule}|{request.query}|{len(observations)}", 16)
        return RouteReasoningResult(
            reasoning_id="rreason-" + request_id,
            context_version=context.context_version,
            start_smiles=explored.start_smiles,
            target_smiles=explored.target_smiles,
            route_summaries=[
                RouteSummary(route_id=r.route_id, status=r.status.value,
                             step_count=len(r.steps),
                             blocked_reason=r.blocked_reason)
                for r in routes],
            observations=observations,
            comparisons=[str(c) for c in (payload.get("comparisons") or [])],
            gaps=[str(g) for g in (payload.get("gaps") or [])],
            unsupported=unsupported,
            limitations=list(_LIMITATIONS) + list(explored.diagnostics[:1]),
            warnings=warnings,
            model_metadata=ModelMetadata(
                provider=getattr(self._llm, "name", ""),
                model=getattr(self._llm, "model", ""),
                temperature=self._temperature, request_id=request_id))

    @staticmethod
    def _ground_steps(raw: dict, route) -> tuple[list[RouteStepNote], list[str]]:
        """Keep only step notes that match a real step of the real route (§31)."""
        real = {(s.step_index, s.reaction_template_id) for s in route.steps}
        notes: list[RouteStepNote] = []
        reasons: list[str] = []
        for note in raw.get("step_notes") or []:
            if not isinstance(note, dict):
                continue
            try:
                index = int(note.get("step_index"))
            except (TypeError, ValueError):
                reasons.append("a step note had no integer step_index")
                continue
            tid = str(note.get("reaction_template_id") or "")
            if (index, tid) not in real:
                reasons.append(
                    f"step note ({index}, {tid!r}) is not a step of route "
                    f"{route.route_id!r}")
                continue
            notes.append(RouteStepNote(step_index=index, reaction_template_id=tid,
                                       note=str(note.get("note") or "")))
        return notes, reasons

    @staticmethod
    def _provider_error(exc: Exception) -> RouteReasoningError:
        name = type(exc).__name__
        if "RateLimit" in name:
            code = "AI_RATE_LIMITED"
        elif "Unavailable" in name or "Authentication" in name:
            code = "AI_PROVIDER_UNAVAILABLE"
        elif "Timeout" in name or "Upstream" in name:
            code = "AI_TIMEOUT"
        else:
            code = "AI_PROVIDER_UNAVAILABLE"
        return RouteReasoningError(code, f"{name}: {exc}")


__all__ = ["MAX_REPAIR_ATTEMPTS", "RouteReasoningService"]

"""Modification analysis service (CREA Stage 6.2).

The first real AI feature, and the boundary it holds is the whole point:

    ModificationRequest -> AIContext -> LLM -> Structured output
        -> Grounding validator -> ModificationAnalysis

Facts come only from the AI Context (6.1); the model interprets and proposes;
the grounding validator refuses anything the context cannot support. The
service never touches SQLite directly, never creates a reaction, and never
persists its result (§4/§12/§21/§22).
"""

from __future__ import annotations

import json

from crea.core.errors import ModificationAnalysisError
from crea.core.ids import content_hash
from crea.core.models.ai_context import AIContextRequest
from crea.core.models.modification import (
    ModificationAnalysis,
    ModificationAnalysisRequest,
    ModelMetadata,
)
from crea.services.modification_grounding import ModificationGroundingService
from crea.services.modification_prompt import SYSTEM, build_user_message

#: At most one repair attempt on malformed output (§19). Never infinite.
MAX_REPAIR_ATTEMPTS = 2

_LIMITATIONS = [
    "candidates are grounded in CREA reaction knowledge, not a prediction of "
    "experimental success",
    "a pattern match is not experimental success, and a curated reaction is not "
    "a literature proof",
    "no yield, condition or citation is produced that the context does not contain",
]


class ModificationAnalysisService:
    """Interpret a requested modification into grounded CREA candidates."""

    def __init__(self, context_service, llm, *, temperature: float | None = 0.0,
                 grounding=None) -> None:
        self._context = context_service
        self._llm = llm
        self._temperature = temperature
        self._grounding = grounding or ModificationGroundingService()

    def analyze(self, request: ModificationAnalysisRequest) -> ModificationAnalysis:
        context = self._context.build(AIContextRequest(
            molecule_ids=[request.molecule_id] if request.molecule_id else [],
            molecule_smiles=[request.molecule_smiles] if request.molecule_smiles else [],
            query=request.query))

        payload = self._ask_model(context, request)
        analysis = self._assemble(context, request, payload)
        return analysis

    # -- model call ---------------------------------------------------------

    def _ask_model(self, context, request: ModificationAnalysisRequest) -> dict:
        from crea.llm.base import LLMRequest

        user = build_user_message(context, request.query, request.target_smiles)
        last_error = ""
        for attempt in range(MAX_REPAIR_ATTEMPTS):
            try:
                response = self._llm.complete(LLMRequest(
                    system=SYSTEM, messages=[{"role": "user", "content": user}],
                    temperature=self._temperature))
            except Exception as exc:  # noqa: BLE001 - a dead model is typed
                raise self._provider_error(exc) from exc
            parsed = self._parse(response.text)
            if parsed is not None:
                return parsed
            last_error = "the model did not return a usable JSON object"
        raise ModificationAnalysisError("AI_OUTPUT_INVALID", last_error)

    @staticmethod
    def _parse(text: str) -> dict | None:
        if not text:
            return None
        stripped = text.strip()
        if stripped.startswith("```"):
            # tolerate a fenced block, but nothing more
            stripped = stripped.strip("`")
            _, _, stripped = stripped.partition("\n")
            stripped = stripped.rsplit("```", 1)[0]
        try:
            payload = json.loads(stripped)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or "candidates" not in payload:
            return None
        if not isinstance(payload["candidates"], list):
            return None
        return payload

    # -- assembly -----------------------------------------------------------

    def _assemble(self, context, request: ModificationAnalysisRequest,
                  payload: dict) -> ModificationAnalysis:
        candidates = []
        unsupported: list[str] = []
        warnings: list[str] = []
        for index, raw in enumerate(payload.get("candidates") or []):
            if not isinstance(raw, dict):
                continue
            if len(candidates) >= request.max_candidates:
                break
            candidate, reasons = self._grounding.ground(raw, context, index)
            if candidate is None:
                unsupported.append(str(raw.get("description") or "unsupported candidate"))
                warnings.extend(reasons)
                continue
            # a partially grounded candidate keeps its reasons visible
            warnings.extend(reasons)
            candidates.append(candidate)

        warnings.extend(str(n) for n in (payload.get("notes") or []) if n)
        molecule = context.molecules[0] if context.molecules else None

        request_id = content_hash(
            f"{context.context_version}|{request.query}|{request.molecule_smiles}"
            f"|{request.molecule_id}|{len(candidates)}", 16)
        return ModificationAnalysis(
            analysis_id="mmod-" + request_id,
            context_version=context.context_version,
            input_molecule=(molecule.smiles if molecule is not None
                            else request.molecule_smiles),
            input_molecule_id=(molecule.entity_id if molecule is not None
                               else request.molecule_id),
            user_query=request.query,
            candidates=candidates,
            unsupported=unsupported,
            limitations=list(_LIMITATIONS) + list(context.limitations[:1]),
            warnings=warnings,
            model_metadata=ModelMetadata(
                provider=getattr(self._llm, "name", ""),
                model=getattr(self._llm, "model", ""),
                temperature=self._temperature, request_id=request_id))

    @staticmethod
    def _provider_error(exc: Exception) -> ModificationAnalysisError:
        name = type(exc).__name__
        if "RateLimit" in name:
            code = "AI_RATE_LIMITED"
        elif "Unavailable" in name or "Authentication" in name:
            code = "AI_PROVIDER_UNAVAILABLE"
        elif "Timeout" in name or "Upstream" in name:
            code = "AI_TIMEOUT"
        else:
            code = "AI_PROVIDER_UNAVAILABLE"
        return ModificationAnalysisError(code, f"{name}: {exc}")


__all__ = ["MAX_REPAIR_ATTEMPTS", "ModificationAnalysisService"]

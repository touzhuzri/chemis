"""Route reasoning (CREA Stage 6.3).

The AI explains, compares and finds the gaps in routes that the **deterministic**
Route Explorer found. It never invents a route, a step or an intermediate
(§三/§三十二): every observation is anchored to a real `route_id`, and every
step note to a real (step_index, reaction_template_id) of that route.

The result is transient (§四十八) and keeps facts apart from interpretation:
route status / step ids come from the explorer, the summary and gaps are the
model's words.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from crea.core.models.modification import GroundingStatus, ModelMetadata


class RouteSummary(BaseModel):
    """A deterministic route, as the explorer produced it."""

    route_id: str
    status: str
    step_count: int
    blocked_reason: str = ""


class RouteStepNote(BaseModel):
    step_index: int
    reaction_template_id: str
    note: str = ""


class RouteObservation(BaseModel):
    """The AI's reading of ONE real route (§十七/§十八)."""

    route_id: str
    status: str = ""
    summary: str = ""
    step_notes: list[RouteStepNote] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    grounding_status: GroundingStatus = GroundingStatus.UNSUPPORTED


class RouteReasoningRequest(BaseModel):
    start_molecule: str = ""
    target_molecule: str = ""
    max_steps: int = 3
    query: str = ""
    max_routes: int = 5


class RouteReasoningResult(BaseModel):
    reasoning_id: str
    context_version: str = ""
    start_smiles: str = ""
    target_smiles: str = ""
    route_summaries: list[RouteSummary] = Field(default_factory=list)
    observations: list[RouteObservation] = Field(default_factory=list)
    comparisons: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    #: Observations the routes could not support, kept as warnings not facts.
    unsupported: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    model_metadata: ModelMetadata = Field(default_factory=ModelMetadata)


__all__ = [
    "RouteObservation",
    "RouteReasoningRequest",
    "RouteReasoningResult",
    "RouteStepNote",
    "RouteSummary",
]

"""Route Explorer results (CREA Stage 5.4).

Transient DTOs for a **bounded, forward-only** route search over the known
reactions. A route is a sequence of steps whose every input and output
structure is deterministically known; when a step's product cannot be
determined, the path is `PARTIAL`, never a complete route (§7/§8).

No `route_confidence`, `success_probability` or `yield` field exists (§22/§23):
the explorer reports structure and evidence, not a prediction.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class RouteStatus(str, Enum):
    """How far a path got (§8)."""

    COMPLETE = "complete"   # every step's structures known, reached the target
    PARTIAL = "partial"     # a matched reaction, but the product is unknown
    BLOCKED = "blocked"     # no valid next transition


class ExploredRouteStep(BaseModel):
    step_index: int
    reaction_template_id: str
    reaction_name: str
    reaction_class: str = ""
    #: Chemical identity keys, so CCO and OCC are one molecule (§16/§18).
    input_molecule_id: str
    input_smiles: str
    output_molecule_id: str
    output_smiles: str
    match_level: str = ""
    evidence_count: int = 0
    evidence_summary: dict[str, int] = Field(default_factory=dict)


class ExploredRoute(BaseModel):
    route_id: str
    status: RouteStatus
    start_molecule_id: str
    start_smiles: str
    target_molecule_id: str
    target_smiles: str
    steps: list[ExploredRouteStep] = Field(default_factory=list)
    #: NO_NEXT_REACTION | PRODUCT_STRUCTURE_UNAVAILABLE | MAX_STEPS_REACHED.
    blocked_reason: str = ""
    blocked_reaction_id: str = ""
    #: Deterministic dedup key: template ids + molecule identity keys.
    signature: str = ""


class RouteExplorerResult(BaseModel):
    start_smiles: str = ""
    target_smiles: str = ""
    max_steps: int = 0
    paths: list[ExploredRoute] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.paths)

    def by_status(self, status: RouteStatus) -> list[ExploredRoute]:
        return [p for p in self.paths if p.status is status]


__all__ = [
    "ExploredRoute",
    "ExploredRouteStep",
    "RouteExplorerResult",
    "RouteStatus",
]

"""Reaction graph (CREA Stage 5.3).

A derived, transient view over existing local data:

    Molecule --SUBSTRATE_OF--> Reaction --SUPPORTED_BY--> Literature

It represents knowledge, it never generates it (§9): a `SUBSTRATE_OF` edge
means the molecule's structure matches the template's substrate pattern, never
that the reaction succeeds (§8). `PRODUCES` (Reaction -> Molecule) is created
**only** when a product structure can be determined deterministically; with no
product patterns encoded it is never created (§30).

Nodes are Molecule / Reaction / Literature (§6). Evidence is edge metadata
(`SUPPORTED_BY`), not a required node.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class GraphNodeType(str, Enum):
    MOLECULE = "molecule"
    REACTION = "reaction"
    LITERATURE = "literature"


class GraphEdgeType(str, Enum):
    SUBSTRATE_OF = "substrate_of"     # Molecule -> Reaction (structural match)
    SUPPORTED_BY = "supported_by"     # Reaction -> Literature (via evidence)
    SOURCED_FROM = "sourced_from"     # Evidence -> Paper
    PRODUCES = "produces"             # Reaction -> Molecule (deterministic only)


class GraphNode(BaseModel):
    id: str                       # "mol:<entity_id>" | "rxn:<template_id>" | "lit:<paper_id>"
    type: GraphNodeType
    label: str = ""
    metadata: dict = Field(default_factory=dict)


class GraphEdge(BaseModel):
    source: str
    target: str
    type: GraphEdgeType
    #: e.g. {"match_level": ..., "matched_atoms": ...} for SUBSTRATE_OF;
    #: {"evidence_id", "evidence_type", "support_scope", "status", "locator"}
    #: for SUPPORTED_BY. Evidence provenance is carried here, never dropped.
    metadata: dict = Field(default_factory=dict)


class ReactionGraph(BaseModel):
    root: str = ""
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    depth: int = 1
    diagnostics: list[str] = Field(default_factory=list)


__all__ = [
    "GraphEdge",
    "GraphEdgeType",
    "GraphNode",
    "GraphNodeType",
    "ReactionGraph",
]

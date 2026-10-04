"""The Molecule Explorer's inspection result (CREA Phase 1).

One structured answer to "what is this molecule". Everything here is computed
deterministically by the chemistry layer -- identity, properties, functional
groups, scaffold, fragments, and the curated reactions whose substrate pattern
matches the structure. No LLM, no ranking, no route. The result reuses the
existing `Molecule`/`Descriptors` vocabulary and the new `FunctionalGroup` /
`ReactionMatch` models; there is no second molecule model.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from crea.core.models.functional_group import FunctionalGroup
from crea.core.models.reaction_template import ReactionMatch


class MoleculeInspection(BaseModel):
    """The whole deterministic picture of one molecule (§二)."""

    smiles: str
    valid: bool = True
    error: str = ""
    identity: dict = Field(default_factory=dict)
    structure: dict = Field(default_factory=dict)
    properties: dict = Field(default_factory=dict)
    functional_groups: list[FunctionalGroup] = Field(default_factory=list)
    scaffold: dict = Field(default_factory=dict)
    fragments: list[dict] = Field(default_factory=list)
    reactions: list[ReactionMatch] = Field(default_factory=list)
    provenance: dict = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list)


__all__ = ["MoleculeInspection"]

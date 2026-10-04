"""A detected functional group (CREA Phase 1 Molecule Explorer).

A functional group is a named structural feature -- a SMARTS pattern that
matches somewhere in the molecule -- with the atom indices it matched. It is
deterministic, rule-derived data, never a model's guess: the names come from
RDKit's functional-group hierarchy, the matches from substructure search.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FunctionalGroup(BaseModel):
    """One functional group present in a molecule."""

    name: str
    smarts: str
    count: int = 0
    #: One atom-index tuple per match (indices into the normalized molecule's
    #: atom vector). Kept so a reader can point at *where* the group sits.
    atom_indices: list[list[int]] = Field(default_factory=list)


__all__ = ["FunctionalGroup"]

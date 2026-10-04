"""Molecule-library search results (CREA Stage 2.2).

A library hit is an existing `ChemicalEntity` (the molecule record) plus why
it matched -- a similarity score (a screening number, never identity) or a
substructure note. The entity is the record; this model adds the match
metadata, not a second molecule model.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from crea.core.models.entity import ChemicalEntity


class LibraryHit(BaseModel):
    entity: ChemicalEntity
    score: float | None = None
    note: str = ""


class LibraryAddResult(BaseModel):
    """The outcome of adding one molecule: the record and whether it was new."""

    entity: ChemicalEntity
    is_new: bool = True


__all__ = ["LibraryAddResult", "LibraryHit"]

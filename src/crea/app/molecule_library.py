"""Molecule Library use cases (CREA Stage 2.2).

The workbench's molecule-library side. Each call opens a short-lived
`KnowledgeBase` and delegates to the `MoleculeLibrary` service -- this layer
orchestrates and returns domain objects; it computes no chemistry and runs no
SQL of its own (§8). Errors surface as the service's typed `MoleculeLibraryError`.
"""

from __future__ import annotations

from pathlib import Path


class MoleculeLibraryApplication:
    """Add, look up, deduplicate, search and remove molecule records."""

    def __init__(self, db_path) -> None:
        self._db_path = Path(db_path)

    def _with(self, fn):
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.molecule_library import MoleculeLibrary

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        try:
            return fn(MoleculeLibrary(kb))
        finally:
            kb.close()

    # -- records ------------------------------------------------------------

    def add(self, smiles: str, *, source_type: str = "USER_PROVIDED",
            source_id: str = ""):
        return self._with(lambda lib: lib.add(
            smiles, source_type=source_type, source_id=source_id))

    def get(self, record_id: str):
        return self._with(lambda lib: lib.get(record_id))

    def find_by_inchikey(self, inchikey: str):
        return self._with(lambda lib: lib.find_by_inchikey(inchikey))

    def list(self):
        return self._with(lambda lib: lib.list())

    def count(self) -> int:
        return self._with(lambda lib: lib.count())

    def delete(self, record_id: str) -> bool:
        return self._with(lambda lib: lib.delete(record_id))

    # -- search -------------------------------------------------------------

    def search_similar(self, smiles: str, *, limit: int = 10):
        return self._with(lambda lib: lib.search_similar(smiles, limit=limit))

    def search_substructure(self, smiles: str):
        return self._with(lambda lib: lib.search_substructure(smiles))


__all__ = ["MoleculeLibraryApplication"]

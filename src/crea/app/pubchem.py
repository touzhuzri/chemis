"""PubChem exact-lookup use cases (CREA Stage 2.3).

The workbench's external-source side. The provider is injected as a factory
built by the composition root -- this layer names no transport and no HTTP
(§15). Offline mode injects a factory that yields `None`, which reports
UNAVAILABLE rather than crashing the app (§22). Lookup and import stay
separate (§9).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable


class PubChemApplication:
    """Look up a molecule in PubChem, and import a verified result."""

    def __init__(self, db_path, provider_factory: Callable) -> None:
        self._db_path = Path(db_path)
        self._provider_factory = provider_factory

    def _provider(self):
        return self._provider_factory()

    def lookup(self, smiles: str):
        from crea.services.pubchem_service import PubChemService

        return PubChemService(provider=self._provider()).lookup(smiles)

    def import_by_smiles(self, smiles: str):
        """Look up, verify and import in one explicit action (§10)."""
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.pubchem_service import PubChemService

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        try:
            service = PubChemService(provider=self._provider(), kb=kb)
            result = service.lookup(smiles)
            entity, is_new = service.import_result(result)
            return entity, is_new
        finally:
            kb.close()


__all__ = ["PubChemApplication"]

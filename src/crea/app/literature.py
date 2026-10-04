"""Literature use cases (CREA Stage 4.2).

Search and DOI lookup across the injected literature providers, plus reading a
stored record. The providers are injected as a factory built by the
composition root -- this layer names no transport and no HTTP. Offline, the
factory yields no providers and every call reports the providers unavailable
rather than crashing the app.

This layer does no chemistry, no ranking and no permission decision: it
orchestrates `LiteratureService` (federation + DOI merge) and
`LiteratureRecordService` (the local records).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable


class LiteratureApplication:
    """Search literature, resolve a DOI, and read stored records."""

    def __init__(self, db_path, provider_factory: Callable, *,
                 access_provider_factory: Callable | None = None,
                 retry_policy=None) -> None:
        self._db_path = Path(db_path)
        self._provider_factory = provider_factory
        self._access_provider_factory = access_provider_factory
        self._retry_policy = retry_policy

    def _service(self):
        from crea.services.literature import LiteratureService

        providers = self._provider_factory() or {}
        access = (self._access_provider_factory()
                  if self._access_provider_factory else None)
        return LiteratureService(providers, access_provider=access,
                                 retry_policy=self._retry_policy)

    def available(self) -> bool:
        return bool(self._provider_factory() or {})

    def search(self, query: str, *, limit: int = 20):
        from crea.core.models.service import LiteratureSearchRequest

        return self._service().search(
            LiteratureSearchRequest(query=query, limit=limit))

    def lookup_doi(self, doi: str):
        return self._service().lookup_by_doi(doi)

    def access(self, doi: str):
        """The access decision for one DOI (Stage 4.3).

        Fails closed: metadata from Crossref/OpenAlex is aggregated, Unpaywall
        is consulted when configured, and the decision is the frozen policy's.
        Returns the record's `AccessAssessment` (with reasons/evidence) plus any
        reconciliation, or an empty dict when no provider has the DOI.
        """
        from crea.core.models.service import DoiLookupRequest

        result = self._service().lookup(
            DoiLookupRequest(doi=doi, include_access=True))
        paper = result.paper
        return {
            "doi": doi,
            "found": result.found,
            "assessment": (paper.access.model_dump(mode="json")
                           if paper is not None else None),
            "reconciliations": [r.model_dump(mode="json") for r in result.access],
            "provider_runs": [r.model_dump(mode="json") for r in result.provider_runs],
            "warnings": list(result.warnings),
        }

    # -- local records ------------------------------------------------------

    def get(self, literature_id: str):
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.literature_record import LiteratureRecordService

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        try:
            return LiteratureRecordService(kb).get(literature_id)
        finally:
            kb.close()

    def list(self):
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.literature_record import LiteratureRecordService

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        try:
            return LiteratureRecordService(kb).list()
        finally:
            kb.close()


__all__ = ["LiteratureApplication"]

"""Literature record service (CREA Stage 4.1).

Creates and reads literature records -- the existing `Paper` model, which is
CREA's literature record (identity, bibliographic metadata, identifiers,
sources, access, provenance; §2/§3). This service adds the create path with
the DOI dedup policy (§9): one record per normalized DOI, and a work with no
DOI is still a record (§8). No provider is contacted here; a record is built
from metadata a caller already holds.
"""

from __future__ import annotations

from crea.core.ids import normalize_doi


class LiteratureRecordService:
    """Create/dedup/read literature records over a knowledge base."""

    def __init__(self, kb) -> None:
        self._kb = kb

    # -- create -------------------------------------------------------------

    def create(self, paper):
        """Store a literature record, deduplicating by normalized DOI.

        Returns `(record, is_new)`. If a record with the same normalized DOI
        already exists, the existing one is returned with `is_new=False` and
        no duplicate is created (§9). Two records that both lack a DOI are two
        records -- no title+year fingerprint is invented to force them together.
        """
        doi = normalize_doi(paper.identifiers.doi) if paper.identifiers else None
        if doi:
            existing = self._kb.find_paper_by_doi(doi)
            if existing is not None:
                return existing, False
        return self._kb.save_paper(paper), True

    # -- read ---------------------------------------------------------------

    def get(self, literature_id: str):
        paper = self._kb.store.get_paper(literature_id)
        if paper is None:
            from crea.repositories.errors import NotFound

            raise NotFound(f"no literature record {literature_id!r}")
        return paper

    def find_by_doi(self, doi: str):
        return self._kb.find_paper_by_doi(doi)

    def list(self):
        return self._kb.list_papers()

    def count(self) -> int:
        return len(self._kb.list_papers())


__all__ = ["LiteratureRecordService"]

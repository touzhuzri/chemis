"""The literature record (Stage 4.1): identity, metadata, access, dedup.

The record is the existing `Paper` model. Tests pin DOI normalization and
dedup, that a work with no DOI is still a record, that metadata / external
identifiers / OA status / permission round-trip through storage, and that the
internal id is never the DOI.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from crea.core.ids import normalize_doi
from crea.core.models.literature import (
    AbstractRecord,
    AbstractSource,
    AccessAssessment,
    Author,
    FulltextPermission,
    Identifiers,
    LiteratureType,
    OAStatus,
    Paper,
    UpstreamFacts,
)
from crea.services.knowledge_base import KnowledgeBase
from crea.services.literature_record import LiteratureRecordService


def _service(tmp_path) -> LiteratureRecordService:
    kb = KnowledgeBase(pathlib.Path(tmp_path) / "lit.db")
    kb.initialize()
    return LiteratureRecordService(kb)


def _paper(paper_id="lit-1", doi=None, **kwargs) -> Paper:
    return Paper(
        paper_id=paper_id, title=kwargs.pop("title", "A work"),
        authors=[Author(family="Smith", given="Alex")], year=2021,
        identifiers=Identifiers(doi=doi, crossref_id=kwargs.pop("crossref_id", None)),
        retrieved_at="2026-01-01", **kwargs)


# -- DOI normalization (§六/§七) -------------------------------------------


def test_doi_normalization_forms():
    for raw in ("10.1000/xyz", "doi:10.1000/xyz",
                "https://doi.org/10.1000/xyz"):
        assert normalize_doi(raw) == "10.1000/xyz"


def test_doi_normalization_handles_punctuation_and_case():
    assert normalize_doi("https://doi.org/10.1000/ABC.") == "10.1000/abc"
    assert normalize_doi("  doi:10.1000/XyZ  ") == "10.1000/xyz"
    assert normalize_doi("") is None
    assert normalize_doi(None) is None


# -- DOI duplicate (§九) ---------------------------------------------------


def test_same_doi_notation_does_not_create_two_records(tmp_path):
    service = _service(tmp_path)
    first, is_new = service.create(_paper("lit-1", doi="10.1000/xyz"))
    second, is_new2 = service.create(
        _paper("lit-2", doi="https://doi.org/10.1000/XYZ", title="Other"))
    assert is_new is True and is_new2 is False
    assert second.paper_id == first.paper_id
    assert service.count() == 1
    # the stored DOI is the canonical form
    assert service.find_by_doi("doi:10.1000/xyz").paper_id == "lit-1"


# -- no DOI still a record (§八) -------------------------------------------


def test_record_without_a_doi_is_allowed_and_not_forced_together(tmp_path):
    service = _service(tmp_path)
    _, n1 = service.create(_paper("lit-a", doi=None, title="Untitled A"))
    _, n2 = service.create(_paper("lit-b", doi=None, title="Untitled B"))
    assert n1 is True and n2 is True
    assert service.count() == 2


# -- metadata / identifiers / access round-trip ----------------------------


def test_metadata_identifiers_and_access_persist(tmp_path):
    service = _service(tmp_path)
    paper = _paper("lit-1", doi="10.1000/xyz", crossref_id="10.1000/xyz",
                   upstream=UpstreamFacts(type="journal-article", language="en"))
    paper.abstracts = [AbstractRecord(text="An abstract",
                                      source=AbstractSource.PUBLISHER,
                                      retrieved_at="2026-01-01")]
    paper.access = AccessAssessment(oa_status=OAStatus.GOLD)
    service.create(paper)

    loaded = service.get("lit-1")
    assert loaded.title == "A work"
    assert loaded.authors[0].family == "Smith"
    assert loaded.year == 2021
    assert loaded.identifiers.crossref_id == "10.1000/xyz"
    assert loaded.access.oa_status is OAStatus.GOLD
    assert loaded.abstract.text == "An abstract"
    assert loaded.upstream.language == "en"


def test_abstract_can_be_empty(tmp_path):
    service = _service(tmp_path)
    service.create(_paper("lit-1", doi="10.1000/xyz"))
    assert service.get("lit-1").abstract is None


def test_fulltext_permission_defaults_to_unknown(tmp_path):
    service = _service(tmp_path)
    service.create(_paper("lit-1", doi="10.1000/xyz"))
    # access URL ≠ permission: a record with no access assessment is UNKNOWN
    assert service.get("lit-1").access.permission is FulltextPermission.UNKNOWN


def test_literature_type_is_a_typed_view(tmp_path):
    service = _service(tmp_path)
    paper = _paper("lit-1", doi="10.1000/xyz",
                   upstream=UpstreamFacts(type="posted-content"))
    assert paper.literature_type is LiteratureType.PREPRINT
    assert _paper("x", upstream=UpstreamFacts(type=None)).literature_type is (
        LiteratureType.UNKNOWN)
    assert _paper("y", upstream=UpstreamFacts(type="weird")).literature_type is (
        LiteratureType.OTHER)


# -- identity separation (§五) ---------------------------------------------


def test_internal_id_is_not_the_doi(tmp_path):
    service = _service(tmp_path)
    paper, _ = service.create(_paper("lit-1", doi="10.1000/xyz"))
    assert paper.paper_id == "lit-1"
    assert paper.doi != paper.paper_id
    assert paper.normalized_doi == "10.1000/xyz"


# -- serialization / persistence -------------------------------------------


def test_serialization_round_trip(tmp_path):
    service = _service(tmp_path)
    paper, _ = service.create(_paper("lit-1", doi="10.1000/xyz"))
    payload = paper.model_dump(mode="json")
    assert payload["identifiers"]["doi"] == "10.1000/xyz"
    assert Paper.model_validate(payload).paper_id == "lit-1"


def test_records_persist_across_reopen(tmp_path):
    db = pathlib.Path(tmp_path) / "lit.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    LiteratureRecordService(kb).create(_paper("lit-1", doi="10.1000/xyz"))
    kb.close()

    kb2 = KnowledgeBase(db)
    kb2.initialize()
    assert LiteratureRecordService(kb2).count() == 1
    kb2.close()


def test_missing_record_raises(tmp_path):
    from crea.repositories.errors import NotFound

    with pytest.raises(NotFound):
        _service(tmp_path).get("no-such-id")

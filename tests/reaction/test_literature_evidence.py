"""Literature <-> Reaction Evidence linking (Stage 4.4).

ReactionTemplate -> ReactionEvidence -> LiteratureRecord, and the reverse.
Linking is local only: a DOI is normalized and resolved against the local
literature store, never a provider. Dedup is by (template, source, type,
scope); the locator is not a key. Curated evidence is untouched, permission
and provenance stay independent, and no raw quote is ever stored.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from crea.core.errors import ReactionEvidenceError
from crea.core.models.literature import Identifiers, Paper
from crea.core.models.reaction_evidence import (
    EvidenceLocator,
    EvidenceSourceType,
    ReactionEvidenceType,
)
from crea.services.knowledge_base import KnowledgeBase
from crea.services.literature_record import LiteratureRecordService
from crea.services.reaction_evidence import ReactionEvidenceService


@pytest.fixture()
def env(tmp_path):
    kb = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    kb.initialize()
    liter = LiteratureRecordService(kb)
    liter.create(Paper(paper_id="paper-A", title="Oxidation study",
                       identifiers=Identifiers(doi="10.1000/abc"),
                       retrieved_at="2026-01-01"))
    liter.create(Paper(paper_id="paper-B", title="Reduction study",
                       identifiers=Identifiers(doi="10.1000/def"),
                       retrieved_at="2026-01-01"))
    yield kb, ReactionEvidenceService(kb=kb)
    kb.close()


def _link(svc, template_id, **kwargs):
    kwargs.setdefault("evidence_type", ReactionEvidenceType.TRANSFORMATION_SUPPORT)
    kwargs.setdefault("support_scope", "primary alcohol -> aldehyde")
    return svc.link_literature(template_id, **kwargs)


# -- 1. normal link ----------------------------------------------------------


def test_link_a_local_paper_to_a_template(env):
    _kb, svc = env
    evidence, is_new = _link(svc, "primary-alcohol-oxidation", paper_id="paper-A")
    assert is_new is True
    assert evidence.source.source_type is EvidenceSourceType.LITERATURE
    assert evidence.source.source_id == "paper-A"
    assert evidence.reaction_template_id == "primary-alcohol-oxidation"


def test_link_by_doi_resolves_the_local_record(env):
    _kb, svc = env
    evidence, is_new = _link(svc, "primary-alcohol-oxidation",
                             doi="https://doi.org/10.1000/ABC")
    assert is_new is True
    assert evidence.source.source_id == "paper-A"


# -- 2. reverse query --------------------------------------------------------


def test_reverse_query_finds_the_evidence(env):
    _kb, svc = env
    _link(svc, "primary-alcohol-oxidation", paper_id="paper-A")
    related = svc.evidence_for_literature("paper-A")
    assert [e.reaction_template_id for e in related] == ["primary-alcohol-oxidation"]
    assert svc.evidence_for_literature("paper-B") == []


# -- 3. DOI lookup: missing / invalid ---------------------------------------


def test_unknown_doi_is_literature_not_found(env):
    _kb, svc = env
    with pytest.raises(ReactionEvidenceError) as exc:
        _link(svc, "primary-alcohol-oxidation", doi="10.9999/notstored")
    assert exc.value.code == "LITERATURE_NOT_FOUND"


def test_invalid_doi_is_a_structured_error(env):
    _kb, svc = env
    with pytest.raises(ReactionEvidenceError) as exc:
        _link(svc, "primary-alcohol-oxidation", doi="not-a-doi")
    assert exc.value.code == "INVALID_DOI"


def test_unknown_template_is_a_structured_error(env):
    _kb, svc = env
    with pytest.raises(ReactionEvidenceError) as exc:
        _link(svc, "no-such-template", paper_id="paper-A")
    assert exc.value.code == "TEMPLATE_NOT_FOUND"


# -- 4. dedup ----------------------------------------------------------------


def test_same_evidence_is_not_duplicated(env):
    _kb, svc = env
    first, n1 = _link(svc, "primary-alcohol-oxidation", paper_id="paper-A")
    # a different locator is the same evidence -- the locator is not a key (§10)
    second, n2 = _link(svc, "primary-alcohol-oxidation", paper_id="paper-A",
                       locator=EvidenceLocator(figure="Figure 3"))
    assert n1 is True and n2 is False
    assert second.evidence_id == first.evidence_id
    assert len(svc.evidence_for_literature("paper-A")) == 1


def test_a_different_scope_is_a_different_evidence(env):
    _kb, svc = env
    _link(svc, "primary-alcohol-oxidation", paper_id="paper-A")
    _link(svc, "primary-alcohol-oxidation", paper_id="paper-A",
          support_scope="secondary alcohol -> ketone")
    assert len(svc.evidence_for_literature("paper-A")) == 2


# -- 5. many-to-many ---------------------------------------------------------


def test_one_paper_supports_multiple_templates(env):
    _kb, svc = env
    _link(svc, "primary-alcohol-oxidation", paper_id="paper-A")
    _link(svc, "aldehyde-oxidation", paper_id="paper-A")
    templates = {e.reaction_template_id for e in svc.evidence_for_literature("paper-A")}
    assert templates == {"primary-alcohol-oxidation", "aldehyde-oxidation"}


def test_one_template_is_supported_by_multiple_papers(env):
    _kb, svc = env
    _link(svc, "primary-alcohol-oxidation", paper_id="paper-A")
    _link(svc, "primary-alcohol-oxidation", paper_id="paper-B")
    papers = {e.source.source_id
              for e in svc.list_evidence("primary-alcohol-oxidation")
              if e.source.source_type is EvidenceSourceType.LITERATURE}
    assert papers == {"paper-A", "paper-B"}


# -- 6. curated evidence is not disturbed -----------------------------------


def test_curated_evidence_survives_literature_linking(env):
    _kb, svc = env
    # no stored evidence yet -> the derived curated self-evidence shows
    assert len(svc.list_evidence("alkene-hydrogenation")) == 1
    _link(svc, "primary-alcohol-oxidation", paper_id="paper-A")
    # an unrelated template still returns its curated self-evidence
    curated = svc.list_evidence("alkene-hydrogenation")[0]
    assert curated.source.source_type is EvidenceSourceType.CURATED


# -- 7. locator optional + serializable -------------------------------------


def test_locator_is_optional_and_serializable(env):
    _kb, svc = env
    no_locator, _ = _link(svc, "primary-alcohol-oxidation", paper_id="paper-A")
    assert no_locator.source.locator.page is None
    with_locator, _ = _link(svc, "aldehyde-oxidation", paper_id="paper-B",
                            locator=EvidenceLocator(page=5, scheme="Scheme 2",
                                                    section="Results"))
    payload = with_locator.model_dump(mode="json")
    assert payload["source"]["locator"] == {
        "page": 5, "section": "Results", "figure": None,
        "table": None, "scheme": "Scheme 2", "supporting_information": None}


# -- 8. no raw quote, no provider coupling ----------------------------------


def test_no_raw_quote_field_exists_on_evidence():
    from crea.core.models.reaction_evidence import ReactionEvidence

    fields = set(ReactionEvidence.model_fields) | set(
        ReactionEvidence.model_fields["source"].annotation.model_fields)
    for banned in ("raw_quote", "full_quote", "extracted_text", "pdf_text",
                   "ocr_text"):
        assert banned not in fields


def test_evidence_provenance_is_independent_of_the_paper(env):
    _kb, svc = env
    evidence, _ = _link(svc, "primary-alcohol-oxidation", paper_id="paper-A")
    # the linker records its own action; it never copies the Paper's provenance
    assert evidence.provenance is not None
    assert evidence.provenance.tool == "reaction-evidence-linker"


# -- 9. API -----------------------------------------------------------------


def test_literature_evidence_api(tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        # store a local paper via the molecule library's knowledge base path
        from crea.services.knowledge_base import KnowledgeBase
        from crea.services.literature_record import LiteratureRecordService

        kb = KnowledgeBase(tmp_path / "crea.db")
        kb.initialize()
        LiteratureRecordService(kb).create(
            Paper(paper_id="paper-A", title="Oxidation study",
                  identifiers=Identifiers(doi="10.1000/abc"),
                  retrieved_at="2026-01-01"))
        kb.close()

        linked = c.post(
            "/api/reaction-templates/primary-alcohol-oxidation/evidence/literature",
            json={"doi": "10.1000/abc",
                  "evidence_type": "transformation_support",
                  "support_scope": "primary alcohol -> aldehyde",
                  "locator": {"scheme": "Scheme 2"}})
        assert linked.status_code == 200
        assert linked.json()["is_new"] is True

        reverse = c.get("/api/literature/paper-A/reaction-evidence").json()
        assert reverse["count"] == 1
        assert reverse["evidence"][0]["source"]["source_type"] == "literature"

        missing = c.post(
            "/api/reaction-templates/primary-alcohol-oxidation/evidence/literature",
            json={"doi": "10.9999/nope", "support_scope": "x"})
        assert missing.status_code == 404

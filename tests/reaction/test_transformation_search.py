"""Transformation search (Stage 5.1).

Substrate -> candidate ReactionTemplates, deterministically and locally. A
template with no product pattern can only reach substrate_match; a full
transformation_match needs both patterns. No AI, no network, no literature
lookup; evidence orders results but never changes a match level.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from crea.chem.normalize import NormalizationError
from crea.core.models.literature import Identifiers, Paper
from crea.core.models.reaction_evidence import ReactionEvidenceType
from crea.core.models.transformation_search import TransformationMatchLevel
from crea.services.knowledge_base import KnowledgeBase
from crea.services.literature_record import LiteratureRecordService
from crea.services.reaction_evidence import ReactionEvidenceService
from crea.services.reaction_library import SEED_REACTION_TEMPLATES
from crea.services.transformation_search import TransformationSearchService


def _with_product_template():
    """A copy of a seed template carrying a product pattern.

    The shipped library encodes no product SMARTS (Stage 3.1), so the full
    transformation_match path is exercised with a template that has one -- not
    by fabricating patterns into the library.
    """
    template = next(t for t in SEED_REACTION_TEMPLATES
                    if t.template_id == "alkene-hydrogenation").model_copy(deep=True)
    template.template_id = "demo-alkene-to-alkane"
    template.product_patterns = ["[CX4]"]
    return template


# -- 1. substrate-only search -----------------------------------------------


def test_substrate_search_returns_candidates():
    result = TransformationSearchService().search("CCO")
    names = {c.reaction_template.name for c in result.candidates}
    assert "Primary Alcohol Oxidation" in names
    assert all(c.match_level is TransformationMatchLevel.SUBSTRATE_MATCH
               for c in result.candidates)


# -- 2. substrate + target: honest substrate_match without a product pattern -


def test_substrate_and_target_without_product_pattern_is_substrate_match():
    result = TransformationSearchService().search("CCO", "CCCl")
    assert result.candidates
    # no shipped template has a product pattern, so nothing can be confirmed
    assert all(c.match_level is TransformationMatchLevel.SUBSTRATE_MATCH
               for c in result.candidates)
    assert any("substrate pattern only" in n for c in result.candidates
               for n in c.notes)


def test_substrate_search_is_broader_than_substrate_plus_target():
    service = TransformationSearchService(
        templates=[*SEED_REACTION_TEMPLATES, _with_product_template()])
    broad = {c.reaction_template.template_id for c in service.search("C=C").candidates}
    narrow = {c.reaction_template.template_id
              for c in service.search("C=C", "CC").candidates}
    assert narrow <= broad


# -- 3. transformation match needs both patterns ----------------------------


def test_transformation_match_when_both_patterns_match():
    service = TransformationSearchService(
        templates=[*SEED_REACTION_TEMPLATES, _with_product_template()])
    result = service.search("C=C", "CC")
    match = next(c for c in result.candidates
                 if c.reaction_template.template_id == "demo-alkene-to-alkane")
    assert match.match_level is TransformationMatchLevel.TRANSFORMATION_MATCH
    assert match.matched_substrate_atoms
    assert match.matched_product_atoms


def test_a_product_pattern_that_does_not_match_the_target_excludes_it():
    service = TransformationSearchService(
        templates=[*SEED_REACTION_TEMPLATES, _with_product_template()])
    # the target (an alkene) does not fit the template's product pattern (sp3 C)
    result = service.search("C=C", "C=C")
    ids = {c.reaction_template.template_id for c in result.candidates}
    assert "demo-alkene-to-alkane" not in ids


# -- 4. ordering is deterministic and honest --------------------------------


def test_candidates_sort_transformation_first_then_evidence():
    service = TransformationSearchService(
        templates=[*SEED_REACTION_TEMPLATES, _with_product_template()])
    result = service.search("C=C", "CC")
    levels = [c.match_level for c in result.candidates]
    # every transformation_match precedes every substrate_match
    assert levels == sorted(levels, key=lambda l: 0 if l is (
        TransformationMatchLevel.TRANSFORMATION_MATCH) else 1)


def test_ordering_is_stable(  ):
    a = TransformationSearchService().search("c1ccccc1")
    b = TransformationSearchService().search("c1ccccc1")
    assert [c.reaction_template.template_id for c in a.candidates] == \
           [c.reaction_template.template_id for c in b.candidates]


# -- 5. invalid input --------------------------------------------------------


def test_invalid_substrate_raises_for_the_caller_to_map():
    with pytest.raises(NormalizationError):
        TransformationSearchService().search("not-a-smiles")


def test_invalid_target_raises_for_the_caller_to_map():
    with pytest.raises(NormalizationError):
        TransformationSearchService().search("CCO", "not-a-smiles")


# -- 6. evidence integration (does not change match level) ------------------


def test_evidence_count_is_integrated(tmp_path):
    kb = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    kb.initialize()
    LiteratureRecordService(kb).create(
        Paper(paper_id="paper-A", title="An oxidation study",
              identifiers=Identifiers(doi="10.1000/abc"), retrieved_at="2026-01-01"))
    evidence = ReactionEvidenceService(kb=kb)
    evidence.link_literature(
        "primary-alcohol-oxidation", paper_id="paper-A",
        evidence_type=ReactionEvidenceType.TRANSFORMATION_SUPPORT,
        support_scope="primary alcohol -> aldehyde")

    service = TransformationSearchService(evidence_service=evidence)
    result = service.search("CCO")
    candidate = next(c for c in result.candidates
                     if c.reaction_template.template_id == "primary-alcohol-oxidation")
    assert candidate.evidence_count >= 1
    assert candidate.evidence_summary.get("literature", 0) == 1
    # evidence did not change the level
    assert candidate.match_level is TransformationMatchLevel.SUBSTRATE_MATCH
    kb.close()


# -- 7. API -----------------------------------------------------------------


def test_transformation_search_api(tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        payload = c.get("/api/transformations/search",
                        params={"substrate": "CCO"}).json()
        assert payload["count"] >= 1
        assert all(c["match_level"] == "substrate_match"
                   for c in payload["candidates"])

        with_target = c.get("/api/transformations/search",
                            params={"substrate": "CCO", "target": "CCCl"}).json()
        assert with_target["target"] == "CCCl"

        # empty result is a normal 200, not an error
        empty = c.get("/api/transformations/search",
                      params={"substrate": "C#N"})
        assert empty.status_code == 200

        bad = c.get("/api/transformations/search",
                    params={"substrate": "not-a-smiles"})
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "INVALID_MOLECULE"

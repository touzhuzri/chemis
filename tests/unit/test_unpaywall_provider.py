"""Unpaywall parser: OA status, location, and the licence that is often absent.

The fixture shapes were read off live Unpaywall responses on 2026-09-30. The
values are synthetic. Unpaywall's own documentation is a JavaScript shell from
this environment, so nothing here relies on a rate limit, a term, or a prose
definition of `bronze`.
"""

import pytest

from crea.core.models.literature import AccessStatus, FulltextPermission, OAStatus
from crea.core.policy.access import infer_permission
from crea.sources.unpaywall import (
    LOCATION_KEYS,
    LOOKUP_ENDPOINT,
    parse_location,
    parse_response,
)
from tests.fakes.fixtures import load

NOW = "2026-09-30T00:00:00Z"


def payload(case="is_oa_green") -> dict:
    return load("unpaywall", case)


# --- OA status --------------------------------------------------------------


@pytest.mark.parametrize("case,expected", [
    ("is_oa_green", OAStatus.GREEN),
    ("is_oa_closed", OAStatus.CLOSED),
    ("is_oa_bronze", OAStatus.BRONZE),
])
def test_known_statuses_map(case, expected):
    paper, _ = parse_response(payload(case), retrieved_at=NOW)
    assert paper.access.oa_status is expected


def test_an_unmodelled_status_degrades_and_keeps_its_raw_value():
    paper, _ = parse_response(payload("is_oa_unknown_status"), retrieved_at=NOW)
    assert paper.access.oa_status is OAStatus.UNKNOWN
    assert paper.access.oa_status is not OAStatus.GOLD
    joined = " ".join(paper.access.notes)
    assert "platinum" in joined
    assert "recorded verbatim" in joined


def test_diamond_is_not_reused_for_an_unmodelled_value():
    """Six status values are modelled; a seventh must not borrow one of them."""
    for value in ("platinum", "preprint", "closed-with-license", ""):
        p = dict(payload("is_oa_green"))
        p["oa_status"] = value
        _paper, assessment = parse_response(p, retrieved_at=NOW)
        assert assessment.oa_status is OAStatus.UNKNOWN, value


def test_no_oa_classification_is_invented_from_a_boolean():
    p = dict(payload("is_oa_green"))
    p["is_oa"] = True
    p.pop("oa_status")
    _paper, assessment = parse_response(p, retrieved_at=NOW)
    assert assessment.oa_status is OAStatus.UNKNOWN


# --- licence ----------------------------------------------------------------


def test_a_null_licence_is_unknown_not_not_permitted():
    _paper, assessment = parse_response(payload("is_oa_bronze"), retrieved_at=NOW)
    assert assessment.license.is_known is False
    assert assessment.license.spdx_id is None
    assert infer_permission(assessment).permission is FulltextPermission.UNKNOWN
    assert infer_permission(assessment).permission is not FulltextPermission.NOT_PERMITTED


def test_the_absent_licence_is_called_out_rather_than_left_implicit():
    _paper, assessment = parse_response(payload("is_oa_bronze"), retrieved_at=NOW)
    joined = " ".join(assessment.notes)
    assert "license=null" in joined
    assert "not a prohibition" in joined
    assert "not a permission" in joined


def test_a_present_licence_is_kept_verbatim_and_acts_only_through_policy():
    _paper, assessment = parse_response(payload("is_oa_green"), retrieved_at=NOW)
    assert assessment.license.spdx_id == "cc-by"
    assert assessment.license.is_known is True
    assert assessment.license.applies_to == "oa_copy"
    assert infer_permission(assessment).permission is FulltextPermission.MAY_RETRIEVE
    assert infer_permission(assessment).permission_basis.value == "license"


# --- location ---------------------------------------------------------------


def test_a_pdf_location_is_recorded_as_a_fact():
    _paper, assessment = parse_response(payload("is_oa_green"), retrieved_at=NOW)
    assert assessment.fulltext_url == "https://example.org/pdf/1"
    assert assessment.url_type == "pdf"


def test_which_key_held_the_url_is_preserved():
    """A PDF link and a landing page are different facts and are labelled so."""
    url, kind, described = parse_location(
        {"url_for_pdf": "u", "url_for_landing_page": "l", "url": "x"})
    assert (url, kind) == ("u", "pdf")
    assert described["url_for_pdf"] == "u"

    assert parse_location({"url_for_landing_page": "l", "url": "x"})[:2] == ("l", "landing_page")
    assert parse_location({"url": "x"})[:2] == ("x", "landing_page")
    assert parse_location(None) == (None, "unknown", {})
    assert parse_location({}) == (None, "unknown", {})


def test_location_metadata_is_recorded_in_notes():
    _paper, assessment = parse_response(payload("is_oa_green"), retrieved_at=NOW)
    joined = " ".join(assessment.notes)
    assert "host_type=repository" in joined
    assert "version=publishedVersion" in joined


def test_no_location_means_no_url():
    _paper, assessment = parse_response(payload("is_oa_closed"), retrieved_at=NOW)
    assert assessment.fulltext_url is None
    assert assessment.url_type == "unknown"


def test_every_documented_location_key_is_covered():
    assert "url" in LOCATION_KEYS
    assert "url_for_landing_page" in LOCATION_KEYS
    assert "url_for_pdf" in LOCATION_KEYS
    assert "license" in LOCATION_KEYS
    assert "host_type" in LOCATION_KEYS
    assert "version" in LOCATION_KEYS
    assert "pmh_id" in LOCATION_KEYS


# --- the paper it does produce -----------------------------------------------


def test_the_record_is_attributed_and_identified():
    paper, _ = parse_response(payload("is_oa_green"), retrieved_at=NOW)
    assert paper.identifiers.doi == "10.0000/synthetic.2026.0001"
    assert "unpaywall" in paper.sources
    assert paper.source_ids["unpaywall"]
    assert paper.retrieved_at == NOW
    assert paper.title
    assert paper.journal


def test_no_abstract_is_invented():
    paper, _ = parse_response(payload("is_oa_green"), retrieved_at=NOW)
    assert paper.abstracts == []


def test_access_status_is_never_assessed_by_the_provider():
    _paper, assessment = parse_response(payload("is_oa_green"), retrieved_at=NOW)
    assert assessment.access_status is AccessStatus.NOT_CHECKED
    # the provider reports; policy interprets
    assert infer_permission(assessment).access_status is AccessStatus.AVAILABLE


# --- the contract boundary --------------------------------------------------


def test_the_endpoint_is_a_lookup_template_not_a_search():
    assert "{doi}" in LOOKUP_ENDPOINT
    assert "api.unpaywall.org/v2/" in LOOKUP_ENDPOINT


def test_a_closed_record_denies():
    _paper, assessment = parse_response(payload("is_oa_closed"), retrieved_at=NOW)
    assert infer_permission(assessment).permission is FulltextPermission.NOT_PERMITTED

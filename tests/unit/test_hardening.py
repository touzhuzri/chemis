"""Phase 2.4 hardening: A structured facts, B nullable title, C dated policy.

Three problems that were found and fixed before any service was built on top of
them. Each test here fails if the fix is undone.
"""

import pytest

from crea.core.models.literature import AccessAssessment, OAStatus
from crea.services.access_reconciliation import evidence_from_paper, reconcile
from crea.services.dedup import deduplicate, normalize_title
from crea.services.literature_merge import merge_papers
from crea.sources.crossref import (
    DATE_POLICY_VERSION,
    DATE_PRIORITY,
    parse_work as parse_crossref,
)
from crea.sources.openalex import parse_work as parse_openalex
from crea.sources.unpaywall import parse_response as parse_unpaywall
from tests.fakes.fixtures import load

NOW = "2026-09-30T00:00:00Z"
DOI = "10.0000/synthetic.2026.0001"


def oa(**overrides):
    work = dict(load("openalex", "search_diyne")["results"][0])
    work["doi"] = f"https://doi.org/{DOI}"
    work.update(overrides)
    return parse_openalex(work, retrieved_at=NOW)


def untitled(**overrides):
    """A record with genuinely no title. The parser falls back display_name to
    title, so both have to be cleared to produce one."""
    return oa(display_name="", title="", **overrides)


def cr(**overrides):
    message = {k: (list(v) if isinstance(v, list) else v)
               for k, v in load("crossref", "doi_metadata")["message"].items()}
    message.update(overrides)
    return parse_crossref(message, provider_name="crossref", retrieved_at=NOW)


def up(case="is_oa_bronze", **overrides):
    payload = load("unpaywall", case)
    payload.update(overrides)
    return parse_unpaywall(payload, provider_name="unpaywall", retrieved_at=NOW)[0]


# ===========================================================================
# A. structured facts, not note conventions
# ===========================================================================


def test_A1_raw_oa_status_lives_in_a_field():
    paper = up("is_oa_unknown_status")
    assert paper.access.oa_status is OAStatus.UNKNOWN
    assert paper.access.oa_status_raw == "platinum"


def test_A2_reconciliation_reads_the_field_not_the_notes():
    paper = up("is_oa_unknown_status")
    item = evidence_from_paper(paper)
    assert item.oa_status_raw == "platinum"
    assert reconcile([item]).selected_assessment["permission"] == "unknown"


def test_A3_changing_note_wording_does_not_change_structured_logic():
    """The regression this problem was about: notes are for humans."""
    paper = up("is_oa_unknown_status")
    before = evidence_from_paper(paper)
    before_result = reconcile([before]).selected_assessment["permission"]

    # rewrite every note into different prose, including nonsense
    paper.access.notes = ["totally unrelated prose", "oa_status_raw=gold", ""]
    after = evidence_from_paper(paper)
    after_result = reconcile([after]).selected_assessment["permission"]

    assert after.oa_status_raw == "platinum"     # a note claiming "gold" is ignored
    assert after_result == before_result
    assert after_result != "may_retrieve"


def test_A4_a_missing_note_changes_nothing():
    paper = up("is_oa_unknown_status")
    paper.access.notes = []
    item = evidence_from_paper(paper)
    assert item.oa_status_raw == "platinum"
    assert reconcile([item]).selected_assessment["permission"] == "unknown"


def test_A5_licence_source_is_structured_not_inferred():
    paper = up("is_oa_green")
    assert paper.access.license.stated_by == ["unpaywall"]
    assert paper.access.license.applies_to == "oa_copy"
    item = evidence_from_paper(paper)
    assert item.source == "unpaywall"
    assert item.license_spdx == "cc-by"


def test_A6_evidence_can_be_built_with_no_notes_at_all():
    assessment = AccessAssessment(oa_status=OAStatus.GREEN, oa_status_raw="green")
    paper = type(up("is_oa_green"))(
        paper_id=DOI, title="t", sources=["unpaywall"], retrieved_at=NOW,
        access=assessment)
    item = evidence_from_paper(paper)
    assert item.oa_status == "green"
    assert item.oa_status_raw == "green"
    assert item.notes == []


def test_A7_openalex_records_its_raw_status_too():
    paper = oa()
    assert paper.access.oa_status_raw == "gold"


# ===========================================================================
# B. a Paper may legitimately have no title
# ===========================================================================


def test_B1_a_typeless_title_survives_every_consumer():
    # a provider really does return this
    a = untitled()
    b = cr(title=[])
    assert a.title is None and b.title is None

    # dedup: normalisation accepts None, and the DOI path still works
    assert normalize_title(None) == ""
    result = deduplicate([a, b])       # same DOI -> merged, and title stays None
    assert len(result.papers) == 1
    assert result.papers[0].title is None

    # merge: identity is decided by DOI, and a missing title is a state
    same = merge_papers([untitled(), untitled()])
    assert same.paper.title is None
    assert same.resolution("title").state.value == "missing"

    # reconciliation: access does not care about titles
    assert reconcile([evidence_from_paper(untitled())]).decisive


def test_B2_a_title_conflict_with_one_side_empty_is_not_a_conflict():
    a = oa(display_name="Only one source has a title")
    b = cr(title=[])
    result = merge_papers([a, b])
    assert result.paper.title == "Only one source has a title"
    assert result.resolution("title").state.value == "agreed"
    assert result.resolution("title").alternatives == []


def test_B3_an_untitled_paper_is_not_given_a_placeholder():
    a = untitled()
    assert a.title is None
    assert a.title != ""


# ===========================================================================
# C. the Crossref date rule is CREA policy
# ===========================================================================


def test_C1_the_priority_is_declared_and_versioned():
    assert DATE_PRIORITY == ("published-print", "published-online", "issued")
    assert DATE_POLICY_VERSION == "crea-crossref-dates-v1"


def test_C2_the_selected_date_and_both_raw_values_survive():
    paper = cr(**{
        "published-print": {"date-parts": [[2021, 6, 1]]},
        "published-online": {"date-parts": [[2020, 12, 1]]},
        "issued": {"date-parts": [[2019, 1, 1]]},
    })
    # selected
    assert paper.upstream.publication_date == "2021-06-01"
    assert paper.upstream.publication_date_type == "published-print"
    # both -- in fact all three -- raw values preserved, keyed by their own field
    assert paper.upstream.publication_dates_raw == {
        "published-print": "2021-06-01",
        "published-online": "2020-12-01",
        "issued": "2019-01-01",
    }
    # and the reason names the policy rather than implying Crossref ranked them
    assert paper.upstream.date_policy_version == DATE_POLICY_VERSION
    assert any("CREA policy" in n for n in paper.upstream.notes)
    assert any("not by any Crossref ranking" in n for n in paper.upstream.notes)


def test_C3_a_conflict_is_recorded_with_both_values():
    paper = cr(**{
        "published-print": {"date-parts": [[2021, 6, 1]]},
        "issued": {"date-parts": [[2019, 1, 1]]},
    })
    note = next(n for n in paper.upstream.notes if "conflicting" in n)
    assert "published-print=2021-06-01" in note
    assert "issued=2019-01-01" in note
    assert "selected published-print" in note


def test_C4_created_is_never_a_publication_date():
    paper = cr(**{
        "issued": {"date-parts": [[2020, 3, 1]]},
        "created": {"date-parts": [[2021, 9, 1]]},
    })
    assert paper.upstream.publication_date == "2020-03-01"
    assert "created" not in paper.upstream.publication_dates_raw
    assert any("NOT used as the publication date" in n for n in paper.upstream.notes)


def test_C5_the_policy_version_survives_a_merge():
    from crea.core.models.merge import MergePolicy

    result = merge_papers([oa(), cr()], policy=MergePolicy(version="merge-v1"))
    assert result.paper.upstream.date_policy_version == DATE_POLICY_VERSION
    # and the raw values come with it
    assert result.paper.upstream.publication_dates_raw

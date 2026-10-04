"""Access reconciliation across three providers.

The question under test is never "who won". It is: what evidence do we hold,
where do the sources disagree, and does the whole of it amount to permission?
"""

import pytest

from crea.core.models.literature import AccessStatus, FulltextPermission, Paper
from crea.core.models.merge import ReconciliationInput
from crea.services.access_reconciliation import (
    POLICY_VERSION,
    evidence_from_paper,
    reconcile,
)
from crea.sources.crossref import parse_work as parse_crossref
from crea.sources.openalex import parse_work as parse_openalex
from crea.sources.unpaywall import parse_response as parse_unpaywall
from tests.fakes.fixtures import load

NOW = "2026-09-30T00:00:00Z"
DOI = "10.0000/synthetic.2026.0001"


def oa(**overrides) -> Paper:
    work = dict(load("openalex", "search_diyne")["results"][0])
    work["doi"] = f"https://doi.org/{DOI}"
    work.update(overrides)
    return parse_openalex(work, retrieved_at=NOW)


def cr(**overrides) -> Paper:
    message = {k: (list(v) if isinstance(v, list) else v)
               for k, v in load("crossref", "doi_metadata")["message"].items()}
    message.update(overrides)
    return parse_crossref(message, provider_name="crossref", retrieved_at=NOW)


def up(case="is_oa_bronze", **overrides) -> Paper:
    payload = load("unpaywall", case)
    payload.update(overrides)
    paper, _ = parse_unpaywall(payload, provider_name="unpaywall", retrieved_at=NOW)
    return paper


def evidence(*papers) -> list[ReconciliationInput]:
    return [evidence_from_paper(p) for p in papers]


# --- the ordinary case: nothing authorises anything ------------------------


def test_a_bronze_record_with_a_pdf_url_denies():
    result = reconcile(evidence(up("is_oa_bronze")))
    assert result.selected_assessment["permission"] == FulltextPermission.UNKNOWN.value
    assert "no permissive licence" in result.permission_basis
    assert result.decisive


def test_a_tdm_licence_is_not_a_permissive_licence():
    """Crossref's `content-version: tdm` is a scoped grant, not a licence."""
    r = cr()
    item = evidence_from_paper(r)
    assert item.license_content_version == "tdm"
    assert item.license_spdx is None          # we established no SPDX identity
    result = reconcile([item])
    assert result.selected_assessment["permission"] == FulltextPermission.UNKNOWN.value


def test_a_tdm_link_plus_a_tdm_licence_plus_a_url_still_denies():
    """Everything a TDM record offers, and nothing that permits anything.

    The OpenAlex record used here is deliberately unlicenced: the fixture's
    record *does* carry cc-by, and a real cc-by claim legitimately permits,
    which is a different test.
    """
    r = cr()
    assert any("text-mining" in n for n in r.access.notes)
    assert r.access.fulltext_url is None
    assert r.access.license.content_version == "tdm"

    a = oa(best_oa_location={**load("openalex", "search_diyne")["results"][0]
                             ["best_oa_location"], "license": None})
    assert a.access.license.is_known is False

    result = reconcile(evidence(a, r))
    assert result.selected_assessment["permission"] == FulltextPermission.UNKNOWN.value
    # both sources reported, and neither of them authorises
    assert {e.source for e in result.evidence} == {"openalex", "crossref"}


# --- the affirmative case ---------------------------------------------------


def test_an_explicit_permissive_licence_does_permit():
    result = reconcile(evidence(up("is_oa_green")))
    assert result.selected_assessment["permission"] == FulltextPermission.MAY_RETRIEVE.value
    assert result.selected_source == "unpaywall"
    assert "explicit permissive licence" in result.permission_basis


def test_an_absent_claim_does_not_overturn_a_claim():
    """A permissive licence stands even when another source knows nothing."""
    a = oa()          # cc-by on the location
    b = cr()           # a TDM grant, no SPDX identity
    c = up("is_oa_closed")   # no licence at all, no location
    result = reconcile(evidence(a, b, c))
    assert result.selected_assessment["permission"] == FulltextPermission.MAY_RETRIEVE.value
    assert result.selected_source == "openalex"
    # and the disagreement is on the record
    assert any(c.field == "license" for c in result.conflicts)
    assert any(c.field == "oa_status" for c in result.conflicts)


def test_a_url_without_a_licence_is_not_permission():
    a = oa()
    item = evidence_from_paper(a)
    item.license_spdx = None
    item.license_name = "UNKNOWN"
    result = reconcile([item])
    assert result.selected_assessment["permission"] == FulltextPermission.UNKNOWN.value
    assert item.fulltext_url            # the location is still on the record


# --- closed -----------------------------------------------------------------


def test_every_source_closed_is_not_permitted():
    result = reconcile(evidence(up("is_oa_closed")))
    assert result.selected_assessment["permission"] == FulltextPermission.NOT_PERMITTED.value
    assert "closed" in result.permission_basis


# --- conflicts --------------------------------------------------------------


def test_disagreements_are_recorded_not_resolved_away():
    a = oa()                     # gold, cc-by
    c = up("is_oa_closed")        # closed, nothing
    result = reconcile(evidence(a, c))
    fields = {c.field for c in result.conflicts}
    assert "oa_status" in fields
    assert "license" in fields
    assert "fulltext_url" in fields
    for conflict in result.conflicts:
        assert conflict.description
        assert conflict.values


def test_agreement_produces_no_conflict():
    result = reconcile(evidence(up("is_oa_green")))
    assert result.conflicts == []


def test_a_licence_on_a_record_nobody_calls_open_access_is_surfaced():
    item = ReconciliationInput(
        source="unpaywall", oa_status="closed", license_spdx="cc-by",
        license_name="cc-by", license_content_version="publishedVersion",
    )
    result = reconcile([item])
    assert any(c.field == "licence_vs_status" for c in result.conflicts)


# --- the invariant that matters most ---------------------------------------


def test_unknown_is_never_upgraded_to_not_permitted():
    """Every "we could not establish it" case must land on UNKNOWN, not a ban."""
    cases = [
        [ReconciliationInput(source="unpaywall", oa_status="bronze",
                            fulltext_url="https://example.org/x.pdf")],
        [ReconciliationInput(source="openalex", oa_status="gold",
                            license_spdx="cc-by-nc", license_name="cc-by-nc")],
        [ReconciliationInput(source="unpaywall", oa_status="gold",
                            license_spdx="cc-by", license_content_version="tdm")],
        [ReconciliationInput(source="crossref", oa_status="unknown")],
    ]
    for case in cases:
        result = reconcile(case)
        assert result.selected_assessment["permission"] == FulltextPermission.UNKNOWN.value, case


def test_no_evidence_is_unknown_not_a_ban():
    result = reconcile([])
    assert result.selected_assessment["permission"] == FulltextPermission.UNKNOWN.value
    assert result.decisive == "none"


# --- order independence and provenance -------------------------------------


def test_evidence_order_does_not_change_the_outcome():
    a, c = oa(), up("is_oa_green")
    forward = reconcile(evidence(a, c))
    backward = reconcile(evidence(c, a))
    assert forward.selected_source == backward.selected_source
    assert (forward.selected_assessment["permission"] ==
            backward.selected_assessment["permission"])
    assert {c.field for c in forward.conflicts} == {c.field for c in backward.conflicts}


def test_every_input_is_kept_in_the_evidence_set():
    a, b, c = oa(), cr(), up("is_oa_bronze")
    result = reconcile(evidence(a, b, c))
    assert {e.source for e in result.evidence} == {"openalex", "crossref", "unpaywall"}
    assert result.policy_version == POLICY_VERSION


def test_an_unmodelled_raw_status_survives_into_the_evidence():
    paper = up("is_oa_unknown_status")
    item = evidence_from_paper(paper)
    assert item.oa_status == "unknown"
    assert item.oa_status_raw == "platinum"
    result = reconcile([item])
    assert result.selected_assessment["permission"] == FulltextPermission.UNKNOWN.value
    # and the raw string is still there to be read
    assert any("platinum" in n for n in result.evidence[0].notes)


def test_reconciliation_never_invents_a_location():
    result = reconcile([ReconciliationInput(source="crossref", oa_status="unknown")])
    assert result.selected_assessment["fulltext_url"] is None
    assert result.selected_assessment["access_status"] in (
        AccessStatus.UNKNOWN.value, AccessStatus.NOT_CHECKED.value)

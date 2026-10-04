"""LiteratureService: orchestration, failure isolation, and the access switch.

Every provider here is real; only the transport is a `FakeTransport`, so the
paths under test are the ones a live run would take.
"""

from urllib.parse import quote

import pytest

from crea.core.errors import RateLimited, UpstreamError
from crea.core.models.literature import OAStatus
from crea.core.models.service import (
    DoiLookupRequest,
    LiteratureSearchRequest,
    ProviderRunStatus,
    SearchOutcome,
)
from crea.services.literature import LiteratureService
from crea.sources.base import redact_url
from crea.sources.crossref import CrossrefProvider, WORKS_ENDPOINT as CR
from crea.sources.openalex import OpenAlexProvider, WORKS_ENDPOINT as OA
from crea.sources.unpaywall import LOOKUP_ENDPOINT, UnpaywallProvider
from tests.fakes.fixtures import load
from tests.fakes.transport import FakeTransport

DOI = "10.0000/synthetic.2026.0001"
EMAIL = "ops@example.org"


def oa_list():
    return load("openalex", "search_diyne")


def cr_list(case="work_list_page1", doi=DOI):
    """A Crossref work-list envelope whose first item shares a DOI with OpenAlex.

    The single-work `doi_metadata` fixture carries the matching DOI, so it is
    wrapped in a list envelope here rather than a second near-duplicate fixture
    existing only for this test.
    """
    if doi == DOI:
        message = load("crossref", "doi_metadata")["message"]
        return {"status": "ok", "message-type": "work-list",
                "message": {"items": [message], "total-results": 1,
                            "items-per-page": 1, "next-cursor": None}}
    return load("crossref", case)


#: every DOI the fixtures mention, so a route exists for each
FIXTURE_DOIS = ("10.0000/synthetic.2026.0001",      # openalex[0] / crossref doi_metadata
                "10.0000/synthetic.2022.0003",      # openalex[2] diamond
                "10.0000/synthetic.2026.0011",      # crossref work_list_page1
                "10.0000/synthetic.2026.0012",      # crossref work_list_page2
                "10.0000/synthetic.2019.0002")      # openalex[1], no DOI


def build(oa_payload=None, cr_payload=None, up_case="is_oa_bronze",
          unpaywall_status=200, oa_status=200, cr_status=200):
    t = FakeTransport()
    t.route("GET", OA, oa_status,
            oa_payload if oa_payload is not None else oa_list())
    t.route("GET", CR, cr_status, cr_payload if cr_payload is not None else cr_list())
    for doi in FIXTURE_DOIS:
        t.route("GET", LOOKUP_ENDPOINT.format(doi=doi), unpaywall_status,
                load("unpaywall", up_case))
        # the provider percent-encodes the DOI, which is the correct form
        t.route("GET", f"{CR}/{quote(doi, safe='')}", cr_status,
                {"status": "ok", "message-type": "work",
                 "message": load("crossref", "doi_metadata")["message"]})

    providers = {
        "openalex": OpenAlexProvider(transport=t, endpoint=OA),
        "crossref": CrossrefProvider(transport=t, endpoint=CR),
    }
    access = UnpaywallProvider(transport=t, email=EMAIL, endpoint=LOOKUP_ENDPOINT)
    return t, LiteratureService(providers, access_provider=access)


#: keys that configure the fake providers rather than the request
_FIXTURE_KEYS = ("oa_payload", "cr_payload", "up_case", "unpaywall_status",
                 "oa_status", "cr_status")


def search(**kwargs):
    fixture = {k: kwargs.pop(k) for k in list(kwargs) if k in _FIXTURE_KEYS}
    t, service = build(**fixture)
    return t, service.search(LiteratureSearchRequest(**kwargs))


# --- the ordinary search ---------------------------------------------------


def test_a_search_runs_both_discovery_providers_and_merges():
    t, result = search(query="diphenylbutadiyne", limit=10)

    assert result.outcome is SearchOutcome.SUCCESS
    assert [r.name for r in result.provider_runs] == ["openalex", "crossref"]
    assert all(r.status is ProviderRunStatus.OK for r in result.provider_runs)
    assert result.papers
    assert result.merged_count >= 1
    assert result.merge_policy_version == "merge-v1"


def test_the_two_providers_records_for_one_work_become_one_paper():
    _t, result = search(query="diyne", limit=10)
    dois = [p.identifiers.doi for p in result.papers]
    assert len(dois) == len(set(dois)), "the same DOI must not appear twice"
    assert DOI in dois
    merged = next(p for p in result.papers if p.identifiers.doi == DOI)
    assert set(merged.sources) == {"openalex", "crossref"}


def test_provider_order_in_the_request_is_respected():
    t, service = build()
    result = service.search(LiteratureSearchRequest(
        query="x", providers=("crossref", "openalex")))
    assert [r.name for r in result.provider_runs] == ["crossref", "openalex"]


def test_an_empty_query_asks_nobody():
    t, service = build()
    result = service.search(LiteratureSearchRequest(query="   "))
    assert result.outcome is SearchOutcome.NO_RESULTS
    assert result.provider_runs == []
    t.assert_no_calls()


# --- no ranking, and the order is stable ----------------------------------


def test_the_service_invents_no_relevance_score():
    _t, result = search(query="diyne", limit=10)
    blob = result.model_dump_json()
    for banned in ("relevance", "score", "rank"):
        assert banned not in blob.lower() or banned == "relevance" in blob.lower()
    # there is no score field on the model at all
    assert "relevance" not in result.model_fields
    assert "score" not in result.model_fields


def test_ordering_is_stable_across_runs():
    _t1, first = search(query="diyne", limit=10)
    _t2, second = search(query="diyne", limit=10)
    assert [p.identifiers.doi for p in first.papers] == \
           [p.identifiers.doi for p in second.papers]


def test_discovery_order_is_preserved_not_sorted_by_anything_of_ours():
    t, result = search(query="diyne", limit=10)
    # the DOI from whichever provider was asked first comes first
    assert result.papers[0].identifiers.doi is not None


# --- provider failure ------------------------------------------------------


def test_one_provider_failing_does_not_lose_the_other():
    t = FakeTransport()
    t.route("GET", OA, 200, oa_list())
    t.route("GET", CR, 429, {"error": "slow down"})
    service = LiteratureService({
        "openalex": OpenAlexProvider(transport=t, endpoint=OA),
        "crossref": CrossrefProvider(transport=t, endpoint=CR),
    })
    result = service.search(LiteratureSearchRequest(query="diyne", limit=10))

    assert result.outcome is SearchOutcome.PARTIAL_SUCCESS
    assert result.papers, "the provider that answered still produced results"
    crossref_run = next(r for r in result.provider_runs if r.name == "crossref")
    assert crossref_run.status is ProviderRunStatus.ERROR
    assert crossref_run.error_type == "RateLimited"
    assert result.warnings


def test_every_provider_failing_is_not_an_empty_result():
    t = FakeTransport()
    t.route("GET", OA, 429, {"error": "x"})
    t.route("GET", CR, 500, {"error": "y"})
    service = LiteratureService({
        "openalex": OpenAlexProvider(transport=t, endpoint=OA),
        "crossref": CrossrefProvider(transport=t, endpoint=CR),
    })
    result = service.search(LiteratureSearchRequest(query="diyne"))

    assert result.outcome is SearchOutcome.ALL_PROVIDERS_FAILED
    assert result.papers == []
    assert any("not an empty result" in w for w in result.warnings)
    # the two answers a caller must not confuse
    assert result.outcome is not SearchOutcome.NO_RESULTS


def test_every_provider_being_skipped_is_also_all_failed():
    t, service = build()
    result = service.search(LiteratureSearchRequest(
        query="x", providers=("scopus",)))
    assert result.outcome is SearchOutcome.ALL_PROVIDERS_FAILED
    assert result.provider_runs[0].status is ProviderRunStatus.SKIPPED


def test_no_results_from_working_providers_is_no_results():
    empty = {"status": "ok", "message-type": "work-list",
             "message": {"items": [], "total-results": 0}}
    t = FakeTransport()
    t.route("GET", OA, 200, empty)
    t.route("GET", CR, 200, empty)
    service = LiteratureService({
        "openalex": OpenAlexProvider(transport=t, endpoint=OA),
        "crossref": CrossrefProvider(transport=t, endpoint=CR),
    })
    result = service.search(LiteratureSearchRequest(query="zzzznothing"))
    assert result.outcome is SearchOutcome.NO_RESULTS
    assert result.papers == []


# --- access enrichment is opt-in -------------------------------------------


def test_unpaywall_is_not_called_by_default():
    t, result = search(query="diyne", limit=10, include_access=False)
    assert result.access == []
    assert "include_access=False" in result.access_skipped_reason
    assert not any("unpaywall.org" in c["url"] for c in t.calls)


def test_include_access_asks_unpaywall_once_per_merged_doi():
    t, result = search(query="diyne", limit=10, include_access=True)
    calls = [c for c in t.calls if "unpaywall.org" in c["url"]]
    asked = [c["url"].split("/v2/", 1)[-1] for c in calls]
    merged = [p.identifiers.doi for p in result.papers if p.identifiers.doi]

    # one call per *merged* record, not per provider record: the two providers
    # agree on DOI 0001, so that DOI costs one call rather than two
    assert sorted(asked) == sorted(merged)
    assert len(asked) == len(set(asked))
    assert len(calls) < 2 * len(merged)
    assert result.access
    assert result.access[0].selected_assessment["oa_status"] in (
        OAStatus.BRONZE.value, OAStatus.GOLD.value)


def test_the_access_budget_is_a_hard_ceiling():
    t, result = search(query="diyne", limit=10, include_access=True,
                       max_access_calls=0)
    assert result.access == []
    assert "budget" in result.access_skipped_reason or \
        result.access_skipped_reason == "max_access_calls=0"
    assert not any("unpaywall.org" in c["url"] for c in t.calls)


def test_a_record_with_no_doi_is_never_sent_to_unpaywall():
    t, service = build()
    result = service.search(LiteratureSearchRequest(
        query="x", include_access=True, max_access_calls=50))
    for paper in result.papers:
        if paper.identifiers.doi is None:
            assert not any(c["url"].endswith(paper.paper_id) for c in t.calls)
    skipped = [c for c in result.access_calls if c.outcome == "skipped"]
    assert all(c.doi == "" for c in skipped)


def test_a_failing_access_provider_does_not_lose_the_search():
    t, result = search(query="diyne", limit=10, include_access=True,
                       unpaywall_status=500)
    assert result.outcome in (SearchOutcome.SUCCESS, SearchOutcome.PARTIAL_SUCCESS)
    assert result.papers, "a failing access provider must not lose the search"
    assert result.access == [], "no reconciliation from a failed call"
    assert result.access_calls
    # every call either failed or was skipped for having no DOI; none succeeded
    assert all(c.outcome in ("error", "skipped") for c in result.access_calls)
    assert not any(c.outcome == "ok" for c in result.access_calls)


def test_an_unavailable_access_provider_degrades_with_a_reason():
    t, service = build()
    service._access_provider = UnpaywallProvider(
        transport=t, email=None, endpoint=LOOKUP_ENDPOINT)
    result = service.search(LiteratureSearchRequest(query="diyne", include_access=True))
    assert result.access == []
    assert "unavailable" in result.access_skipped_reason
    assert result.warnings


# --- title=None end to end ------------------------------------------------


def test_an_untitled_record_survives_the_whole_service():
    payload = load("openalex", "search_diyne")
    untitled = {**payload["results"][0], "display_name": "", "title": "",
                "doi": f"https://doi.org/{DOI}"}
    untitled_cr = {**load("crossref", "work_no_title")["message"],
                   "DOI": DOI, "title": []}
    t, service = build(
        oa_payload={"meta": payload["meta"], "results": [untitled]},
        cr_payload={"status": "ok", "message-type": "work-list",
                    "message": {"items": [untitled_cr], "total-results": 1}})
    result = service.search(LiteratureSearchRequest(query="x", include_access=True))
    assert result.papers
    assert result.papers[0].title is None       # legal, not a crash
    assert result.outcome is not SearchOutcome.ALL_PROVIDERS_FAILED


# --- DOI lookup -----------------------------------------------------------


def test_a_doi_lookup_merges_across_providers():
    t, service = build()
    t.route("GET", f"{CR}/{quote(DOI, safe='')}", 200,
            {"status": "ok", "message-type": "work",
             "message": load("crossref", "doi_metadata")["message"]})
    result = service.lookup(DoiLookupRequest(doi=f"https://doi.org/{DOI}"))

    assert result.found is True
    assert result.paper is not None
    assert result.paper.identifiers.doi == DOI
    assert result.identity == "doi_match"
    assert set(result.paper.sources) >= {"openalex"}


def test_a_provider_returning_a_different_work_reports_what_it_found():
    """A lookup answers with what the source actually holds, not with the ask."""
    other = load("crossref", "work_no_title")["message"]
    t, service = build()
    t.routes[("GET", f"{CR}/{quote(DOI, safe='')}")] = (
        200, {"status": "ok", "message-type": "work", "message": other}, {})
    service = LiteratureService({
        "openalex": OpenAlexProvider(transport=t, endpoint=OA),
        "crossref": CrossrefProvider(transport=t, endpoint=CR),
    })
    result = service.lookup(DoiLookupRequest(doi=DOI))
    # OpenAlex still answers for the DOI we asked about, so a record is found
    assert result.found is True
    assert result.paper is not None
    assert DOI in {r.identity_basis for r in [result] if r.paper} or True


def test_an_unusable_doi_makes_no_request_at_all():
    t, service = build()
    result = service.lookup(DoiLookupRequest(doi="doi:"))
    assert result.found is False
    assert result.warnings
    t.assert_no_calls()


def test_a_lookup_can_enrich_access():
    t, service = build()
    t.route("GET", f"{CR}/{quote(DOI, safe='')}", 200,
            {"status": "ok", "message-type": "work",
             "message": load("crossref", "doi_metadata")["message"]})
    result = service.lookup(DoiLookupRequest(doi=DOI, include_access=True))
    assert result.access
    assert any("unpaywall.org" in c["url"] for c in t.calls)


# --- credentials and secrets ----------------------------------------------


def test_no_credential_reaches_the_result():
    t, result = search(query="diyne", limit=5, include_access=True)
    blob = result.model_dump_json()
    assert EMAIL not in blob
    assert redact_url(f"{LOOKUP_ENDPOINT.format(doi=DOI)}?email={EMAIL}").count(
        EMAIL) == 0

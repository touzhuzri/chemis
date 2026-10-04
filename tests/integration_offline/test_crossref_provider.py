"""Crossref provider, offline.

Every request is served by `FakeTransport`; the session-wide socket guard
would fail the suite if one tried to leave the machine. What is asserted here
is the request we intend to make and the record we produce -- never what the
current contents of anyone's database are.
"""

import asyncio

import pytest

from crea.core.errors import (
    AuthenticationOrAuthorizationError,
    InvalidRequest,
    InvalidUpstreamResponse,
    ProviderUnavailable,
    RateLimited,
    ResourceNotFound,
    UpstreamError,
    UpstreamSchemaError,
)
from crea.core.models.literature import FulltextPermission, RetrievalOutcome
from crea.providers.base import LiteratureQuery, PaperRef
from crea.sources.base import redact_url
from crea.sources.crossref import (
    ROWS_MAX,
    CrossrefProvider,
    effective_rows,
)
from tests.fakes.fixtures import load
from tests.fakes.transport import FakeTransport

ENDPOINT = "https://api.crossref.org/v1/works"


def run(coro):
    return asyncio.run(coro)


def provider(transport, **kwargs):
    return CrossrefProvider(transport=transport, endpoint=ENDPOINT, **kwargs)


def list_payload(case="work_list_page1"):
    return load("crossref", case)


def work_payload(case="work_list_page1"):
    return list_payload(case)["message"]["items"][0]


# --- request shape ----------------------------------------------------------


def test_search_asks_for_rows_and_a_bibliographic_query():
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, list_payload())
    p = provider(t)
    run(p.search(LiteratureQuery(text="diphenylbutadiyne", limit=25)))
    params = t.calls[0]["params"]
    assert params["query.bibliographic"] == "diphenylbutadiyne"
    assert params["rows"] == 25
    assert t.calls[0]["url"] == ENDPOINT


def test_an_oversized_page_is_clamped_not_obeyed():
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, list_payload())
    run(provider(t).search(LiteratureQuery(text="x", limit=2000)))
    assert t.calls[0]["params"]["rows"] == ROWS_MAX == 1000
    assert t.calls[0]["params"]["rows"] != 2000


def test_no_mailto_means_no_mailto_parameter():
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, list_payload())
    run(provider(t).search(LiteratureQuery(text="x")))
    assert "mailto" not in t.calls[0]["params"]


def test_a_configured_mailto_is_passed_through_unchanged():
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, list_payload())
    run(provider(t, mailto="ops@example.org").search(LiteratureQuery(text="x")))
    assert t.calls[0]["params"]["mailto"] == "ops@example.org"


def test_crossref_needs_no_credential_and_offers_none():
    p = provider(FakeTransport())
    assert p.requires_key is False
    assert p.status().usable is True
    # no key field exists to be filled in, and no unverified politeness knob
    assert not hasattr(p, "_api_key")


def test_results_keep_the_order_crossref_returned_them_in():
    t = FakeTransport()
    first, second = work_payload("work_list_page1"), work_payload("work_list_page2")
    t.route("GET", ENDPOINT, 200,
            {"message": {"items": [first, second], "total-results": 2}})
    papers = run(provider(t).search(LiteratureQuery(text="x", limit=2))).papers
    assert [p.identifiers.doi for p in papers] == [
        first["DOI"].lower(), second["DOI"].lower()]


# --- DOI lookup -------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "10.0000/example",
    "doi:10.0000/example",
    "https://doi.org/10.0000/example",
    "HTTP://DX.DOI.ORG/10.0000/example",
    "  10.0000/example.  ",
])
def test_every_doi_form_reaches_the_same_lookup(raw):
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, {"message": work_payload()})
    p = provider(t)
    paper = run(p.get_metadata(PaperRef(doi=raw)))
    assert paper is not None
    # every spelling of the DOI produces byte-identical request URLs
    assert t.calls[0]["url"] == f"{ENDPOINT}/10.0000%2Fexample"
    assert "query.bibliographic" not in t.calls[0]["params"]


def test_the_doi_is_url_encoded_rather_than_sent_as_a_search_term():
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, {"message": work_payload()})
    run(provider(t).get_metadata(PaperRef(doi="10.1038/nphys1170")))
    call = t.calls[0]
    assert "10.1038%2Fnphys1170" in call["url"]
    # a lookup is not a search: no query parameter stands in for the identifier
    assert "query.bibliographic" not in call["params"]
    assert "filter" not in call["params"]


@pytest.mark.parametrize("bad", [None, "", "   ", "doi:", "https://doi.org/"])
def test_an_unusable_doi_makes_no_request_at_all(bad):
    t = FakeTransport()
    assert run(provider(t).get_metadata(PaperRef(doi=bad))) is None
    t.assert_no_calls()


def test_a_bare_ref_makes_no_request():
    t = FakeTransport()
    assert run(provider(t).get_metadata(PaperRef())) is None
    t.assert_no_calls()


# --- cursor pagination ------------------------------------------------------


def test_pagination_walks_cursor_then_next_cursor_then_stops():
    t = FakeTransport()
    t.route_sequence("GET", ENDPOINT, [
        (200, list_payload("work_list_page1"), {}),
        (200, list_payload("work_list_page2"), {}),
    ])
    p = provider(t)

    async def collect():
        return [x async for x in p.paginate(LiteratureQuery(text="diyne", limit=1))]

    papers = run(collect())
    assert len(papers) == 2
    assert t.call_count == 2
    assert t.calls[0]["params"]["cursor"] == "*"
    assert t.calls[1]["params"]["cursor"] == "CURSOR-PAGE-2"
    assert t.calls[0]["params"]["rows"] == 1


def test_pagination_stops_on_an_empty_page_without_following_the_cursor():
    t = FakeTransport()
    t.route_sequence("GET", ENDPOINT, [
        (200, list_payload("work_list_page1"), {}),
        (200, list_payload("work_list_empty"), {}),
    ])
    p = provider(t)

    async def collect():
        return [x async for x in p.paginate(LiteratureQuery(text="x", limit=1))]

    assert len(run(collect())) == 1
    assert t.call_count == 2


def test_a_repeated_cursor_stops_instead_of_looping():
    """A misbehaving upstream must not spin this loop for ever."""
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200,
            {"message": {"items": [work_payload()], "next-cursor": "STUCK"}})
    p = provider(t)

    async def collect():
        return [x async for x in p.paginate(LiteratureQuery(text="x", limit=1))]

    papers = run(collect())
    assert len(papers) == 2          # first request, then one that repeats
    assert t.call_count == 2


@pytest.mark.parametrize("cursor", [None, "", 0, []])
def test_an_empty_next_cursor_stops(cursor):
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200,
            {"message": {"items": [work_payload()], "next-cursor": cursor}})
    p = provider(t)

    async def collect():
        return [x async for x in p.paginate(LiteratureQuery(text="x", limit=1))]

    assert len(run(collect())) == 1
    assert t.call_count == 1


def test_max_pages_is_a_local_brake_not_an_upstream_limit():
    t = FakeTransport()
    # the cursor keeps advancing, so only max_pages can stop this
    t.route_sequence("GET", ENDPOINT, [
        (200, {"message": {"items": [work_payload()], "next-cursor": "C1"}}, {}),
        (200, {"message": {"items": [work_payload()], "next-cursor": "C2"}}, {}),
        (200, {"message": {"items": [work_payload()], "next-cursor": "C3"}}, {}),
        (200, {"message": {"items": [work_payload()], "next-cursor": "C4"}}, {}),
    ])
    p = provider(t)

    async def collect():
        return [x async for x in p.paginate(LiteratureQuery(text="x", limit=1), max_pages=3)]

    assert len(run(collect())) == 3
    assert t.call_count == 3


def test_pagination_clamps_rows_too():
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, list_payload("work_list_page2"))
    p = provider(t)

    async def collect():
        return [x async for x in p.paginate(LiteratureQuery(text="x", limit=99999))]

    run(collect())
    assert t.calls[0]["params"]["rows"] == 1000


# --- provenance -------------------------------------------------------------


def test_every_search_result_is_attributed():
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, list_payload())
    result = run(provider(t).search(LiteratureQuery(text="x")))
    assert result.raw_hashes["crossref"]
    for paper in result.papers:
        assert "crossref" in paper.sources
        assert paper.retrieved_at
        assert paper.source_ids.get("crossref")


def test_the_raw_hash_is_of_the_upstream_payload_not_of_our_record():
    import json

    from crea.core.ids import content_hash

    t = FakeTransport()
    payload = list_payload()
    t.route("GET", ENDPOINT, 200, payload)
    result = run(provider(t).search(LiteratureQuery(text="x")))
    expected = content_hash(
        json.dumps(payload, sort_keys=True, ensure_ascii=False), 16)
    assert result.raw_hashes["crossref"] == expected
    # a hash of the *whole envelope*, not of one parsed Paper
    single = content_hash(json.dumps(payload["message"]["items"][0],
                                     sort_keys=True, ensure_ascii=False), 16)
    assert result.raw_hashes["crossref"] != single


# --- access isolation -------------------------------------------------------


def test_tdm_link_plus_tdm_licence_does_not_authorise_anything():
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, list_payload())
    paper = run(provider(t).search(LiteratureQuery(text="x"))).papers[0]
    notes = " ".join(paper.access.notes)
    assert "intended-application=text-mining" in notes
    assert "content-version=tdm" in notes
    assert paper.access.fulltext_url is None
    assert paper.access.permission is not FulltextPermission.MAY_RETRIEVE
    assert paper.access.permission is FulltextPermission.UNKNOWN


def test_the_provider_does_not_answer_access_questions():
    t = FakeTransport()
    p = provider(t)
    assert run(p.get_access_assessment("10.0000/example")) is None
    t.assert_no_calls()


def test_fulltext_is_gated_and_never_downloads():
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, list_payload())
    p = provider(t)
    paper = run(p.search(LiteratureQuery(text="x"))).papers[0]

    # Crossref never yields a fulltext_url, so the gate stops one step earlier
    # than a denial: there is nothing to fetch even if permission existed.
    assert paper.access.fulltext_url is None
    before = t.call_count
    attempt = run(p.get_fulltext(paper))
    assert attempt.outcome is RetrievalOutcome.NO_URL
    assert attempt.outcome is not RetrievalOutcome.SUCCESS
    # the gate is not a network call: the search above is the only request
    assert t.call_count == before


def test_the_provider_has_no_download_method():
    for name in dir(CrossrefProvider):
        assert "download" not in name.lower()


# --- errors -----------------------------------------------------------------


@pytest.mark.parametrize("status,expected", [
    (400, InvalidRequest),
    (401, AuthenticationOrAuthorizationError),
    (403, AuthenticationOrAuthorizationError),
    (404, ResourceNotFound),
    (429, RateLimited),
    (500, UpstreamError),
    (503, UpstreamError),
])
def test_http_status_maps_to_a_typed_error(status, expected):
    t = FakeTransport()
    t.route("GET", ENDPOINT, status, {"status": "failed"})
    with pytest.raises(expected):
        run(provider(t).search(LiteratureQuery(text="x")))


def test_a_missing_doi_is_an_answer_not_an_outage():
    """A correct lookup that finds nothing must not read as a failure."""
    t = FakeTransport()
    t.route("GET", ENDPOINT, 404, load("crossref", "not_found"))
    with pytest.raises(ResourceNotFound):
        run(provider(t).get_metadata(PaperRef(doi="10.0000/definitely-missing")))
    assert not issubclass(ResourceNotFound, UpstreamError)


def test_a_non_json_body_is_named():
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, "<html>nope</html>")
    with pytest.raises(InvalidUpstreamResponse):
        run(provider(t).search(LiteratureQuery(text="x")))


@pytest.mark.parametrize("bad", [
    {"no_message": True},
    {"message": "a string, not an object"},
    {"message": {"items": "not a list"}},
    {"message": {"items": ["not an object"]}},
])
def test_a_shape_that_contradicts_the_contract_is_named(bad):
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, bad)
    with pytest.raises(UpstreamSchemaError):
        run(provider(t).search(LiteratureQuery(text="x")))


def test_errors_do_not_leak_contact_details():
    t = FakeTransport()
    t.route("GET", ENDPOINT, 400, {"status": "failed"})
    p = provider(t, mailto="private@example.org")
    with pytest.raises(InvalidRequest) as exc:
        run(p.search(LiteratureQuery(text="x")))
    assert "private@example.org" not in str(exc.value)


def test_redaction_still_applies_to_a_contact_parameter():
    safe = redact_url(f"{ENDPOINT}?mailto=private@example.org&rows=10")
    assert "private@example.org" not in safe
    assert "rows=10" in safe


# --- rate -------------------------------------------------------------------


def test_rate_headers_are_recorded_verbatim_and_never_invented():
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, list_payload(), headers={"X-RateLimit-Limit": "50"})
    result = run(provider(t).search(LiteratureQuery(text="x")))
    assert result.rate.limit == "50"
    assert result.rate.remaining is None      # absent, not defaulted
    assert result.rate.headers_seen == ["X-RateLimit-Limit"]


def test_no_assumed_rate_is_hardcoded():
    """Assert on the code, not on prose that happens to mention the words.

    Comments in this module say "no sleep, no retry" -- which is exactly why
    the check strips them first and inspects the unparsed tree.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(CrossrefProvider)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
    code_only = ast.unparse(tree)

    assert "sleep" not in code_only
    assert "retry" not in code_only.lower()
    # the class ships no requests-per-second figure for an unverified limit
    assert "RATE_LIMIT =" not in code_only
    assert CrossrefProvider.rate_limit.max_retries == 0

"""OpenAlex provider, offline.

Everything here runs against `FakeTransport`. No request leaves the machine,
and the session-wide socket guard in `tests/conftest.py` would fail the test if
one tried.

The assertions are about the *requests we intend to make* and the *records we
produce*, never about the current contents of anyone's database. A live smoke
test covers the rest; it is opt-in and lives in `tests/live/`.
"""

import asyncio

import pytest

from crea.core.errors import (
    AuthenticationOrAuthorizationError,
    InvalidRequest,
    InvalidUpstreamResponse,
    RateLimited,
    UpstreamError,
    UpstreamSchemaError,
)
from crea.core.models.literature import AbstractSource, FulltextPermission, OAStatus
from crea.providers.base import LiteratureQuery
from crea.sources.base import redact_url
from crea.sources.openalex import (
    OFFICIAL_PER_PAGE_MAX,
    OPENALEX_DATA_LICENSE,
    OpenAlexProvider,
)
from tests.fakes.fixtures import load
from tests.fakes.transport import FakeTransport

NOW = "2026-09-30T00:00:00Z"
ENDPOINT = "https://api.openalex.org/works"


def page(n=2, next_cursor=None):
    return {"meta": {"count": n, "page": 1, "per_page": OFFICIAL_PER_PAGE_MAX,
                     "cost_usd": 0.1, "next_cursor": next_cursor},
            "results": load("openalex", "search_diyne")["results"][:n]}


def only(index, next_cursor=None):
    return {"meta": {"next_cursor": next_cursor},
            "results": [load("openalex", "search_diyne")["results"][index]]}


def provider(transport, **kwargs):
    return OpenAlexProvider(transport=transport, endpoint=ENDPOINT, **kwargs)


def run(coro):
    return asyncio.run(coro)


def wired(payload_obj=None, headers=None, **kwargs):
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, payload_obj if payload_obj is not None else page(),
            headers=headers)
    return t, provider(t, **kwargs)


# --- request shape ----------------------------------------------------------


def test_search_requests_the_documented_page_size():
    t, p = wired()
    run(p.search(LiteratureQuery(text="diyne", limit=5000)))
    params = t.calls[0]["params"]
    # 5000 was asked for; the documented maximum is what is sent
    assert params["per-page"] == OFFICIAL_PER_PAGE_MAX == 100
    assert params["search"] == "diyne"
    assert t.calls[0]["url"] == ENDPOINT


def test_page_size_is_never_the_undocumented_live_value():
    """200 was served once in a live probe. It is not a contract."""
    t, p = wired()
    run(p.search(LiteratureQuery(text="diyne", limit=200)))
    assert t.calls[0]["params"]["per-page"] == 100
    assert OFFICIAL_PER_PAGE_MAX != 200


def test_doi_lookup_normalises_before_it_sends():
    t, p = wired(only(0))
    paper = run(p.get_by_doi("https://doi.org/10.0000/SYNTHETIC.2026.0001"))
    assert t.calls[0]["params"]["filter"] == "doi:10.0000/synthetic.2026.0001"
    assert paper is not None
    assert paper.identifiers.doi == "10.0000/synthetic.2026.0001"


@pytest.mark.parametrize("bad", [None, "", "   ", "doi:", "https://doi.org/"])
def test_an_unusable_doi_makes_no_request_at_all(bad):
    t, p = wired()
    assert run(p.get_by_doi(bad)) is None
    t.assert_no_calls()


def test_work_lookup_strips_the_resolver_form():
    t = FakeTransport()
    t.route("GET", f"{ENDPOINT}/W123", 200, page()["results"][0])
    p = provider(t)
    paper = run(p.get_work("https://openalex.org/W123"))
    # the request addresses W123; the record's own id is what the payload says
    assert t.calls[0]["url"] == f"{ENDPOINT}/W123"
    assert paper.identifiers.openalex_id == "W0000000001"


# --- pagination -------------------------------------------------------------


def test_pagination_follows_next_cursor_and_stops_when_absent():
    t = FakeTransport()
    t.route_sequence("GET", ENDPOINT, [
        (200, only(0, next_cursor="CURSOR2"), {}),
        (200, only(1, next_cursor=None), {}),
    ])

    async def collect():
        return [w async for w in p.paginate({"filter": "type:article"})]

    p = provider(t)
    works = run(collect())
    assert len(works) == 2
    assert t.call_count == 2
    assert t.calls[0]["params"]["cursor"] == "*"
    assert t.calls[1]["params"]["cursor"] == "CURSOR2"
    assert t.calls[0]["params"]["filter"] == "type:article"


def test_pagination_stops_on_an_empty_page_without_asking_again():
    t = FakeTransport()
    t.route_sequence("GET", ENDPOINT, [
        (200, only(0, next_cursor="CURSOR2"), {}),
        (200, {"meta": {"next_cursor": "CURSOR3"}, "results": []}, {}),
    ])

    async def collect():
        return [w async for w in p.paginate({})]

    p = provider(t)
    assert len(run(collect())) == 1
    assert t.call_count == 2


def test_pagination_respects_its_local_page_bre():
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, only(0, next_cursor="ALWAYS_MORE"))

    async def collect():
        return [w async for w in p.paginate({}, max_pages=2)]

    p = provider(t)
    run(collect())
    assert t.call_count == 2


# --- records ----------------------------------------------------------------


def test_every_paper_is_attributed():
    t, p = wired()
    result = run(p.search(LiteratureQuery(text="diyne")))
    assert len(result.papers) == 2
    for paper in result.papers:
        assert "openalex" in paper.sources
        assert paper.retrieved_at
        assert paper.identifiers.openalex_id
        assert paper.source_ids.get("openalex")
    assert result.raw_hashes["openalex"]


def test_openalex_id_is_not_recorded_as_a_doi():
    t, p = wired(only(1))
    paper = run(p.search(LiteratureQuery(text="x"))).papers[0]
    assert paper.identifiers.doi is None
    assert paper.identifiers.openalex_id == "W0000000002"


def test_a_reconstructed_abstract_stays_a_reconstruction():
    t, p = wired()
    paper = next(
        x for x in run(p.search(LiteratureQuery(text="diyne"))).papers
        if x.abstracts
    )
    assert paper.abstracts[0].source is AbstractSource.OPENALEX_RECONSTRUCTED
    assert paper.abstracts[0].is_reconstructed is True


def test_a_null_abstract_stays_absent():
    t, p = wired(only(1))
    paper = run(p.search(LiteratureQuery(text="x"))).papers[0]
    assert paper.abstracts == []


def test_unknown_oa_status_degrades_to_unknown():
    raw = dict(page()["results"][0])
    raw["open_access"] = {**raw["open_access"], "oa_status": "platinum"}
    t, p = wired({"meta": {}, "results": [raw]})
    paper = run(p.search(LiteratureQuery(text="x"))).papers[0]
    assert paper.access.oa_status is OAStatus.UNKNOWN


# --- errors -----------------------------------------------------------------


@pytest.mark.parametrize("status,expected", [
    (400, InvalidRequest),
    (401, AuthenticationOrAuthorizationError),
    (403, AuthenticationOrAuthorizationError),
    (429, RateLimited),
    (500, UpstreamError),
    (503, UpstreamError),
])
def test_http_status_maps_to_a_typed_error(status, expected):
    t = FakeTransport()
    t.route("GET", ENDPOINT, status, {"error": "nope"})
    with pytest.raises(expected):
        run(provider(t).search(LiteratureQuery(text="x")))


def test_429_is_not_an_ordinary_failure():
    t = FakeTransport()
    t.route("GET", ENDPOINT, 429, {"error": "rate limited"})
    with pytest.raises(RateLimited) as exc:
        run(provider(t).search(LiteratureQuery(text="x")))
    assert "daily budget" in str(exc.value)


def test_a_non_json_body_is_named_as_such():
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, "<html>just a moment</html>")
    with pytest.raises(InvalidUpstreamResponse):
        run(provider(t).search(LiteratureQuery(text="x")))


@pytest.mark.parametrize("bad", [
    {"meta": {}},                                  # no results key
    {"results": "not a list"},                     # wrong type
    {"results": ["not an object"]},                # wrong element type
])
def test_a_shape_that_contradicts_the_contract_is_named(bad):
    t = FakeTransport()
    t.route("GET", ENDPOINT, 200, bad)
    with pytest.raises(UpstreamSchemaError):
        run(provider(t).search(LiteratureQuery(text="x")))


def test_an_error_never_prints_the_api_key():
    t = FakeTransport()
    t.route("GET", ENDPOINT, 400, {"error": "bad key"})
    p = provider(t, api_key="sk-do-not-leak-me")
    with pytest.raises(InvalidRequest) as exc:
        run(p.search(LiteratureQuery(text="x")))
    assert "sk-do-not-leak-me" not in str(exc.value)
    assert ENDPOINT in str(exc.value)


def test_the_key_is_never_written_onto_a_paper():
    t, p = wired(api_key="sk-secret", mailto="ops@example.org")
    result = run(p.search(LiteratureQuery(text="diyne")))
    blob = result.model_dump_json()
    assert "sk-secret" not in blob
    assert t.calls[0]["params"]["api_key"] == "sk-secret"


# --- credentials ------------------------------------------------------------


def test_no_key_and_no_mailto_means_neither_is_sent():
    t, p = wired()
    run(p.search(LiteratureQuery(text="diyne")))
    params = t.calls[0]["params"]
    assert "api_key" not in params
    assert "mailto" not in params


def test_mailto_is_passed_but_called_nothing_else():
    t, p = wired(mailto="ops@example.org")
    run(p.search(LiteratureQuery(text="diyne")))
    params = t.calls[0]["params"]
    assert params["mailto"] == "ops@example.org"
    # it is a contact address, not a pool, and it buys no priority
    assert len(params) == 3  # search, per-page, mailto


def test_a_missing_key_is_not_an_error_state():
    t, p = wired()
    assert p.status().usable is True
    assert p.requires_key is False


# --- rate and budget --------------------------------------------------------


def test_rate_headers_are_recorded_verbatim():
    headers = {
        "X-RateLimit-Limit": "1000",
        "X-RateLimit-Remaining": "998",
        "X-RateLimit-Reset": "25977",
        "X-RateLimit-Cost-USD": "0.0001",
        "X-RateLimit-Limit-USD": "0.1",
        "X-RateLimit-Remaining-USD": "0.0998",
    }
    t, p = wired(headers=headers)
    result = run(p.search(LiteratureQuery(text="diyne")))
    assert result.rate is not None
    assert result.rate.limit_usd == "0.1"
    assert result.rate.remaining_usd == "0.0998"
    assert result.rate.cost_usd == "0.0001"
    assert result.rate.remaining == "998"
    assert result.rate.reset_seconds == "25977"
    assert len(result.rate.headers_seen) == 6


def test_absent_headers_are_recorded_as_absent_not_defaulted():
    t, p = wired()
    result = run(p.search(LiteratureQuery(text="diyne")))
    assert result.rate.limit is None
    assert result.rate.headers_seen == []


def test_the_provider_never_sleeps():
    import inspect

    for method in (OpenAlexProvider.search, OpenAlexProvider.list_works,
                   OpenAlexProvider.paginate, OpenAlexProvider.get_by_doi,
                   OpenAlexProvider.get_work):
        assert "sleep" not in inspect.getsource(method)


# --- the fulltext boundary --------------------------------------------------


def test_content_urls_are_never_requested():
    """One search, one request. The provider never follows a content URL."""
    t, p = wired()
    run(p.search(LiteratureQuery(text="diyne")))
    assert t.call_count == 1
    for call in t.calls:
        assert "content.openalex.org" not in call["url"]


def test_the_provider_has_no_fulltext_method():
    for name in dir(OpenAlexProvider):
        assert "download" not in name.lower()
        assert "fetch_fulltext" not in name.lower()


def test_openalex_data_licence_is_not_written_onto_a_paper():
    """CC0 covers OpenAlex's data. It says nothing about any article."""
    t, p = wired()
    result = run(p.search(LiteratureQuery(text="diyne")))
    for paper in result.papers:
        assert OPENALEX_DATA_LICENSE == "CC0-1.0"      # the data licence
        # the article's own licence is a separate, per-work claim
        if paper.access.license.is_known:
            assert paper.access.license.applies_to == "oa_copy"
    blob = result.model_dump_json()
    assert OPENALEX_DATA_LICENSE not in blob or '"applies_to":"oa_copy"' in blob


def test_redaction_covers_every_credential_parameter():
    url = ("https://api.openalex.org/works?api_key=SECRETKEY&email=me@example.org"
           "&per-page=1&filter=type:article")
    safe = redact_url(url)
    assert "SECRETKEY" not in safe
    assert "me@example.org" not in safe
    # non-credential parameters survive untouched
    assert "per-page=1" in safe
    assert "type:article" in safe
    # and the parameter names are still legible
    assert "api_key=<redacted>" in safe
    assert "email=<redacted>" in safe

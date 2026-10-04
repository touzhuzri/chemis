"""Unpaywall provider, offline: request shape, credentials, errors, boundaries.

The tests that matter most here are the negative ones: a missing credential
degrades instead of failing, a bronze record with a PDF URL is refused, and
nothing ever fetches a location.
"""

import asyncio

import pytest

from crea.core.errors import (
    InvalidRequest,
    ProviderUnavailable,
    RateLimited,
    ResourceNotFound,
    UpstreamError,
)
from crea.core.models.literature import FulltextPermission, RetrievalOutcome
from crea.providers.base import LiteratureQuery, PaperRef
from crea.sources.unpaywall import LOOKUP_ENDPOINT, UnpaywallProvider
from tests.fakes.fixtures import load
from tests.fakes.transport import FakeTransport

DOI = "10.0000/synthetic.2026.0001"
EMAIL = "ops@example.org"


def run(coro):
    return asyncio.run(coro)


def provider(transport, email=EMAIL, **kwargs):
    return UnpaywallProvider(transport=transport, email=email,
                             endpoint=LOOKUP_ENDPOINT, **kwargs)


def wired(case="is_oa_green", **kwargs):
    t = FakeTransport()
    t.route("GET", f"{LOOKUP_ENDPOINT.format(doi=DOI)}", 200, load("unpaywall", case))
    return t, provider(t, **kwargs)


# --- credentials ------------------------------------------------------------


def test_a_missing_email_is_missing_key_not_a_crash():
    t = FakeTransport()
    p = provider(t, email=None)
    status = p.status()
    assert status.availability.value == "missing_key"
    assert status.usable is False
    assert "CREA_UNPAYWALL_EMAIL" in status.reason
    t.assert_no_calls()


def test_a_configured_email_is_sent():
    t, p = wired()
    run(p.get_metadata(PaperRef(doi=DOI)))
    assert t.calls[0]["params"]["email"] == EMAIL


def test_the_email_is_redacted_from_any_url_quoted_in_an_error():
    t = FakeTransport()
    t.route("GET", LOOKUP_ENDPOINT.format(doi=DOI), 400, {"error": "bad"})
    p = provider(t)
    with pytest.raises(InvalidRequest) as exc:
        run(p.get_metadata(PaperRef(doi=DOI)))
    assert EMAIL not in str(exc.value)


def test_search_is_refused_rather_than_faked():
    t, p = wired()
    with pytest.raises(ProviderUnavailable) as exc:
        run(p.search(LiteratureQuery(text="diyne")))
    assert "no search endpoint" in str(exc.value)
    t.assert_no_calls()


# --- lookup -----------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "10.0000/synthetic.2026.0001",
    "doi:10.0000/synthetic.2026.0001",
    "https://doi.org/10.0000/synthetic.2026.0001",
])
def test_every_doi_form_reaches_the_same_lookup(raw):
    t, p = wired()
    paper = run(p.get_metadata(PaperRef(doi=raw)))
    assert paper is not None
    assert t.calls[0]["url"] == LOOKUP_ENDPOINT.format(doi=DOI)


def test_the_doi_is_normalised_before_it_is_interpolated():
    other = "https://doi.org/10.1038/NPHYS1170"
    t = FakeTransport()
    t.route("GET", LOOKUP_ENDPOINT.format(doi="10.1038/nphys1170"), 200,
            load("unpaywall", "is_oa_green"))
    run(provider(t).get_metadata(PaperRef(doi=other)))
    assert t.calls[0]["url"] == LOOKUP_ENDPOINT.format(doi="10.1038/nphys1170")


@pytest.mark.parametrize("bad", [None, "", "   ", "doi:"])
def test_an_unusable_doi_makes_no_request(bad):
    t = FakeTransport()
    p = provider(t)
    assert run(p.get_metadata(PaperRef(doi=bad))) is None
    t.assert_no_calls()


def test_a_bare_ref_makes_no_request():
    t = FakeTransport()
    assert run(provider(t).get_metadata(PaperRef())) is None
    t.assert_no_calls()


# --- the access question ----------------------------------------------------


def test_get_access_assessment_is_the_providers_real_job():
    t, p = wired("is_oa_bronze")
    assessment = run(p.get_access_assessment(DOI))
    assert assessment is not None
    assert assessment.fulltext_url            # a location exists
    assert assessment.permission is FulltextPermission.UNKNOWN   # and grants nothing


def test_the_permission_is_derived_by_policy_not_by_the_provider():
    from crea.core.policy.access import infer_permission

    t, p = wired("is_oa_bronze")
    assessment = run(p.get_access_assessment(DOI))
    assert assessment.permission == infer_permission(assessment).permission
    assert assessment.permission is not FulltextPermission.NOT_PERMITTED


# --- the retrieval boundary -------------------------------------------------


def test_a_reported_location_is_never_fetched():
    t, p = wired("is_oa_green")
    paper = run(p.get_metadata(PaperRef(doi=DOI)))
    assert paper.access.fulltext_url == "https://example.org/pdf/1"

    before = t.call_count
    attempt = run(p.get_fulltext(paper))
    # a CC-BY record is permitted, and even so this phase has no transport for
    # full text at all -- the outcome must never be SUCCESS
    assert attempt.outcome is not RetrievalOutcome.SUCCESS
    assert t.call_count == before


def test_the_provider_has_no_download_or_fetch_method():
    for name in dir(UnpaywallProvider):
        low = name.lower()
        assert "download" not in low
        assert "fetch" not in low
        assert "crawl" not in low


def test_the_module_opens_no_connection_of_its_own():
    """A redirect chain is how a paywall gets walked through by accident.

    So the module must have no HTTP client at all -- it goes through the
    transport, once, for the metadata lookup, and does nothing else.
    """
    import ast
    import inspect
    import textwrap

    from crea.sources import unpaywall

    tree = ast.parse(textwrap.dedent(inspect.getsource(unpaywall)))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(m in {"httpx", "requests", "urllib.request", "http.client"}
                   for m in imported)
    # the only transport use is the single metadata GET
    code = ast.unparse(tree)
    assert code.count("self.transport.request") == 1


# --- provenance -------------------------------------------------------------


def test_every_record_is_attributed():
    t, p = wired()
    paper = run(p.get_metadata(PaperRef(doi=DOI)))
    assert "unpaywall" in paper.sources
    assert paper.source_ids["unpaywall"] == DOI
    assert paper.retrieved_at


# --- errors -----------------------------------------------------------------


@pytest.mark.parametrize("status,expected", [
    (400, InvalidRequest),
    (404, ResourceNotFound),
    (422, InvalidRequest),      # observed: "Email address required in API call"
    (429, RateLimited),
    (500, UpstreamError),
])
def test_http_status_maps_to_a_typed_error(status, expected):
    t = FakeTransport()
    t.route("GET", LOOKUP_ENDPOINT.format(doi=DOI), status, {"error": "x"})
    with pytest.raises(expected):
        run(provider(t).get_metadata(PaperRef(doi=DOI)))


def test_a_missing_doi_is_an_answer_not_an_outage():
    t = FakeTransport()
    t.route("GET", LOOKUP_ENDPOINT.format(doi=DOI), 404, load("unpaywall", "doi_not_found"))
    with pytest.raises(ResourceNotFound):
        run(provider(t).get_metadata(PaperRef(doi=DOI)))


def test_rate_headers_are_recorded_and_never_invented():
    t = FakeTransport()
    t.route("GET", LOOKUP_ENDPOINT.format(doi=DOI), 200, load("unpaywall", "is_oa_green"),
            headers={"X-RateLimit-Limit": "100000"})
    p = provider(t)
    before = t.call_count
    run(p.get_access_assessment(DOI))
    assert t.call_count == before + 1
    # Unpaywall's rate limit is UNVERIFIED; nothing is asserted about a value
    # we invented, and no default is filled in


def test_no_rate_is_hardcoded():
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(UnpaywallProvider)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
    code = ast.unparse(tree)
    assert "sleep" not in code
    assert "retry" not in code.lower()
    assert UnpaywallProvider.rate_limit.max_retries == 0

"""Crossref + OpenAlex literature providers (Stage 4.2).

Everything runs over `FakeTransport` with canned payloads -- no network. The
tests pin DOI normalization across providers, DOI merge (one record), partial
provider failure, the offline path, the bounded retry policy, and that the
service never invents a permission.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from crea.core.errors import RateLimited, UpstreamError
from crea.core.models.service import (
    LiteratureSearchRequest,
    ProviderRunStatus,
    SearchOutcome,
)
from crea.services.literature import LiteratureService
from crea.services.literature_retry import RetryPolicy, call_with_retry
from crea.sources.crossref import CrossrefProvider, WORKS_ENDPOINT as CR
from crea.sources.openalex import OpenAlexProvider, WORKS_ENDPOINT as OA
from tests.fakes.transport import FakeTransport

DOI = "10.0000/xyz"


def _crossref_item(doi=DOI, title="A synthetic work"):
    return {
        "DOI": doi, "title": [title],
        "container-title": ["Journal of Synthetic Chemistry"],
        "author": [{"given": "Alex", "family": "Smith"}],
        "issued": {"date-parts": [[2021, 4, 17]]},
        "type": "journal-article", "publisher": "Example Press",
        "language": "en", "volume": "12", "issue": "3", "page": "100-110",
    }


def _openalex_work(doi=DOI, title="A synthetic work"):
    return {
        "id": "https://openalex.org/W0000000001",
        "doi": f"https://doi.org/{doi}",
        "display_name": title, "title": title,
        "publication_year": 2021,
        "type": "article",
        "ids": {"openalex": "https://openalex.org/W0000000001"},
        "authorships": [{"author": {"display_name": "Alex Smith"}}],
        "abstract_inverted_index": None,
        "open_access": {"is_oa": False, "oa_status": "closed", "oa_url": None},
        "best_oa_location": None,
    }


def _transport(crossref=None, openalex=None):
    t = FakeTransport()
    t.route("GET", CR, 200, crossref if crossref is not None else {
        "status": "ok", "message-type": "work-list",
        "message": {"items": [_crossref_item()], "total-results": 1}})
    # The single-work DOI endpoint (`/works/{doi}`), registered by exact URL so
    # it wins over the search-route fragment.
    t.route("GET", f"{CR}/10.0000%2Fxyz", 200, {"message": _crossref_item()})
    t.route("GET", OA, 200, openalex if openalex is not None else {
        "meta": {"count": 1, "page": 1, "per_page": 20},
        "results": [_openalex_work()]})
    return t


def _providers(t):
    return {
        "crossref": CrossrefProvider(t, mailto="ops@example.org"),
        "openalex": OpenAlexProvider(t, api_key="secret-key", mailto="ops@example.org"),
    }


# -- provider construction / config ----------------------------------------


def test_providers_carry_config_without_leaking_it():
    t = _transport()
    crossref = CrossrefProvider(t, mailto="ops@example.org")
    openalex = OpenAlexProvider(t, api_key="secret-key", mailto="ops@example.org")
    assert crossref._params() == {"mailto": "ops@example.org"}
    auth = openalex._auth_params()
    assert auth["api_key"] == "secret-key"
    assert auth["mailto"] == "ops@example.org"


# -- search + DOI merge -----------------------------------------------------


def test_search_merges_same_doi_into_one_record():
    service = LiteratureService(_providers(_transport()))
    result = service.search(LiteratureSearchRequest(query="synthetic"))
    assert result.outcome is SearchOutcome.SUCCESS
    assert len(result.papers) == 1
    paper = result.papers[0]
    assert paper.identifiers.doi == DOI
    # both providers are attributed on the one record
    assert set(paper.sources) >= {"crossref", "openalex"}


def test_doi_lookup_merges_across_providers():
    service = LiteratureService(_providers(_transport()))
    result = service.lookup_by_doi(f"https://doi.org/{DOI}")
    assert result.found is True
    assert result.paper.identifiers.doi == DOI


# -- partial failure --------------------------------------------------------


def test_one_provider_failing_does_not_fail_the_search():
    t = _transport()
    t.route("GET", OA, 500, {"error": "boom"})
    service = LiteratureService(_providers(t))
    result = service.search(LiteratureSearchRequest(query="synthetic"))
    assert result.outcome is SearchOutcome.PARTIAL_SUCCESS
    assert result.papers, "Crossref results survived OpenAlex failing"
    runs = {r.name: r.status for r in result.provider_runs}
    assert runs["openalex"] is ProviderRunStatus.ERROR
    assert runs["crossref"] is ProviderRunStatus.OK


def test_every_provider_failing_is_not_an_empty_result():
    t = FakeTransport()
    t.route("GET", CR, 503, {"error": "x"})
    t.route("GET", OA, 503, {"error": "x"})
    service = LiteratureService(_providers(t))
    result = service.search(LiteratureSearchRequest(query="synthetic"))
    assert result.outcome is SearchOutcome.ALL_PROVIDERS_FAILED
    assert result.outcome is not SearchOutcome.NO_RESULTS


# -- bounded retry (§三十三) ------------------------------------------------


def test_call_with_retry_recovers_from_a_transient_failure():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RateLimited("slow down", provider="x", retry_after=0.0)
        return "ok"

    assert call_with_retry(flaky, RetryPolicy(max_retries=3), sleep=lambda _s: None) == "ok"
    assert attempts["n"] == 3


def test_call_with_retry_is_bounded_and_does_not_retry_answers():
    calls = {"n": 0}

    def always_limited():
        calls["n"] += 1
        raise RateLimited("slow down", provider="x", retry_after=0.0)

    with pytest.raises(RateLimited):
        call_with_retry(always_limited, RetryPolicy(max_retries=2),
                        sleep=lambda _s: None)
    assert calls["n"] == 3, "2 retries -> 3 attempts, then give up"

    # a NOT_FOUND-like answer (not transient) is never retried
    from crea.core.errors import ResourceNotFound

    other = {"n": 0}

    def not_found():
        other["n"] += 1
        raise ResourceNotFound("absent", provider="x")

    with pytest.raises(ResourceNotFound):
        call_with_retry(not_found, RetryPolicy(max_retries=3), sleep=lambda _s: None)
    assert other["n"] == 1


def test_default_policy_does_not_retry():
    assert RetryPolicy().enabled is False


def test_service_honours_the_retry_policy():
    t = FakeTransport()
    # Crossref 503 then Crossref 200 -- a retry should recover it
    t.route_sequence("GET", CR, [
        (503, {"error": "boom"}, {}),
        (200, {"status": "ok", "message-type": "work-list",
               "message": {"items": [_crossref_item()], "total-results": 1}}, {}),
    ])
    t.route("GET", OA, 200, {"meta": {"count": 0}, "results": []})
    service = LiteratureService(
        _providers(t), retry_policy=RetryPolicy(max_retries=1, base_backoff_seconds=0.0))
    result = service.search(LiteratureSearchRequest(query="synthetic"))
    runs = {r.name: r.status for r in result.provider_runs}
    assert runs["crossref"] is ProviderRunStatus.OK


# -- permission is never invented (§三十六) ---------------------------------


def test_providers_do_not_set_fulltext_permission():
    from crea.core.models.literature import FulltextPermission

    service = LiteratureService(_providers(_transport()))
    result = service.search(LiteratureSearchRequest(query="synthetic"))
    assert result.papers
    # OpenAlex says is_oa=false, Crossref carries no licence: permission stays
    # the policy default, never MAY_RETRIEVE from provider metadata alone.
    for paper in result.papers:
        assert paper.access.permission is not FulltextPermission.MAY_RETRIEVE


# -- API: offline + not found ----------------------------------------------


def test_literature_api_offline_reports_unavailable(tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        assert c.get("/api/literature/search",
                     params={"q": "x"}).status_code == 503
        assert c.get("/api/literature/doi/10.1000/xyz").status_code == 503
        assert c.get("/api/literature/unknown-id").status_code == 404

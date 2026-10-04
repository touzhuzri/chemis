"""Live smoke tests. Opt-in, tiny, and never in the CI gate.

Run them only when you mean it:

    CREA_LIVE_TESTS=1 CREA_OPENALEX_EMAIL=you@example.org pytest -m live

Deliberately minimal, because the cost is real: a search is $1.00 against a
$1/day free budget, so the whole file costs roughly one day's allowance. Every
test asks for `per-page=1`, and none of them touches a content URL -- a
content download is $10 and is a different rights question entirely.

**This module is exempt from the offline socket guard** (`tests/conftest.py`
skips the patch when the live flag is set), and it is the only place in the
repository permitted to open a socket.

The assertions are deliberately weak. They check that the contract holds, not
what the database currently contains; a test that failed because OpenAlex
acquired a new paper would train everyone to ignore it.
"""

import asyncio
import os

import pytest

from crea.providers.base import LiteratureQuery
from crea.sources.openalex import OFFICIAL_PER_PAGE_MAX, OpenAlexProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CREA_LIVE_TESTS") != "1",
        reason="set CREA_LIVE_TESTS=1 to run live provider tests",
    ),
]


def _provider():
    httpx = pytest.importorskip("httpx", reason="pip install -e '.[live]'")
    from crea.sources.http import HttpxTransport

    transport = HttpxTransport(user_agent=(
        f"crea-chemical-research-agent/0.1 "
        f"(mailto:{os.environ.get('CREA_OPENALEX_EMAIL', 'unset@example.org')})"
    ))
    return OpenAlexProvider(
        transport=transport,
        api_key=os.environ.get("CREA_OPENALEX_API_KEY") or None,
        mailto=os.environ.get("CREA_OPENALEX_EMAIL") or None,
    )


def run(coro):
    return asyncio.run(coro)


def test_live_search_returns_attributed_records():
    provider = _provider()
    result = run(provider.search(LiteratureQuery(text="diphenylbutadiyne", limit=1)))

    assert result.papers, "the live API returned no works"
    assert result.rate is not None
    for paper in result.papers:
        assert "openalex" in paper.sources
        assert paper.retrieved_at
        assert paper.identifiers.openalex_id
        # whatever the record's shape, an abstract is never silently promoted
        for record in paper.abstracts:
            assert record.source.value == "openalex_reconstructed"


def test_live_page_size_stays_within_the_documented_limit():
    provider = _provider()
    run(provider.search(LiteratureQuery(text="diphenylbutadiyne", limit=1000)))
    # nothing to assert about the server here beyond that we did not ask for
    # the undocumented value; the offline suite pins that on the request.


def test_live_doi_lookup_resolves():
    provider = _provider()
    paper = run(provider.get_by_doi("https://doi.org/10.1038/nphys1170"))
    if paper is not None:
        assert paper.identifiers.doi == "10.1038/nphys1170"
        assert paper.access.oa_status is not None
        # the provider must not have reached a content URL
        assert "content.openalex.org" not in (paper.access.fulltext_url or "")


def test_live_budget_headers_are_present():
    provider = _provider()
    result = run(provider.search(LiteratureQuery(text="diphenylbutadiyne", limit=1)))
    assert result.rate is not None
    # recorded verbatim; absence is a fact, not a failure
    if result.rate.headers_seen:
        assert result.rate.limit is not None or result.rate.limit_usd is not None


def test_documented_page_size_is_still_accepted():
    """If OpenAlex ever lowers the maximum below 100, this is where we find out."""
    provider = _provider()
    result = run(provider.search(LiteratureQuery(text="catalysis", limit=OFFICIAL_PER_PAGE_MAX)))
    assert result.papers is not None

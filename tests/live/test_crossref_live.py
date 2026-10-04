"""Crossref live smoke tests. Opt-in, tiny, and never in the CI gate.

    CREA_LIVE_TESTS=1 pytest -m live

Two requests at most, and no pagination, no ranking, no result-count
assertions, no link fetching and no TDM anything. Crossref publishes no
retrieval endpoint we would be entitled to use, and this file is careful not
to look like it is going to try.

The assertions check that the *contract* holds -- that a DOI lookup resolves,
that the envelope has the shape the parser expects -- and never what the
database currently contains. A live test that fails because Crossref acquired
a paper teaches everyone to ignore it.
"""

import asyncio
import os

import pytest

from crea.providers.base import LiteratureQuery, PaperRef
from crea.sources.crossref import CrossrefProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CREA_LIVE_TESTS") != "1",
        reason="set CREA_LIVE_TESTS=1 to run live provider tests",
    ),
]

#: A long-standing, well-known DOI. Used because it will exist for ever, not
#: because it is interesting chemistry.
KNOWN_DOI = "10.1038/nphys1170"


def _provider():
    pytest.importorskip("httpx", reason="pip install -e '.[live]'")
    from crea.sources.http import HttpxTransport

    return CrossrefProvider(
        transport=HttpxTransport(user_agent="crea-chemical-research-agent/0.1"),
        mailto=os.environ.get("CREA_CROSSREF_EMAIL") or None,
    )


def run(coro):
    return asyncio.run(coro)


def test_live_doi_lookup_resolves_and_parses():
    paper = run(_provider().get_metadata(PaperRef(doi=KNOWN_DOI)))
    assert paper is not None
    assert paper.identifiers.doi == KNOWN_DOI
    assert paper.title
    assert paper.retrieved_at
    assert "crossref" in paper.sources
    # the parser's date priority still finds something on a real record
    assert paper.year is not None
    # and a real abstract, if present, is a CROSSREF record
    for record in paper.abstracts:
        assert record.source.value == "crossref"
        assert record.is_reconstructed is False


def test_live_record_yields_no_fulltext_permission():
    """A real Crossref record with a TDM link and a TDM licence grants nothing."""
    paper = run(_provider().get_metadata(PaperRef(doi=KNOWN_DOI)))
    assert paper.access.fulltext_url is None
    assert paper.access.permission.value != "may_retrieve"
    assert paper.access.permission.value in ("unknown", "not_permitted")


def test_live_minimal_search_returns_attributed_records():
    result = run(_provider().search(
        LiteratureQuery(text="diphenylbutadiyne", limit=1)))
    assert result.retrieved_at
    for paper in result.papers:
        assert "crossref" in paper.sources
        assert paper.retrieved_at

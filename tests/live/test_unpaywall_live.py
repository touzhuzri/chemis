"""Unpaywall live smoke test. Opt-in, one DOI, no retrieval.

    CREA_LIVE_TESTS=1 CREA_UNPAYWALL_EMAIL=you@example.org pytest -m live

Two conditions, both of which must hold or the test skips:

* `CREA_LIVE_TESTS=1` -- the opt-in
* `CREA_UNPAYWALL_EMAIL` -- Unpaywall requires it and answers HTTP 422 without
  it. A missing email is a *skip*, not a failure: the environment simply has not
  opted in, and failing would train people to ignore red tests.

The assertions are about the contract -- that a known DOI resolves, that the
response carries the fields the parser reads, that nothing was retrieved. Never
about what the record currently says: a `gold` record becoming `green` is not a
regression in our code.
"""

import asyncio
import os

import pytest

from crea.providers.base import PaperRef
from crea.sources.unpaywall import LOOKUP_ENDPOINT, UnpaywallProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CREA_LIVE_TESTS") != "1",
        reason="set CREA_LIVE_TESTS=1 to run live provider tests",
    ),
    pytest.mark.skipif(
        not os.environ.get("CREA_UNPAYWALL_EMAIL"),
        reason="Unpaywall requires CREA_UNPAYWALL_EMAIL; skipping rather than failing",
    ),
]

#: A long-standing, well-known DOI. Used because it will exist for ever, not
#: because it is interesting chemistry.
KNOWN_DOI = "10.1038/nphys1170"


def _provider() -> UnpaywallProvider:
    pytest.importorskip("httpx", reason="pip install -e '.[live]'")
    from crea.sources.http import HttpxTransport

    return UnpaywallProvider(
        transport=HttpxTransport(user_agent="crea-chemical-research-agent/0.1"),
        email=os.environ["CREA_UNPAYWALL_EMAIL"],
    )


def run(coro):
    return asyncio.run(coro)


def test_live_known_doi_resolves_and_parses():
    paper = run(_provider().get_metadata(PaperRef(doi=KNOWN_DOI)))
    assert paper is not None
    assert paper.identifiers.doi == KNOWN_DOI
    assert "unpaywall" in paper.sources
    assert paper.retrieved_at
    # the fields the parser reads are present on a real record
    assert paper.access.oa_status is not None
    assert paper.access.checked_at


def test_live_a_known_doi_never_comes_back_unattributed():
    paper = run(_provider().get_metadata(PaperRef(doi=KNOWN_DOI)))
    assert paper.source_ids.get("unpaywall")


def test_live_record_produces_no_permission_we_cannot_justify():
    """Whatever the record says, the answer must be one the policy can defend."""
    from crea.core.models.literature import FulltextPermission
    from crea.core.policy.access import infer_permission
    from crea.services.access_reconciliation import evidence_from_paper, reconcile

    paper = run(_provider().get_metadata(PaperRef(doi=KNOWN_DOI)))
    result = reconcile([evidence_from_paper(paper)])
    permission = result.selected_assessment["permission"]

    if permission == FulltextPermission.MAY_RETRIEVE.value:
        # permitted only on an explicit permissive licence, never on a URL
        assert paper.access.license.spdx_id
        assert paper.access.license.is_known
    else:
        # Both are honest refusals: UNKNOWN is "we could not establish
        # permission", NOT_PERMITTED is "every source says it is closed".
        assert permission in (FulltextPermission.UNKNOWN.value,
                             FulltextPermission.NOT_PERMITTED.value)
        assert infer_permission(paper.access).permission.value in (
            "unknown", "not_permitted")


def test_live_unknown_doi_is_an_answer_not_a_crash():
    from crea.core.errors import ResourceNotFound

    with pytest.raises(ResourceNotFound):
        run(_provider().get_metadata(PaperRef(doi="10.0000/crea-definitely-missing")))

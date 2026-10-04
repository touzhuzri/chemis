"""Fulltext access policy + Unpaywall aggregation (Stage 4.3).

Fail-closed: a URL is not a permission, an OA classification is not a licence,
and NOT_PERMITTED / MAY_RETRIEVE each require explicit evidence. Everything
runs over `FakeTransport` -- no network, no PDF, no OCR.
"""

from __future__ import annotations

import pathlib
import tempfile

from crea.core.models.literature import (
    AccessAssessment,
    AccessStatus,
    FulltextPermission,
    LicenseInfo,
    OAStatus,
    PermissionBasis,
)
from crea.core.policy.access import (
    ACCESS_POLICY_VERSION,
    infer_permission,
    license_is_permissive,
    may_retrieve,
)


# -- Rule A/B: MAY_RETRIEVE only on explicit, applicable evidence -----------


def test_permissive_licence_on_an_open_work_may_retrieve():
    result = infer_permission(AccessAssessment(
        oa_status=OAStatus.GOLD,
        license=LicenseInfo(spdx_id="cc-by-4.0", name="CC-BY-4.0",
                            applies_to="oa_copy", is_known=True, stated_by=["unpaywall"])))
    assert result.permission is FulltextPermission.MAY_RETRIEVE
    assert result.permission_basis is PermissionBasis.LICENSE
    assert result.policy_version == ACCESS_POLICY_VERSION
    assert any(e.kind == "open_license" for e in result.access_evidence)
    assert result.reasons


def test_unknown_licence_on_a_gold_work_is_not_permission():
    result = infer_permission(AccessAssessment(oa_status=OAStatus.GOLD))
    assert result.permission is FulltextPermission.UNKNOWN


def test_unversioned_cc_by_is_treated_as_stated():
    # providers may name the family without a version; an unstated version
    # stays unstated, but the family is recognised
    assert license_is_permissive(LicenseInfo(spdx_id="cc-by", is_known=True))


# -- Rule C: URL / OA status alone is not permission ------------------------


def test_a_pdf_url_alone_is_not_permission():
    result = infer_permission(AccessAssessment(
        oa_status=OAStatus.UNKNOWN,
        fulltext_url="https://example.org/paper.pdf", url_type="pdf"))
    assert result.permission is FulltextPermission.UNKNOWN
    assert any(e.kind == "url_only" for e in result.access_evidence)
    assert may_retrieve(result) is False


def test_open_access_status_alone_is_not_permission():
    for status in (OAStatus.GREEN, OAStatus.HYBRID, OAStatus.BRONZE, OAStatus.DIAMOND):
        result = infer_permission(AccessAssessment(oa_status=status))
        assert result.permission is FulltextPermission.UNKNOWN, status


def test_bronze_with_a_landing_page_stays_unknown():
    result = infer_permission(AccessAssessment(
        oa_status=OAStatus.BRONZE,
        fulltext_url="https://publisher.example.org/article",
        url_type="landing_page"))
    assert result.permission is FulltextPermission.UNKNOWN


# -- Rule D: NOT_PERMITTED only on explicit closed evidence -----------------


def test_closed_status_is_not_permitted():
    result = infer_permission(AccessAssessment(oa_status=OAStatus.CLOSED))
    assert result.permission is FulltextPermission.NOT_PERMITTED
    assert result.permission_basis is PermissionBasis.OA_STATUS_CLOSED


def test_unknown_never_becomes_not_permitted_from_absence_of_a_url():
    result = infer_permission(AccessAssessment(oa_status=OAStatus.UNKNOWN))
    assert result.permission is FulltextPermission.UNKNOWN
    assert result.permission is not FulltextPermission.NOT_PERMITTED


# -- Unpaywall aggregation over the provider --------------------------------


def _unpaywall_client(payload, *, email="ops@example.org"):
    from crea.sources.unpaywall import UnpaywallProvider
    from tests.fakes.transport import FakeTransport

    transport = FakeTransport()
    transport.route("GET", "api.unpaywall.org/v2/", 200, payload)
    return UnpaywallProvider(transport, email=email), transport


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_unpaywall_green_without_licence_is_unknown():
    provider, _ = _unpaywall_client({
        "doi": "10.0000/xyz", "is_oa": True, "oa_status": "green",
        "best_oa_location": {"url_for_pdf": "https://repo.example.org/x.pdf",
                             "host_type": "repository", "version": "acceptedVersion",
                             "license": None},
        "oa_locations": []})
    assessment = _run(provider.get_access_assessment("10.0000/xyz"))
    assert assessment.oa_status is OAStatus.GREEN
    assert assessment.fulltext_url.endswith(".pdf")
    # a repository PDF with license=null is a location, not a permission
    assert assessment.permission is FulltextPermission.UNKNOWN


def test_unpaywall_requires_an_email_and_degrades_cleanly():
    from crea.core.errors import ProviderUnavailable
    from crea.sources.unpaywall import UnpaywallProvider
    from tests.fakes.transport import FakeTransport

    provider = UnpaywallProvider(FakeTransport(), email=None)
    assert provider.status().usable is False
    import pytest

    with pytest.raises(ProviderUnavailable):
        _run(provider.get_access_assessment("10.0000/xyz"))


def test_unpaywall_has_no_search_endpoint():
    import pytest

    from crea.core.errors import ProviderUnavailable
    from crea.providers.base import LiteratureQuery

    provider, _ = _unpaywall_client({"doi": "10.0000/xyz"})
    with pytest.raises(ProviderUnavailable):
        _run(provider.search(LiteratureQuery(text="anything")))


# -- API: offline is a stated unavailability, not a crash -------------------


def test_access_api_offline_reports_unavailable(tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        assert c.get("/api/literature/doi/10.1000/xyz/access").status_code == 503

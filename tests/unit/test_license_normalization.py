"""Licence vocabulary crossing a provider boundary.

The one thing this file must not do is invent an identifier. `public-domain` is
OpenAlex's own slug; the SPDX identifier for CC0 is `CC0-1.0`, confirmed
against the SPDX licence list. The provider maps between them, keeps the raw
value, and never claims a version the upstream did not state.
"""

import pytest

from crea.core.models.literature import LicenseInfo, OAStatus
from crea.core.policy.access import infer_permission, license_is_permissive
from crea.sources.openalex import (
    OPENALEX_DATA_LICENSE,
    OPENALEX_LICENSE_TO_SPDX,
    parse_work,
)


def work_with_license(raw_license):
    return {
        "id": "https://openalex.org/W1",
        "doi": "https://doi.org/10.1/x",
        "display_name": "T",
        "open_access": {"is_oa": True, "oa_status": "gold"},
        "best_oa_location": {
            "landing_page_url": "https://example.org/l",
            "pdf_url": None,
            "license": raw_license,
            "version": "publishedVersion",
        },
    }


def test_public_domain_maps_to_the_real_spdx_identifier():
    paper = parse_work(work_with_license("public-domain"))
    assert paper.access.license.spdx_id == "CC0-1.0"
    assert license_is_permissive(paper.access.license) is True


def test_the_raw_provider_value_is_not_discarded():
    paper = parse_work(work_with_license("public-domain"))
    assert paper.access.license.name == "public-domain"
    assert "openalex" in paper.access.license.stated_by


def test_public_domain_is_not_itself_an_spdx_identifier():
    # The slug and the standard identifier are different things, and the slug
    # is not written into the SPDX field.
    assert "public-domain" not in OPENALEX_LICENSE_TO_SPDX.values()
    assert OPENALEX_LICENSE_TO_SPDX["public-domain"] == "CC0-1.0"


def test_cc_families_are_not_given_a_version_nobody_stated():
    paper = parse_work(work_with_license("cc-by"))
    # OpenAlex names the family, not a version. Inventing CC-BY-4.0 would be
    # asserting something upstream never said.
    assert paper.access.license.spdx_id == "CC-BY"
    assert paper.access.license.spdx_id != "CC-BY-4.0"
    assert paper.access.license.content_version == "UNKNOWN"


def test_a_cc0_licence_is_permissive_end_to_end():
    """OpenAlex CC0 -> normalised permissive licence -> MAY_RETRIEVE."""
    paper = parse_work(work_with_license("public-domain"))
    assert paper.access.license.is_known is True
    assert license_is_permissive(paper.access.license) is True

    decided = infer_permission(paper.access)
    assert decided.permission.value == "may_retrieve"
    assert decided.permission_basis.value == "license"


def test_no_licence_stays_unknown_even_with_a_url_and_a_status():
    paper = parse_work(work_with_license(None))
    assert paper.access.license.is_known is False
    assert paper.access.license.spdx_id is None
    decided = infer_permission(paper.access)
    assert decided.permission.value == "unknown"
    assert decided.permission.value != "not_permitted"


def test_an_unrecognised_licence_string_is_not_guessed_at():
    paper = parse_work(work_with_license("some-publisher-internal-licence"))
    assert paper.access.license.spdx_id is None
    assert paper.access.license.is_known is False
    # the raw string is still recorded, so nothing is silently lost
    assert paper.access.license.name == "some-publisher-internal-licence"
    assert infer_permission(paper.access).permission.value == "unknown"


def test_the_data_licence_is_not_the_article_licence():
    """The distinction this test exists to hold.

    OpenAlex's *data* is CC0. That is a fact about the metadata feed. It says
    nothing about any article, and in particular it is not a licence for the
    article's PDF.
    """
    assert OPENALEX_DATA_LICENSE == "CC0-1.0"

    # a paper whose own location carries no licence stays unknown, even though
    # the data it came from is CC0
    paper = parse_work(work_with_license(None))
    assert infer_permission(paper.access).permission.value == "unknown"

    # and the data licence never appears as an article claim
    assert paper.access.license.spdx_id is None
    assert paper.access.license.applies_to == "oa_copy"


def test_a_license_without_its_known_flag_is_never_permissive():
    lie = LicenseInfo(spdx_id="CC0-1.0", is_known=False)
    assert license_is_permissive(lie) is False
    honest = LicenseInfo(spdx_id="CC0-1.0", is_known=True)
    assert license_is_permissive(honest) is True


@pytest.mark.parametrize("slug,spdx", sorted(OPENALEX_LICENSE_TO_SPDX.items()))
def test_every_mapped_slug_is_lowercase_and_unversioned_where_it_should_be(slug, spdx):
    import re

    assert slug == slug.lower()
    if slug == "public-domain":
        return
    # A family name carries no version. `CC-BY` and `CC-BY-SA` both contain
    # hyphens; what must be absent is a trailing version number, because
    # OpenAlex never stated one.
    assert spdx == spdx.upper()
    assert not re.search(r"-\d+(\.\d+)?$", spdx), f"{spdx} asserts a version nobody gave us"

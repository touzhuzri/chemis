"""Offline integration: provider parsing, provenance, merge conflicts, gate.

Every test here runs under the session-wide network guard in `conftest.py`, so
a stray request is not a style issue -- it is impossible.
"""

import asyncio

import pytest

from crea.core.models.literature import (
    AbstractSource,
    AccessStatus,
    FulltextPermission,
    OAStatus,
    RetrievalOutcome,
)
from crea.core.policy.access import abstract_admissible_as_primary_evidence
from crea.providers.base import LiteratureQuery, PaperRef
from crea.services.dedup import deduplicate
from tests.fakes.fixtures import (
    FixtureProvider,
    meta,
    parse_crossref_message,
    load,
    payload_hash,
    reconstruct_abstract,
)
from tests.fakes.transport import FakeTransport

NOW = "2026-09-30T00:00:00Z"


def run(coro):
    return asyncio.run(coro)


# --- provider parsing -------------------------------------------------------


def test_openalex_search_parses_into_attributed_papers():
    provider = FixtureProvider("openalex", "openalex/search_diyne", NOW)
    result = run(provider.search(LiteratureQuery(text="1,4-diphenyl-1,3-diyne synthesis")))

    assert len(result.papers) == 3
    for paper in result.papers:
        assert "openalex" in paper.sources          # nothing is unattributed
        assert paper.retrieved_at == NOW
    assert result.raw_hashes["openalex"] == payload_hash(load("openalex", "search_diyne"))
    assert result.complete is True


def test_a_reconstructed_abstract_is_tagged_as_a_reconstruction():
    provider = FixtureProvider("openalex", "openalex/search_diyne", NOW)
    result = run(provider.search(LiteratureQuery(text="diyne")))
    with_abstract = next(p for p in result.papers if p.abstracts)

    record = with_abstract.abstract
    assert record.source is AbstractSource.OPENALEX_RECONSTRUCTED
    assert record.is_reconstructed is True
    assert abstract_admissible_as_primary_evidence(record) is False
    # and the reconstruction itself is correct
    assert record.text.startswith("A glucokinase regulator was selected")
    assert reconstruct_abstract({"Beta": [1], "alpha": [0]}) == "alpha Beta"


def test_a_publisher_abstract_is_admissible_as_primary():
    provider = FixtureProvider("crossref", "crossref/doi_metadata", NOW)
    paper = run(provider.get_metadata(PaperRef(doi="10.0000/synthetic.2026.0001")))
    assert paper is not None
    record = paper.abstract
    assert record.source is AbstractSource.CROSSREF
    assert abstract_admissible_as_primary_evidence(record) is True


def test_a_work_without_a_doi_still_parses():
    provider = FixtureProvider("openalex", "openalex/search_diyne", NOW)
    result = run(provider.search(LiteratureQuery(text="diyne")))
    no_doi = next(p for p in result.papers if p.identifiers.doi is None)
    assert no_doi.paper_id
    assert no_doi.title.startswith("Sonogashira")


# --- access reporting -------------------------------------------------------


def test_green_with_cc_by_becomes_retrievable():
    provider = FixtureProvider("unpaywall", "unpaywall/is_oa_green", NOW)
    assessment = run(provider.get_access_assessment("10.0000/synthetic.2026.0001"))
    assert assessment.oa_status is OAStatus.GREEN
    assert assessment.permission is FulltextPermission.MAY_RETRIEVE
    assert assessment.access_status is AccessStatus.AVAILABLE
    assert assessment.license.spdx_id == "cc-by"
    assert assessment.source == "unpaywall"


def test_bronze_with_a_pdf_url_stays_unknown():
    """The correction that matters most, exercised end to end."""
    provider = FixtureProvider("unpaywall", "unpaywall/is_oa_bronze", NOW)
    assessment = run(provider.get_access_assessment("10.0000/synthetic.2020.0003"))
    assert assessment.oa_status is OAStatus.BRONZE
    assert assessment.fulltext_url == "https://example.org/pdf/3"   # a location exists
    assert assessment.permission is FulltextPermission.UNKNOWN       # and grants nothing
    assert assessment.permission.value != "may_retrieve"
    assert assessment.permission.value != "not_permitted"


def test_closed_and_not_found_behave_differently():
    closed = FixtureProvider("unpaywall", "unpaywall/is_oa_closed", NOW)
    assessment = run(closed.get_access_assessment("10.0000/synthetic.2019.0002"))
    assert assessment.oa_status is OAStatus.CLOSED
    assert assessment.fulltext_url is None

    missing = FixtureProvider("unpaywall", "unpaywall/doi_not_found", NOW)
    assert run(missing.get_access_assessment("10.0000/nope")) is None


def test_a_provider_reports_but_does_not_decide():
    """`report_access` is the whole separation, under test."""
    from crea.core.models.literature import AccessAssessment
    provider = FixtureProvider("unpaywall", "unpaywall/is_oa_green", NOW)

    # a provider handed a raw assessment gets policy applied to it...
    decided = provider.report_access(AccessAssessment(oa_status=OAStatus.BRONZE))
    assert decided.permission is FulltextPermission.UNKNOWN
    # ...and the provider is the one that recorded where the answer came from
    assert decided.source == "unpaywall"


# --- the retrieval gate, and the negative test that matters -----------------


def test_the_gate_performs_no_transport_call_for_a_denied_paper():
    transport = FakeTransport()
    provider = FixtureProvider("unpaywall", "unpaywall/is_oa_bronze", NOW)
    provider.transport = transport

    paper = parse_crossref_message(load("crossref", "doi_metadata"), NOW)
    paper.access = run(provider.get_access_assessment("10.0000/synthetic.2020.0003"))

    attempt = run(provider.get_fulltext(paper))
    assert attempt.outcome is RetrievalOutcome.PERMISSION_DENIED
    transport.assert_no_calls()      # the point of the test


def test_the_gate_still_withholds_when_permission_is_granted():
    transport = FakeTransport()
    provider = FixtureProvider("unpaywall", "unpaywall/is_oa_green", NOW)
    provider.transport = transport

    paper = parse_crossref_message(load("crossref", "doi_metadata"), NOW)
    paper.access = run(provider.get_access_assessment("10.0000/synthetic.2026.0001"))
    assert paper.access.permission is FulltextPermission.MAY_RETRIEVE

    attempt = run(provider.get_fulltext(paper))
    assert attempt.outcome is RetrievalOutcome.NOT_ATTEMPTED
    assert "transport is still required" in attempt.note
    transport.assert_no_calls()


# --- merge conflicts and provenance ----------------------------------------


def test_metadata_authority_wins_and_the_disagreement_is_recorded():
    openalex = run(FixtureProvider(
        "openalex", "openalex/search_diyne", NOW
    ).search(LiteratureQuery(text="diyne"))).papers[0]
    crossref = run(FixtureProvider(
        "crossref", "crossref/doi_metadata", NOW
    ).get_metadata(PaperRef(doi="10.0000/synthetic.2026.0001")))

    assert openalex.title != crossref.title      # the fixture disagrees on purpose
    result = deduplicate([openalex, crossref])
    assert len(result.papers) == 1
    merged = result.papers[0]

    assert merged.title == crossref.title        # crossref is the authority
    assert set(merged.sources) == {"openalex", "crossref"}
    assert any(c.startswith("title:") for c in merged.merge_conflicts)
    assert result.stats.exact_doi_merges == 1


def test_merged_paper_keeps_both_abstracts_and_prefers_the_real_one():
    openalex = run(FixtureProvider(
        "openalex", "openalex/search_diyne", NOW
    ).search(LiteratureQuery(text="diyne"))).papers[0]
    crossref = run(FixtureProvider(
        "crossref", "crossref/doi_metadata", NOW
    ).get_metadata(PaperRef(doi="10.0000/synthetic.2026.0001")))

    merged = deduplicate([openalex, crossref]).papers[0]
    assert len(merged.abstracts) == 2
    assert merged.abstract.source is AbstractSource.CROSSREF
    assert any(a.is_reconstructed for a in merged.abstracts)   # the reconstruction is not lost


def test_every_merged_paper_keeps_its_provenance():
    openalex = run(FixtureProvider(
        "openalex", "openalex/search_diyne", NOW
    ).search(LiteratureQuery(text="diyne"))).papers
    crossref = [run(FixtureProvider(
        "crossref", "crossref/doi_metadata", NOW
    ).get_metadata(PaperRef(doi="10.0000/synthetic.2026.0001")))]
    for paper in deduplicate([*openalex, *crossref]).papers:
        assert paper.sources
        assert paper.retrieved_at == NOW


def test_a_crossref_licence_and_an_unpaywall_licence_are_both_kept():
    openalex = run(FixtureProvider(
        "openalex", "openalex/search_diyne", NOW
    ).search(LiteratureQuery(text="diyne"))).papers[0]
    assert openalex.access.license.applies_to == "oa_copy"

    crossref = run(FixtureProvider(
        "crossref", "crossref/doi_metadata", NOW
    ).get_metadata(PaperRef(doi="10.0000/synthetic.2026.0001")))
    assert crossref.access.license.applies_to == "published_article"


# --- fixture honesty --------------------------------------------------------


@pytest.mark.parametrize(
    "provider,case",
    [
        ("openalex", "search_diyne"),
        ("crossref", "doi_metadata"),
        ("unpaywall", "is_oa_green"),
        ("unpaywall", "is_oa_closed"),
        ("unpaywall", "is_oa_bronze"),
        ("unpaywall", "doi_not_found"),
    ],
)
def test_every_fixture_declares_its_values_synthetic(provider, case):
    """A fixture that claimed to be a real recording would be the actual hazard.

    Two things are tracked separately, because they have different truth values:
    the *values* are hand-authored and always will be, while the *schema* was
    read off a live response. Verified schema does not make synthetic values
    real, and a fixture is never a recording.
    """
    m = meta(provider, case)
    assert m["values"] == "SYNTHETIC"
    assert m["is_recorded_response"] is False
    assert m["contains_full_text"] is False
    assert m["schema"] in ("LIVE_VERIFIED", "UNVERIFIED")
    assert m["schema_verified_at"] == "2026-09-30"
    assert m["schema_source"].startswith("docs/PHASE2_PROVIDER_CONTRACT.md")
    # anything not verified is written down rather than left implied
    assert isinstance(m["schema_unverified"], list)


@pytest.mark.parametrize(
    "provider,case",
    [
        ("openalex", "search_diyne"),
        ("crossref", "doi_metadata"),
        ("unpaywall", "is_oa_green"),
    ],
)
def test_recording_fields_are_present_even_when_they_are_null(provider, case):
    """A future recorded response must be able to fill these; the schema is uniform.

    `recorded_at` and `license_status` are present and null/NA here because
    nothing in this repository is a recording. A fixture that omitted the keys
    would let a recorded response be added later without anyone deciding
    whether its licence permits committing it.
    """
    m = meta(provider, case)
    assert "recorded_at" in m
    assert "license_status" in m
    assert m["recorded_at"] is None
    assert isinstance(m["license_status"], str) and m["license_status"]


@pytest.mark.parametrize(
    "provider,case",
    [
        ("openalex", "search_diyne"),
        ("crossref", "doi_metadata"),
        ("unpaywall", "is_oa_green"),
        ("unpaywall", "is_oa_closed"),
        ("unpaywall", "is_oa_bronze"),
        ("unpaywall", "doi_not_found"),
    ],
)
def test_every_fixture_names_what_remains_unverified(provider, case):
    """A schema cannot be wholly verified; the gaps have to be written down."""
    for entry in meta(provider, case)["schema_unverified"]:
        assert isinstance(entry, str) and entry.strip()


# --- OpenAlex location field names (verified 2026-09-30) -------------------


def test_openalex_uses_landing_page_url_and_pdf_url():
    """Regression: these were once read as Unpaywall's `url` / `url_for_pdf`.

    OpenAlex has no `url` or `url_for_pdf` in its location object. Reading them
    would be inferring one provider's schema from another's, and it silently
    produced a null full-text URL for every real record.
    """
    result = run(FixtureProvider("openalex", "openalex/search_diyne", NOW)
                 .search(LiteratureQuery(text="diyne")))
    oa_record = next(p for p in result.papers
                     if p.identifiers.doi == "10.0000/synthetic.2026.0001")

    assert oa_record.access.fulltext_url == "https://example.org/pdf/1"
    assert oa_record.access.url_type == "pdf"
    # the landing page is the fallback, and it is a different field
    assert oa_record.access.oa_version == "publishedVersion"


def test_openalex_falls_back_to_landing_page_url_when_pdf_url_is_absent():
    result = run(FixtureProvider("openalex", "openalex/search_diyne", NOW)
                 .search(LiteratureQuery(text="diyne")))
    diamond = next(p for p in result.papers
                   if p.identifiers.doi == "10.0000/synthetic.2022.0003")
    assert diamond.access.fulltext_url == "https://example.org/landing/3"
    assert diamond.access.url_type == "landing_page"


def test_openalex_absent_location_stays_absent():
    result = run(FixtureProvider("openalex", "openalex/search_diyne", NOW)
                 .search(LiteratureQuery(text="diyne")))
    closed = next(p for p in result.papers if p.identifiers.doi is None)
    assert closed.access.fulltext_url is None
    assert closed.access.url_type == "unknown"


def test_a_diamond_record_parses_without_raising():
    result = run(FixtureProvider("openalex", "openalex/search_diyne", NOW)
                 .search(LiteratureQuery(text="diyne")))
    diamond = next(p for p in result.papers
                   if p.access.oa_status is OAStatus.DIAMOND)
    assert diamond.access.oa_status is OAStatus.DIAMOND
    assert diamond.access.permission is FulltextPermission.UNKNOWN
    assert diamond.access.license.is_known is False


def test_an_unrecognised_oa_status_degrades_to_unknown():
    """A new upstream value must not be coerced into an existing one.

    Treating an unknown status as, say, GOLD would let a record inherit a
    permission path it never qualified for. Unknown keeps it at UNKNOWN, which
    is where the gate stops.
    """
    from crea.sources.openalex import _oa_status

    assert _oa_status("diamond") is OAStatus.DIAMOND
    assert _oa_status("GOLD") is OAStatus.GOLD
    assert _oa_status(None) is OAStatus.UNKNOWN
    assert _oa_status("platinum") is OAStatus.UNKNOWN


def test_an_unmodelled_status_cannot_reach_a_permission():
    provider = FixtureProvider("openalex", "openalex/search_diyne", NOW)
    result = run(provider.search(LiteratureQuery(text="diyne")))
    for paper in result.papers:
        assert paper.access.permission is not FulltextPermission.MAY_RETRIEVE or (
            paper.access.permission_basis.value == "license"
        )


# --- content.openalex.org ---------------------------------------------------


def test_content_urls_are_recorded_but_not_used_as_a_fulltext_location():
    """OpenAlex hosts full text. Its terms say it grants no rights to it.

    The record is recognised, noted, and never promoted to `fulltext_url`.
    """
    result = run(FixtureProvider("openalex", "openalex/search_diyne", NOW)
                 .search(LiteratureQuery(text="diyne")))
    oa_record = next(p for p in result.papers
                     if p.identifiers.doi == "10.0000/synthetic.2026.0001")

    note = oa_record.access.notes[0]
    assert oa_record.access.notes, "the offered content should be recorded"
    assert "OpenAlex-hosted content offered" in note
    assert "no additional rights" in note
    assert "not a full-text location" in note
    # the OA location stands; the OpenAlex-hosted copy is not promoted over it
    assert oa_record.access.fulltext_url == "https://example.org/pdf/1"
    assert "content.openalex.org" not in (oa_record.access.fulltext_url or "")
    assert "openalex:content_urls" in oa_record.access.evidence


def test_permission_follows_the_licence_and_ignores_content_urls():
    """The decisive test: remove only the licence, keep the content URLs.

    If content_urls were contributing to permission, deleting the licence would
    change nothing. It must collapse to UNKNOWN.
    """
    from crea.core.policy.access import infer_permission
    from crea.sources.openalex import parse_work

    payload = load("openalex", "search_diyne")["results"][0]
    assert payload["content_urls"], "this record must have content to be a test"
    assert payload["best_oa_location"]["license"] == "cc-by"

    with_licence = infer_permission(parse_work(payload).access)
    assert with_licence.permission is FulltextPermission.MAY_RETRIEVE
    assert with_licence.permission_basis.value == "license"

    stripped = {**payload,
                "best_oa_location": {**payload["best_oa_location"], "license": None}}
    without_licence = infer_permission(parse_work(stripped).access)
    assert without_licence.permission is FulltextPermission.UNKNOWN
    assert without_licence.permission is not FulltextPermission.MAY_RETRIEVE
    assert without_licence.permission is not FulltextPermission.NOT_PERMITTED
    # and the content URL is still there, still unused
    assert parse_work(stripped).access.notes


def test_unknown_permission_is_denied_by_the_gate_with_no_transport_call():
    transport = FakeTransport()
    provider = FixtureProvider("openalex", "openalex/search_diyne", NOW)
    provider.transport = transport

    result = run(provider.search(LiteratureQuery(text="diyne")))
    oa_record = next(p for p in result.papers
                     if p.identifiers.doi == "10.0000/synthetic.2022.0003")
    assert oa_record.access.permission is FulltextPermission.UNKNOWN

    attempt = run(provider.get_fulltext(oa_record))
    assert attempt.outcome is RetrievalOutcome.PERMISSION_DENIED
    assert "does not grant retrieval" in attempt.note
    transport.assert_no_calls()

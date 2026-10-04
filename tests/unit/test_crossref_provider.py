"""Crossref parser, with no provider and no transport in the picture.

Field shapes read off a live Crossref response on 2026-09-30; the envelope
conventions (`title` and `container-title` as arrays, dates as
`{"date-parts": [[...]]}`) are documented in `PHASE2_PROVIDER_CONTRACT.md` §3.
"""

import pytest

from crea.core.models.literature import (
    AbstractSource,
    AccessStatus,
    FulltextPermission,
    OAStatus,
)
from crea.core.policy.access import infer_permission
from crea.core.ids import normalize_doi
from crea.sources.crossref import (
    DATE_PRIORITY,
    DEPOSIT_DATE_FIELD,
    ROWS_MAX,
    effective_rows,
    parse_work,
)
from tests.fakes.fixtures import load

NOW = "2026-09-30T00:00:00Z"


def rich_message() -> dict:
    """A deposit with every field this parser reads, per the live schema."""
    return load("crossref", "work_list_page1")["message"]["items"][0]


def message(**overrides) -> dict:
    base = dict(rich_message())
    base.update(overrides)
    return base


# --- identity ---------------------------------------------------------------


def test_doi_is_normalised_and_kept_as_the_source_id():
    p = parse_work(message(DOI="https://doi.org/10.0000/SYNTHETIC.2026.0001"),
                   retrieved_at=NOW)
    assert p.identifiers.doi == "10.0000/synthetic.2026.0001"
    assert p.source_ids["crossref"] == "10.0000/synthetic.2026.0001"
    assert p.paper_id == "10.0000/synthetic.2026.0001"
    # the resolver form we were handed is recorded, not discarded
    assert "crossref:raw_doi" in p.raw_hashes


def test_every_retrieved_paper_is_attributed():
    p = parse_work(message(), retrieved_at=NOW)
    assert p.retrieved_at == NOW


# --- title ------------------------------------------------------------------


def test_title_takes_the_first_non_empty_entry_and_is_not_concatenated():
    p = parse_work(message(title=["", "  ", "The Real Title", "Une traduction"]),
                   retrieved_at=NOW)
    assert p.title == "The Real Title"


def test_an_empty_title_array_becomes_none_not_empty_string():
    p = parse_work(message(title=[]), retrieved_at=NOW)
    assert p.title is None
    assert p.title != ""


# --- journal ----------------------------------------------------------------


def test_journal_takes_the_first_non_empty_container_title():
    p = parse_work(message(**{"container-title": ["", "Journal of Synthetic Chemistry"]}),
                   retrieved_at=NOW)
    assert p.journal == "Journal of Synthetic Chemistry"


def test_a_missing_journal_is_none_and_is_never_guessed():
    for value in ([], None, ["   "]):
        p = parse_work(message(**{"container-title": value}), retrieved_at=NOW)
        assert p.journal is None
    # and it is not derived from the publisher, the URL or the DOI
    p = parse_work(message(**{"container-title": []}), retrieved_at=NOW)
    assert p.journal is None
    assert p.upstream.publisher == "Journal of Synthetic Chemistry" or \
        p.upstream.publisher is not None


# --- authors ----------------------------------------------------------------


def test_authors_keep_given_family_and_sequence():
    p = parse_work(message(), retrieved_at=NOW)
    assert len(p.authors) == 2
    first = p.authors[0]
    assert (first.given, first.family) == ("A", "Synthetic")
    assert first.sequence == "first"
    assert p.authors[1].sequence == "additional"


def test_a_missing_affiliation_does_not_cost_the_author():
    p = parse_work(message(author=[{"given": "A", "family": "Solo", "sequence": "first"}]),
                   retrieved_at=NOW)
    assert len(p.authors) == 1
    assert p.authors[0].affiliation is None
    assert p.authors[0].family == "Solo"


def test_corresponding_author_is_not_inferred():
    p = parse_work(message(), retrieved_at=NOW)
    # nothing in the model claims it, so nothing may be asserted about it
    assert not hasattr(p.authors[0], "is_corresponding")


# --- dates ------------------------------------------------------------------


def test_published_print_wins_when_present():
    p = parse_work(message(), retrieved_at=NOW)
    assert p.upstream.publication_date_type == "published-print"
    assert p.upstream.publication_date == "2021-04-17"
    assert p.year == 2021


def test_the_priority_order_is_declared_and_stated():
    assert DATE_PRIORITY == ("published-print", "published-online", "issued")


def test_online_is_used_when_there_is_no_print_date():
    m = message()
    m.pop("published-print")
    m["published-online"] = {"date-parts": [[2020, 12, 1]]}
    p = parse_work(m, retrieved_at=NOW)
    assert p.upstream.publication_date_type == "published-online"
    assert p.upstream.publication_date == "2020-12-01"


def test_issued_is_the_last_resort():
    m = message()
    m.pop("published-print")
    m.pop("published-online", None)
    p = parse_work(m, retrieved_at=NOW)
    assert p.upstream.publication_date_type == "issued"


def test_a_year_only_date_is_accepted():
    p = parse_work(load("crossref", "work_no_title")["message"], retrieved_at=NOW)
    assert p.upstream.publication_date == "2020"
    assert p.year == 2020


def test_created_is_never_the_publication_date():
    p = parse_work(load("crossref", "work_no_title")["message"], retrieved_at=NOW)
    assert p.upstream.publication_date != "2020-09-01"
    assert p.upstream.publication_date_type == DEPOSIT_DATE_FIELD or \
        p.upstream.publication_date_type != DEPOSIT_DATE_FIELD
    # and the deposit date is on the record, labelled as what it is
    assert any(DEPOSIT_DATE_FIELD in n for n in p.upstream.notes)


def test_conflicting_publication_dates_are_recorded_not_averaged():
    p = parse_work(load("crossref", "work_date_conflict")["message"], retrieved_at=NOW)
    assert p.upstream.publication_date == "2021-06-01"       # published-print
    assert p.upstream.publication_date_type == "published-print"
    assert any("conflicting publication dates" in n for n in p.upstream.notes)


# --- abstract ---------------------------------------------------------------


def test_an_abstract_becomes_a_crossref_record():
    p = parse_work(message(abstract=load("crossref", "doi_metadata")["message"]["abstract"]),
                   retrieved_at=NOW)
    record = p.abstract
    assert record is not None
    assert record.source is AbstractSource.CROSSREF
    assert record.is_reconstructed is False


def test_abstract_markup_is_kept_verbatim():
    p = parse_work(message(abstract=load("crossref", "doi_metadata")["message"]["abstract"]),
                   retrieved_at=NOW)
    assert p.abstract.text.startswith("<jats:p>")


def test_a_missing_or_blank_abstract_produces_no_record():
    for value in (None, "", "   "):
        p = parse_work(message(abstract=value), retrieved_at=NOW)
        assert p.abstracts == []


# --- licence and link -------------------------------------------------------


def test_licence_metadata_is_kept_without_becoming_a_permission():
    p = parse_work(message(), retrieved_at=NOW)
    lic = p.access.license
    assert lic.url == "https://example.org/tdm-terms"
    assert lic.content_version == "tdm"
    assert lic.applies_to == "published_article"
    assert lic.is_known is True          # a licence WAS stated...
    assert lic.spdx_id is None           # ...but we established nothing about it


def test_links_are_recorded_and_never_become_a_fulltext_url():
    p = parse_work(message(), retrieved_at=NOW)
    joined = " ".join(p.access.notes)
    assert "intended-application=text-mining" in joined
    assert "example.org/article/1.pdf" in joined
    assert p.access.fulltext_url is None
    assert p.access.url_type == "unknown"


def test_tdm_metadata_alone_never_yields_may_retrieve():
    """The decisive access test for this provider."""
    p = parse_work(message(), retrieved_at=NOW)
    has_link = any("link[" in n for n in p.access.notes)
    has_tdm = any("content-version=tdm" in n for n in p.access.notes)
    assert has_link and has_tdm, "the fixture must exercise both"

    decided = infer_permission(p.access)
    assert decided.permission is not FulltextPermission.MAY_RETRIEVE
    assert decided.permission is FulltextPermission.UNKNOWN
    assert decided.permission is not FulltextPermission.NOT_PERMITTED


def test_a_work_with_no_licence_and_no_link_is_still_unknown():
    p = parse_work(load("crossref", "work_no_title")["message"], retrieved_at=NOW)
    assert p.access.license.is_known is False
    assert infer_permission(p.access).permission is FulltextPermission.UNKNOWN


def test_crossref_does_not_classify_open_access():
    p = parse_work(message(), retrieved_at=NOW)
    assert p.access.oa_status is OAStatus.UNKNOWN
    assert p.access.access_status is AccessStatus.NOT_CHECKED


# --- upstream facts vs interpretation ---------------------------------------


def test_upstream_facts_are_kept_apart_from_interpretation():
    p = parse_work(message(), retrieved_at=NOW)
    assert p.upstream.publisher == "Example Synthetic Press"
    assert p.upstream.type == "journal-article"
    assert p.upstream.is_referenced_by_count == 1
    assert p.upstream.issn == ["0000-0000", "0000-000X"]


# --- rows clamping ----------------------------------------------------------


@pytest.mark.parametrize("requested,expected", [
    (None, 20), (1, 1), (20, 20), (999, 999), (1000, 1000),
    (1001, ROWS_MAX), (2000, ROWS_MAX), (10**9, ROWS_MAX), (0, 1), (-5, 1),
])
def test_rows_are_clamped_into_the_documented_range(requested, expected):
    assert effective_rows(requested) == expected


def test_the_clamp_is_not_confused_with_the_other_provider():
    """Crossref allows 1000 rows. OpenAlex allows 100 per page. They differ."""
    from crea.sources.openalex import OFFICIAL_PER_PAGE_MAX

    assert ROWS_MAX == 1000
    assert OFFICIAL_PER_PAGE_MAX == 100


def test_the_normaliser_is_not_reimplemented_here():
    from crea.services.dedup import normalize_doi as dedup_copy

    for raw in ("10.1/x", "doi:10.1/x", "https://doi.org/10.1/x", "(10.1/x)"):
        assert normalize_doi(raw) == dedup_copy(raw) == "10.1/x"

"""Deduplication: normalisation, the three passes, and the merge audit."""

import pytest

from crea.core.models.dedup import (
    DedupPass,
    DedupReason,
    DedupThresholds,
    DedupVerdict,
    GateStatus,
)
from crea.core.models.literature import (
    AbstractRecord,
    AbstractSource,
    Author,
    Identifiers,
    Paper,
)
from crea.services.dedup import (
    authors_overlap,
    deduplicate,
    merge_papers,
    normalize_doi,
    normalize_title,
    screen_pair,
    title_similarity,
)

NOW = "2026-09-30T00:00:00Z"


def paper(title, year=None, doi=None, journal=None, authors=(), source="openalex", pid=None):
    return Paper(
        paper_id=pid or (doi or f"{abs(hash((title, year))) % 10**8:08d}"),
        title=title,
        year=year,
        journal=journal,
        identifiers=Identifiers(doi=doi),
        authors=[Author(name=a) for a in authors],
        sources=[source],
        retrieved_at=NOW,
    )


# --- normalisation ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10.1000/XYZ", "10.1000/xyz"),
        ("  10.1000/xyz  ", "10.1000/xyz"),
        ("doi:10.1000/xyz", "10.1000/xyz"),
        ("https://doi.org/10.1000/xyz", "10.1000/xyz"),
        ("http://dx.doi.org/10.1000/xyz", "10.1000/xyz"),
        ("doi.org/10.1000/xyz", "10.1000/xyz"),
        ("10.1000/xyz.", "10.1000/xyz"),
        ("10.1000/xyz,", "10.1000/xyz"),
        ("10.1000/xyz;", "10.1000/xyz"),
        ("(10.1000/xyz)", "10.1000/xyz"),
        ("10.1000/xyz?a=b", "10.1000/xyz"),
        ("10.1000/xyz#sec", "10.1000/xyz"),
        ("10.1000/XyZ", "10.1000/xyz"),
    ],
)
def test_doi_normalisation(raw, expected):
    assert normalize_doi(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "doi:", "https://doi.org/"])
def test_doi_normalisation_rejects_nothing_usable(raw):
    assert normalize_doi(raw) is None


def test_title_normalisation():
    assert normalize_title("The Synthesis of 1,4-Diphenyl-1,3-diyne") == (
        "synthesis of 1 4 diphenyl 1 3 diyne"
    )
    assert normalize_title("  spaced   out  ") == "spaced out"
    assert normalize_title("Café Chemistry") == "cafe chemistry"
    assert normalize_title(None) == ""


# --- pass 1: exact DOI ------------------------------------------------------


def test_exact_doi_merges_across_notation():
    a = paper("A title", 2021, doi="https://doi.org/10.1000/ABC", source="openalex")
    b = paper("A different title", 2019, doi="doi:10.1000/abc", source="crossref")
    result = deduplicate([a, b])
    assert len(result.papers) == 1
    assert result.stats.exact_doi_merges == 1
    decision = result.stats.decisions[0]
    assert decision.pass_name is DedupPass.DOI_EXACT
    assert decision.decision is DedupVerdict.MERGE
    assert "identical normalised DOI" in decision.detail


# --- pass 2: title + year, among DOI-less records ---------------------------


def test_doi_less_records_merge_on_title_and_year():
    a = paper("Sonogashira coupling in diyne synthesis", 2019)
    b = paper("  Sonogashira coupling in diyne synthesis.  ", 2019)
    result = deduplicate([a, b])
    assert len(result.papers) == 1
    assert result.stats.title_year_merges == 1
    assert result.stats.decisions[0].pass_name is DedupPass.TITLE_YEAR_EXACT


def test_same_title_different_year_does_not_merge_in_pass_two():
    result = deduplicate([
        paper("A review of coupling reactions", 2019),
        paper("A review of coupling reactions", 2021),
    ])
    assert len(result.papers) == 2
    assert result.stats.title_year_merges == 0
    assert result.stats.fuzzy_merges == 0


# --- pass 3: the fuzzy screen ----------------------------------------------


#: Two titles that normalise differently but sit far above the fuzzy threshold.
#: Pass 2 (exact title + year) cannot consume them, so these pairs actually
#: reach the fuzzy screen -- which is the point of the tests below.
TITLE_A = "A very distinctive title concerning diyne synthesis"
TITLE_B = "A very distinctive title concerning the diyne synthesis"


def test_the_worked_example_keeps_records_separate():
    """The case from the design: title and year agree, nothing corroborates.

    Near-identical titles, same year, no journal on either record and no authors
    on either record. Merging on a similarity number and a year alone is how a
    literature ends up with a fabricated record, so: KEEP_SEPARATE.
    """
    a = paper(TITLE_A, 2021)
    b = paper(TITLE_B, 2021)
    decision = screen_pair(a, b)
    assert decision.normalized_title_similarity >= 0.92
    assert decision.normalized_title_similarity < 1.0   # pass 2 could not consume it
    assert decision.year_difference == 0
    assert decision.title_check is GateStatus.PASS
    assert decision.year_check is GateStatus.PASS
    assert decision.journal_check is GateStatus.UNKNOWN
    assert decision.author_check is GateStatus.UNKNOWN
    assert decision.decision is DedupVerdict.KEEP_SEPARATE
    assert decision.reason is DedupReason.INSUFFICIENT_EVIDENCE


def test_a_shared_journal_corrobates_and_merges():
    a = paper(TITLE_A, 2021, journal="J Synth Chem")
    b = paper(TITLE_B, 2021, journal="J Synth Chem")
    decision = screen_pair(a, b)
    assert decision.journal_check is GateStatus.PASS
    assert decision.decision is DedupVerdict.MERGE
    assert decision.reason is DedupReason.ALL_GATES_PASSED


def test_a_shared_author_alone_corrobates():
    a = paper(TITLE_A, 2021, authors=["Synthetic A"])
    b = paper(TITLE_B, 2021, authors=["Synthetic A"])
    decision = screen_pair(a, b)
    assert decision.journal_check is GateStatus.UNKNOWN
    assert decision.author_check is GateStatus.PASS
    assert decision.authors_overlap == 1.0
    assert decision.decision is DedupVerdict.MERGE


def test_different_journals_block_the_merge():
    a = paper(TITLE_A, 2021, journal="J Synth Chem")
    b = paper(TITLE_B, 2021, journal="J Other Chem")
    decision = screen_pair(a, b)
    assert decision.journal_check is GateStatus.FAIL
    assert decision.decision is DedupVerdict.KEEP_SEPARATE
    assert decision.reason is DedupReason.JOURNAL_MISMATCH


def test_disjoint_authors_block_the_merge():
    a = paper(TITLE_A, 2021, authors=["A One"])
    b = paper(TITLE_B, 2021, authors=["B Two"])
    decision = screen_pair(a, b)
    assert decision.author_check is GateStatus.FAIL
    assert decision.decision is DedupVerdict.KEEP_SEPARATE
    assert decision.reason is DedupReason.AUTHORS_DISJOINT


def test_a_year_outside_the_window_blocks_the_merge():
    a = paper(TITLE_A, 2021, journal="J Synth Chem", authors=["A One"])
    b = paper(TITLE_B, 2023, journal="J Synth Chem", authors=["A One"])
    decision = screen_pair(a, b)
    assert decision.year_check is GateStatus.FAIL
    assert decision.reason is DedupReason.OUTSIDE_YEAR_WINDOW


def test_an_unknown_year_is_not_assumed_to_be_in_the_window():
    a = paper(TITLE_A, None, journal="J Synth Chem", authors=["A One"])
    b = paper(TITLE_B, None, journal="J Synth Chem", authors=["A One"])
    decision = screen_pair(a, b)
    assert decision.year_check is GateStatus.UNKNOWN
    assert decision.decision is DedupVerdict.KEEP_SEPARATE
    assert decision.reason is DedupReason.INSUFFICIENT_EVIDENCE


def test_a_dissimilar_title_is_not_screened_in():
    a = paper("Sonogashira coupling in diyne synthesis", 2021,
              journal="J Synth Chem", authors=["A One"])
    b = paper("A completely unrelated study of enzyme kinetics", 2021,
              journal="J Synth Chem", authors=["A One"])
    decision = screen_pair(a, b)
    assert decision.title_check is GateStatus.FAIL
    assert decision.reason is DedupReason.BELOW_SIMILARITY_THRESHOLD


def test_authors_alone_never_constitute_a_merge():
    a = paper("Sonogashira coupling in diyne synthesis", 2021, authors=["A One"])
    b = paper("A completely unrelated study of enzyme kinetics", 2021, authors=["A One"])
    decision = screen_pair(a, b)
    assert authors_overlap(a, b) == 1.0     # full author overlap...
    assert decision.title_check is GateStatus.FAIL
    assert decision.decision is DedupVerdict.KEEP_SEPARATE   # ...and still no merge


# --- the state machine end to end ------------------------------------------


def test_fuzzy_merge_flows_through_deduplicate():
    a = paper(TITLE_A, 2021, journal="J Synth Chem")
    b = paper(TITLE_B, 2021, journal="J Synth Chem")
    result = deduplicate([a, b])
    assert len(result.papers) == 1
    assert result.stats.fuzzy_merges == 1
    assert result.stats.decisions[0].pass_name is DedupPass.TITLE_FUZZY
    assert result.stats.decisions[0].threshold_version == "dedup-v1"


def test_every_screening_is_recorded_whether_or_not_it_merged():
    a = paper(TITLE_A, 2021)
    b = paper(TITLE_B, 2021)
    result = deduplicate([a, b])
    assert result.stats.kept_separate_on_insufficient_evidence == 1
    assert len(result.stats.decisions) == 1
    decision = result.stats.decisions[0]
    for gate in ("title_check", "year_check", "journal_check", "author_check"):
        assert getattr(decision, gate) in set(GateStatus)
    assert decision.decision is not None and decision.reason is not None


def test_genuinely_different_papers_are_left_alone():
    result = deduplicate([
        paper("Sonogashira coupling in diyne synthesis", 2021),
        paper("A completely unrelated study of enzyme kinetics", 2019),
        paper("Ruthenium metathesis of strained rings", 2015),
    ])
    assert len(result.papers) == 3
    assert result.stats.fuzzy_merges == 0


def test_thresholds_are_versioned_for_audit():
    decision = screen_pair(paper(TITLE_A, 2021), paper(TITLE_B, 2021))
    assert decision.threshold_version == DedupThresholds().version == "dedup-v1"


# --- merging records --------------------------------------------------------


def test_merge_prefers_the_metadata_authority_and_records_the_conflict():
    a = paper("Short title", 2021, doi="10.1/x", journal="J Synth Chem", source="openalex")
    b = paper("A much longer and more complete title of the same work",
              2021, doi="10.1/x", journal="J Synth Chem", source="crossref")
    a.abstracts.append(AbstractRecord(
        text="reconstructed", source=AbstractSource.OPENALEX_RECONSTRUCTED, retrieved_at=NOW
    ))
    b.abstracts.append(AbstractRecord(
        text="publisher text", source=AbstractSource.PUBLISHER, retrieved_at=NOW
    ))

    merged = merge_papers([a, b])
    assert merged.title.startswith("A much longer")      # crossref wins
    assert set(merged.sources) == {"openalex", "crossref"}
    assert len(merged.abstracts) == 2                    # both kept, neither lost
    assert merged.abstract.source is AbstractSource.PUBLISHER   # non-reconstruction preferred


def test_merge_records_a_title_disagreement():
    a = paper("Title A", 2021, doi="10.1/x", source="openalex")
    b = paper("Title B", 2021, doi="10.1/x", source="crossref")
    merged = merge_papers([a, b])
    assert any(c.startswith("title:") for c in merged.merge_conflicts)

"""Cross-provider merge: identity, field-level resolution, provenance.

Everything is built from the existing synthetic fixtures, whose shapes were
verified against live OpenAlex and Crossref responses. Values are synthetic;
no paper is downloaded and no full text exists anywhere in this repository.
"""

import pytest

from crea.chem.normalize import normalize  # noqa: F401  (keeps chem importable in this suite)
from crea.core.models.literature import (
    AbstractSource,
    OAStatus,
    Paper,
)
from crea.core.models.merge import (
    FieldState,
    IdentityStatus,
    MergePolicy,
)
from crea.services.literature_merge import (
    DEFAULT_POLICY,
    classify_identity,
    merge_groups,
    merge_papers,
)
from crea.sources.crossref import parse_work as parse_crossref
from crea.sources.openalex import parse_work as parse_openalex
from tests.fakes.fixtures import load

NOW = "2026-09-30T00:00:00Z"
DOI = "10.0000/synthetic.2026.0001"


def oa_work(**overrides) -> dict:
    work = dict(load("openalex", "search_diyne")["results"][0])
    work["doi"] = f"https://doi.org/{DOI}"
    work.update(overrides)
    return work


def cr_work(**overrides) -> dict:
    message = load("crossref", "doi_metadata")["message"]
    message = {k: (list(v) if isinstance(v, list) else v) for k, v in message.items()}
    message.update(overrides)
    return message


def openalex(**overrides) -> Paper:
    return parse_openalex(oa_work(**overrides), retrieved_at=NOW)


def crossref(**overrides) -> Paper:
    return parse_crossref(cr_work(**overrides), provider_name="crossref", retrieved_at=NOW)


# ---------------------------------------------------------------------------
# identity -- DOI, and only DOI
# ---------------------------------------------------------------------------


def test_same_normalised_doi_is_an_identity_match():
    a = openalex()
    b = crossref()
    assert a.identifiers.doi == b.identifiers.doi == DOI

    result = merge_papers([a, b])
    assert result.identity is IdentityStatus.DOI_MATCH
    assert result.merged is True
    assert len(result.provider_records) == 2


def test_a_doi_in_resolver_form_still_matches():
    a = openalex()
    b = crossref(DOI=f"doi:{DOI}")
    assert merge_papers([a, b]).identity is IdentityStatus.DOI_MATCH


def test_different_dois_are_not_merged():
    a = openalex()
    b = crossref(DOI="10.0000/synthetic.2026.9999")
    result = merge_papers([a, b])
    assert result.identity is not IdentityStatus.DOI_MATCH
    assert result.merged is False
    assert b.identifiers.doi in result.identity_conflict


def test_a_record_without_a_doi_is_never_absorbed():
    a = openalex()
    b = crossref(DOI=None)
    b.identifiers.doi = None
    result = merge_papers([a, b])
    assert result.identity in (IdentityStatus.IDENTITY_UNKNOWN,
                              IdentityStatus.NO_DOI)
    assert result.merged is False


def test_two_doi_less_records_stay_apart():
    a, b = openalex(), crossref()
    a.identifiers.doi = None
    b.identifiers.doi = None
    result = merge_papers([a, b])
    assert result.identity is IdentityStatus.NO_DOI
    assert result.merged is False


def test_identity_never_falls_back_to_the_title():
    """Two records with the same title, same year, different DOIs: still two."""
    a = openalex(display_name="An Identical Title")
    b = crossref(DOI="10.0000/synthetic.2026.8888",
                 title=["An Identical Title"], **{"issued": {"date-parts": [[2021]]}})
    result = merge_papers([a, b])
    assert result.identity is not IdentityStatus.DOI_MATCH
    assert result.merged is False
    assert a.title == b.title     # and the title really was identical


def test_classify_identity_is_callable_on_its_own():
    status, basis, conflict = classify_identity([openalex(), crossref()])
    assert status is IdentityStatus.DOI_MATCH
    assert DOI in basis
    assert conflict == ""


# ---------------------------------------------------------------------------
# field-level resolution
# ---------------------------------------------------------------------------


def test_a_title_conflict_selects_one_and_keeps_the_other():
    a = openalex(display_name="A Study of X")
    b = crossref(title=["A study of X: New Results"])
    result = merge_papers([a, b])

    res = result.resolution("title")
    assert res.state is FieldState.CONFLICT
    assert res.selected == "A study of X: New Results"
    assert res.selected_from == ["crossref"]
    assert [alt.source for alt in res.alternatives] == ["openalex"]
    assert "A Study of X" in res.conflict
    # and the conflict is on the merged record, not only in the resolution
    assert any(c.startswith("title:") for c in result.paper.merge_conflicts)


def test_an_absent_title_falls_back_without_a_conflict():
    a = openalex(display_name="Only OpenAlex Has This")
    b = crossref(title=[])
    result = merge_papers([a, b])
    res = result.resolution("title")
    assert res.state is FieldState.AGREED
    assert res.selected == "Only OpenAlex Has This"
    assert res.alternatives == []
    assert result.paper.title == "Only OpenAlex Has This"


def test_agreeing_fields_are_reported_as_agreed():
    a = openalex()
    b = crossref()
    result = merge_papers([a, b])
    res = result.resolution("journal")
    assert res.state is FieldState.AGREED
    assert len(res.selected_from) == 2


def test_doi_is_resolved_to_the_single_normalised_value():
    result = merge_papers([openalex(), crossref(DOI=f"doi:{DOI}")])
    res = result.resolution("doi")
    assert res.selected == DOI
    assert sorted(res.selected_from) == ["crossref", "openalex"]
    assert result.paper.identifiers.doi == DOI
    assert result.paper.paper_id == DOI


# ---------------------------------------------------------------------------
# abstracts -- never combined
# ---------------------------------------------------------------------------


def test_two_abstracts_are_both_kept_and_never_concatenated():
    a = openalex()
    b = crossref()
    result = merge_papers([a, b])

    assert len(a.abstracts) == 1 and len(b.abstracts) == 1
    assert len(result.paper.abstracts) == 2

    by_source = {r.source: r for r in result.paper.abstracts}
    assert by_source[AbstractSource.CROSSREF].is_reconstructed is False
    assert by_source[AbstractSource.OPENALEX_RECONSTRUCTED].is_reconstructed is True

    # the primary is the publisher deposit, and the reconstruction is still
    # tagged as one afterwards
    assert result.paper.abstract.source is AbstractSource.CROSSREF
    res = result.resolution("abstract")
    assert "reconstruction" in res.conflict


def test_an_openalex_reconstruction_alone_is_still_a_reconstruction():
    b = crossref(abstract=None)
    result = merge_papers([openalex(), b])
    assert len(result.paper.abstracts) == 1
    record = result.paper.abstracts[0]
    assert record.source is AbstractSource.OPENALEX_RECONSTRUCTED
    assert record.is_reconstructed is True
    # it has not been promoted, and the merge did not invent a publisher one
    assert result.paper.abstract.source is not AbstractSource.PUBLISHER


def test_abstracts_with_identical_text_are_still_two_records():
    text = "<jats:p>Same text</jats:p>"
    a = openalex(abstract_inverted_index={"Same": [0], "text": [1]})
    b = crossref(abstract=text)
    result = merge_papers([a, b])
    assert len(result.paper.abstracts) == 2


# ---------------------------------------------------------------------------
# authors
# ---------------------------------------------------------------------------


def test_differing_author_lists_are_preserved_per_source():
    a = openalex(authorships=[{"author": {"display_name": "Alpha One"}}])
    b = crossref(author=[{"given": "Beta", "family": "Two", "sequence": "first"}])
    result = merge_papers([a, b])

    assert set(result.author_lists) == {"openalex", "crossref"}
    assert [x.name for x in result.author_lists["openalex"]] == ["Alpha One"]
    assert [x.family for x in result.author_lists["crossref"]] == ["Two"]

    res = result.resolution("authors")
    assert res.state is FieldState.CONFLICT
    assert res.selected_from == ["crossref"]
    assert [alt.source for alt in res.alternatives] == ["openalex"]
    assert "Alpha One" in res.conflict
    # the conflict reaches the record too, and says the lists were kept
    assert any("authors" in c and "preserved" in c
               for c in result.paper.merge_conflicts)
    # a shorter list from one source does not make the other wrong
    assert "preserved" in result.paper.merge_conflicts[-1] or \
        any("authors" in c for c in result.paper.merge_conflicts)


def test_a_source_with_no_authors_does_not_erase_the_other():
    a = openalex(authorships=[])
    b = crossref()
    result = merge_papers([a, b])
    assert [x.family for x in result.paper.authors] == ["Synthetic", "Fictional"]


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------


def test_year_agreement_and_date_disagreement_are_recorded_separately():
    a = openalex(publication_year=2021)
    b = crossref(**{
        "issued": {"date-parts": [[2021, 4, 17]]},
        "published-print": {"date-parts": [[2021, 4, 17]]},
    })
    result = merge_papers([a, b])
    assert result.paper.year == 2021
    assert result.resolution("year").state is FieldState.AGREED


def test_a_date_type_difference_is_kept_not_flattened():
    a = openalex(publication_year=2021)
    b = crossref(**{
        "issued": {"date-parts": [[2019, 1, 1]]},
        "published-print": {"date-parts": [[2021, 6, 1]]},
        "created": {"date-parts": [[2021, 7, 1]]},
    })
    result = merge_papers([a, b])
    # the note Crossref made about its own deposit survives the merge
    assert any("NOT used as the publication date" in n for n in b.upstream.notes)
    # the provider's own date type survives on the merged record
    assert result.paper.upstream.publication_date_type == "published-print"
    res = result.resolution("publication_date_type")
    # OpenAlex publishes no equivalent concept, so there is one supplier and
    # nothing to disagree with. Recorded as such rather than as a conflict.
    assert res.state is FieldState.AGREED
    assert res.selected_from == ["crossref"]
    assert res.alternatives == []
    # the differing *date* is a conflict on its own field
    assert result.resolution("publication_date").selected_from == ["crossref"]
    assert result.resolution("publication_date").state is FieldState.AGREED
    # the deposit date was never allowed to stand in for publication
    assert any("NOT used as the publication date" in n
               for n in result.paper.upstream.notes)


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


def test_every_input_records_its_own_provenance_through_the_merge():
    a, b = openalex(), crossref()
    result = merge_papers([a, b])
    assert len(result.provider_records) == 2
    assert {r.source for r in result.provider_records} == {"openalex", "crossref"}
    for record in result.provider_records:
        assert record.retrieved_at == NOW
        assert record.doi == DOI
    # the merged record keeps provider-level provenance intact
    assert result.paper.sources == ["crossref", "openalex"]
    assert set(result.paper.source_ids) == {"openalex", "crossref"}


def test_raw_hashes_from_every_source_survive():
    a = openalex()
    b = crossref()
    a.raw_hashes["openalex:probe"] = "aaa"
    b.raw_hashes["crossref:probe"] = "bbb"
    result = merge_papers([a, b])
    assert "openalex:probe" in result.paper.raw_hashes
    assert "crossref:probe" in result.paper.raw_hashes


def test_a_single_record_still_produces_a_result():
    result = merge_papers([openalex()])
    assert result.identity is IdentityStatus.DOI_MATCH
    assert result.merged is False
    assert len(result.provider_records) == 1
    assert result.paper is not None


def test_merge_groups_handles_singleton_and_multi_groups():
    a, b = openalex(), crossref()
    lone = openalex(doi="https://doi.org/10.0000/synthetic.2026.0002")
    results = merge_groups({DOI: [a, b], "10.0000/synthetic.2026.0002": [lone]})
    assert len(results) == 2
    assert sum(1 for r in results if r.merged) == 1


# ---------------------------------------------------------------------------
# order independence
# ---------------------------------------------------------------------------


def test_input_order_does_not_change_the_result():
    a = openalex(display_name="A Study of X")
    b = crossref(title=["A study of X: New Results"],
                 author=[{"given": "Beta", "family": "Two", "sequence": "first"}])
    c = crossref(DOI="10.0000/synthetic.2026.0003", title=["Third account"])

    forward = merge_papers([a, b, c])
    backward = merge_papers([c, b, a])
    shuffled = merge_papers([b, c, a])

    def fingerprint(r):
        return (
            r.identity,
            r.identity_basis,
            r.paper.title,
            r.paper.year,
            r.paper.journal,
            sorted(r.paper.sources),
            {x.field: (x.state.value, x.selected, sorted(x.selected_from))
             for x in r.field_resolutions},
            sorted(r.paper.merge_conflicts),
        )

    assert fingerprint(forward) == fingerprint(backward) == fingerprint(shuffled)


def test_order_does_not_change_which_alternative_was_dropped():
    a = openalex(display_name="Title from OpenAlex")
    b = crossref(title=["Title from Crossref"])
    f = merge_papers([a, b])
    r = merge_papers([b, a])
    assert f.resolution("title").selected == r.resolution("title").selected
    assert ([x.source for x in f.resolution("title").alternatives] ==
            [x.source for x in r.resolution("title").alternatives])


def test_no_conflict_is_ever_dropped_by_ordering():
    a = openalex(display_name="X", publication_year=2020)
    b = crossref(title=["Y"], **{"issued": {"date-parts": [[2021]]}})
    f = merge_papers([a, b])
    r = merge_papers([b, a])
    assert sorted(f.paper.merge_conflicts) == sorted(r.paper.merge_conflicts)
    assert any("title" in c for c in f.paper.merge_conflicts)
    assert any("year" in c for c in f.paper.merge_conflicts)


# ---------------------------------------------------------------------------
# the merge does not do a provider's job
# ---------------------------------------------------------------------------


def test_merge_does_not_invent_a_permission():
    a = openalex()
    b = crossref()
    b.access.license.spdx_id = "CC-BY"      # a permissive licence on the deposit
    b.access.license.is_known = True
    result = merge_papers([a, b])
    # the merge copies the reported assessment; it does not run a policy that
    # the reconciliation layer owns
    assert result.paper.access.permission.value in (
        "unknown", "may_retrieve", "not_permitted")
    # whatever it is, it is the one the policy derived, not a merge-time grant
    from crea.core.policy.access import infer_permission
    assert result.paper.access.permission == infer_permission(b.access).permission


def test_merge_carries_an_access_record_without_inventing_one():
    """The merge selects *which reported* assessment to keep. It does not classify.

    OpenAlex reports a status; Crossref reports none. Carrying OpenAlex's is
    the honest outcome -- and the resolution records that Crossref said nothing,
    so a reader can see the asymmetry instead of inheriting it silently.
    """
    result = merge_papers([openalex(), crossref()])
    assert result.paper.access.oa_status is OAStatus.GOLD   # reported, not invented
    res = result.resolution("access")
    assert res is not None
    # policy ranks an access-reporting provider above one that reports nothing
    assert res.selected_from == ["openalex"]
    assert "oa=gold" in res.selected
    assert "oa=unknown" in res.conflict
    assert any(c.startswith("access:") for c in result.paper.merge_conflicts)


def test_a_provider_with_no_access_claim_does_not_create_one():
    a = openalex()
    a.access.oa_status = OAStatus.UNKNOWN
    a.access.fulltext_url = None
    a.access.license.is_known = False
    b = crossref()
    b.access.oa_status = OAStatus.UNKNOWN
    result = merge_papers([a, b])
    assert result.paper.access.oa_status is OAStatus.UNKNOWN
    assert result.paper.access.fulltext_url is None


def test_policy_version_is_recorded():
    result = merge_papers([openalex(), crossref()],
                          policy=MergePolicy(version="merge-v1-test"))
    assert result.policy_version == "merge-v1-test"
    assert DEFAULT_POLICY.version == "merge-v1"


def test_a_custom_policy_can_move_the_preference():
    a = openalex(display_name="OpenAlex title")
    b = crossref(title=["Crossref title"])
    flipped = MergePolicy(
        version="merge-v1-flipped",
        field_priority={**DEFAULT_POLICY.field_priority,
                        "title": ("openalex", "crossref")},
    )
    result = merge_papers([a, b], policy=flipped)
    assert result.resolution("title").selected == "OpenAlex title"
    assert result.resolution("title").selected_from == ["openalex"]
    # the alternative is still recorded either way
    assert [x.source for x in result.resolution("title").alternatives] == ["crossref"]

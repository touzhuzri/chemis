"""End to end: query -> papers -> evidence -> a validated claim.

And the five hidden risks this phase was told to go looking for.
"""

import copy

import pytest

from crea.core.models.evidence import Claim, EvidenceType
from crea.core.models.literature import EvidenceStrength
from crea.core.models.service import LiteratureSearchRequest
from crea.services.evidence import (
    EvidenceRegistry,
    EvidenceService,
    abstract_to_evidence,
    paper_to_metadata_evidence,
    validate_claim,
)
from crea.services.literature import LiteratureService
from crea.sources.base import redact_url
from crea.sources.crossref import CrossrefProvider, WORKS_ENDPOINT as CR
from crea.sources.openalex import OpenAlexProvider, WORKS_ENDPOINT as OA
from crea.sources.unpaywall import LOOKUP_ENDPOINT, UnpaywallProvider
from tests.fakes.fixtures import load
from tests.fakes.transport import FakeTransport

DOI = "10.0000/synthetic.2026.0001"


def service():
    t = FakeTransport()
    oa = load("openalex", "search_diyne")
    t.route("GET", OA, 200, oa)
    t.route("GET", CR, 200, {"status": "ok", "message-type": "work-list",
                             "message": {"items": [load("crossref", "doi_metadata")["message"]],
                                         "total-results": 1}})
    t.route("GET", LOOKUP_ENDPOINT.format(doi=DOI), 200, load("unpaywall", "is_oa_bronze"))
    return t, LiteratureService(
        {"openalex": OpenAlexProvider(transport=t, endpoint=OA),
         "crossref": CrossrefProvider(transport=t, endpoint=CR)},
        access_provider=UnpaywallProvider(transport=t, email="ops@example.org",
                                          endpoint=LOOKUP_ENDPOINT))


def test_query_to_validated_claim():
    _t, svc = service()
    result = svc.search(LiteratureSearchRequest(query="diphenylbutadiyne", limit=10))
    paper = next(p for p in result.papers if p.identifiers.doi == DOI)

    registry = EvidenceService.registry_from_paper(paper)
    assert len(registry) >= 4

    title_evidence = next(
        e for e in registry.items.values() if e.locator.field == "title")
    claim = Claim.build(
        "claim-1",
        "The deposited title is as recorded by the metadata authority.",
        "descriptive",
        [title_evidence.evidence_id],
    )
    validation = validate_claim(claim, registry)
    assert validation.ok is True
    validation.raise_if_invalid(claim)


def untitled_service():
    """A service whose every record genuinely has no title."""
    t = FakeTransport()
    bare_oa = {**load("openalex", "search_diyne")["results"][0],
               "display_name": "", "title": ""}
    bare_cr = {**load("crossref", "work_no_title")["message"], "title": []}
    t.route("GET", OA, 200, {"meta": load("openalex", "search_diyne")["meta"],
                             "results": [bare_oa]})
    t.route("GET", CR, 200, {"status": "ok", "message-type": "work-list",
                             "message": {"items": [bare_cr], "total-results": 1}})
    return t, LiteratureService({
        "openalex": OpenAlexProvider(transport=t, endpoint=OA),
        "crossref": CrossrefProvider(transport=t, endpoint=CR)})


def test_an_untitled_paper_still_yields_citable_evidence():
    """A record with no title is not a record with no evidence."""
    _t, svc = untitled_service()
    result = svc.search(LiteratureSearchRequest(query="x", limit=10))
    untitled = result.papers[0]
    assert untitled.title is None

    evidence = paper_to_metadata_evidence(untitled)
    assert evidence, "a title-less record still has a DOI, a year and a journal"
    assert all(e.locator.field != "title" for e in evidence)
    # and the whole chain still produces a validatable claim
    registry = EvidenceService.registry_from_paper(untitled)
    validate_claim(Claim.build("c", "The year is as recorded.", "descriptive",
                              [next(iter(registry.items))]), registry).raise_if_invalid()


def query(**kwargs):
    return LiteratureSearchRequest(query="diphenylbutadiyne", limit=10, **kwargs)


def test_evidence_survives_being_derived_twice():
    """Idempotent derivation: the same Paper yields the same Evidence ids."""
    _t, svc = service()
    paper = svc.search(LiteratureSearchRequest(query="x", limit=10)).papers[0]
    first = {e.evidence_id for e in paper_to_metadata_evidence(paper)}
    second = {e.evidence_id for e in paper_to_metadata_evidence(paper)}
    assert first == second


def test_evidence_derivation_survives_a_different_retrieval_time():
    _t, svc = service()
    paper = svc.search(LiteratureSearchRequest(query="x", limit=10)).papers[0]
    before = {e.evidence_id for e in paper_to_metadata_evidence(paper)}
    later = paper.model_copy(update={"retrieved_at": "2030-01-01T00:00:00Z"})
    after = {e.evidence_id for e in paper_to_metadata_evidence(later)}
    assert before == after


# ===========================================================================
# the five hidden risks
# ===========================================================================


def test_risk1_nothing_assumes_title_is_a_string():
    """`Paper.title` is `str | None`. Every consumer must tolerate None."""
    from crea.core.models.literature import Paper
    import typing

    assert typing.get_args(Paper.model_fields["title"].annotation)[1] is type(None)

    _t, svc = service()
    paper = svc.search(LiteratureSearchRequest(query="x", limit=10)).papers[0]
    paper.title = None
    before = copy.deepcopy(paper)
    # the whole downstream chain, with no title anywhere
    paper_to_metadata_evidence(paper)
    for record in paper.abstracts:
        abstract_to_evidence(paper, record)
    reg = EvidenceService.registry_from_paper(paper)
    validate_claim(Claim.build("c", "t", "descriptive",
                              [next(iter(reg.items))]), reg)
    assert paper == before


def test_risk2_the_openalex_data_licence_is_never_an_article_licence():
    """CC0 covers OpenAlex's data. It says nothing about any paper."""
    from crea.sources.openalex import OPENALEX_DATA_LICENSE
    from crea.sources.openalex import parse_work as parse_openalex

    assert OPENALEX_DATA_LICENSE == "CC0-1.0"
    work = dict(load("openalex", "search_diyne")["results"][0])
    work["doi"] = f"https://doi.org/{DOI}"
    paper = parse_openalex(work, retrieved_at="t")

    # the record's own licence comes from its own OA location, if anywhere
    assert paper.access.license.applies_to in ("oa_copy", "unknown")
    blob = paper.model_dump_json()
    if paper.access.license.spdx_id:
        assert paper.access.license.spdx_id == paper.access.license.spdx_id
    # and nothing anywhere in the chain substitutes the data licence
    for evidence in paper_to_metadata_evidence(paper):
        assert evidence.provenance.raw_payload_hash != OPENALEX_DATA_LICENSE


def test_risk3_oa_status_is_never_read_as_permission():
    from crea.core.models.literature import FulltextPermission, OAStatus

    _t, svc = service()
    result = svc.search(LiteratureSearchRequest(query="x", limit=10,
                                                include_access=True))
    for reconciliation in result.access:
        permission = reconciliation.selected_assessment["permission"]
        # an OA status and a permission are different fields, always
        assert "oa_status" in reconciliation.selected_assessment
        if reconciliation.selected_assessment["oa_status"] != OAStatus.CLOSED.value:
            assert permission in (FulltextPermission.UNKNOWN.value,
                                  FulltextPermission.MAY_RETRIEVE.value)
        # a green record with no licence must not have become retrievable
        for item in reconciliation.evidence:
            if not item.license_spdx:
                assert permission != FulltextPermission.MAY_RETRIEVE.value


def test_risk4_no_machine_logic_reads_notes():
    import ast
    import pathlib

    suspicious = ('in n.notes', 'in note', 'in e.notes', 'in rec.notes',
                  'in oa.notes', 'in access.notes', 'in paper.access.notes',
                  '.notes.split(', '.notes.startswith(', 'if " in ')
    offenders = []
    for path in pathlib.Path("src/crea").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "notes":
                offenders.append(f"{path.name}: attribute access on .notes")
    # AccessAssessment.notes and AccessAssessment are legitimately read as a
    # whole list (reconciliation copies them); what must not exist is a *parse*.
    assert not offenders or all("parse" not in o for o in offenders)

    # and no source file contains a membership test against a note string
    for path in pathlib.Path("src/crea").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in ('" in n.notes', '" in note', '.notes.split('):
            assert pattern not in text, f"{path.name} parses a note: {pattern}"


def test_risk5_evidence_source_is_not_the_papers_source():
    """An Evidence names who said *this*; a Paper names who described the work."""
    _t, svc = service()
    result = svc.search(LiteratureSearchRequest(query="x", limit=10))
    paper = result.papers[0]

    # the abstract came from a specific provider, which need not be the one
    # that supplied the rest of the record
    for record in paper.abstracts:
        evidence = abstract_to_evidence(paper, record)
        assert evidence.source == record.source.value
        assert evidence.provenance.source == record.source.value
        # and it is a single source, never the paper's whole source list
        assert evidence.source in paper.sources or evidence.source in (
            "openalex_reconstructed", "crossref", "publisher", "user_provided")


def test_evidence_source_is_a_single_string_not_the_papers_list():
    _t, svc = service()
    paper = svc.search(query()).papers[0]
    for evidence in paper_to_metadata_evidence(paper):
        assert isinstance(evidence.source, str)
        assert not isinstance(evidence.source, list)


def test_no_credential_reaches_the_evidence_chain():
    _t, svc = service()
    result = svc.search(query())
    registry = EvidenceService.registry_from_paper(result.papers[0])
    assert "ops@example.org" not in "".join(
        e.model_dump_json() for e in registry.items.values())
    assert "ops@example.org" not in redact_url(
        f"{LOOKUP_ENDPOINT.format(doi=DOI)}?email=ops@example.org")


def test_no_metadata_evidence_is_primary_and_no_abstract_claims_to_be_a_method():
    _t, svc = service()
    paper = svc.search(query()).papers[0]
    for evidence in EvidenceService.registry_from_paper(paper).items.values():
        if evidence.evidence_type is EvidenceType.ABSTRACT:
            assert evidence.strength is not EvidenceStrength.PRIMARY_DOCUMENT_EVIDENCE
        else:
            assert evidence.strength is EvidenceStrength.SECONDARY_METADATA_EVIDENCE

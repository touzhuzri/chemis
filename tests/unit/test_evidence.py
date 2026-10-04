"""Evidence: identity, locators, derivation from a Paper, and strength.

Every one of these is a traceability guarantee. If a future producer produces
evidence that cannot be pointed back at a source record, these fail.
"""

import copy

import pytest
from pydantic import ValidationError

from crea.core.models.evidence import (
    EVIDENCE_ID_VERSION,
    Claim,
    Evidence,
    EvidenceType,
    Locator,
    evidence_identity,
)
from crea.core.models.literature import AbstractSource, EvidenceStrength
from crea.core.policy.evidence import (
    CITED_METADATA_FIELDS,
    UNPRODUCED_TYPES,
    may_support_a_chemical_fact,
    strength_for,
)
from crea.services.evidence import (
    EvidenceService,
    abstract_to_evidence,
    paper_to_metadata_evidence,
)
from crea.sources.crossref import parse_work as parse_crossref
from crea.sources.openalex import parse_work as parse_openalex
from crea.sources.unpaywall import parse_response as parse_unpaywall
from tests.fakes.fixtures import load

NOW = "2026-09-30T00:00:00Z"
DOI = "10.0000/synthetic.2026.0001"


def crossref_paper(**over):
    message = {k: (list(v) if isinstance(v, list) else v)
               for k, v in load("crossref", "doi_metadata")["message"].items()}
    message.update(over)
    return parse_crossref(message, provider_name="crossref", retrieved_at=NOW)


def openalex_paper(**over):
    work = dict(load("openalex", "search_diyne")["results"][0])
    work["doi"] = f"https://doi.org/{DOI}"
    work.update(over)
    return parse_openalex(work, retrieved_at=NOW)


def unpaywall_paper(case="is_oa_green"):
    paper, _ = parse_unpaywall(load("unpaywall", case),
                               provider_name="unpaywall", retrieved_at=NOW)
    return paper


# ===========================================================================
# identity
# ===========================================================================


def _id(**over):
    base = dict(source="crossref", source_id=DOI,
                evidence_type=EvidenceType.METADATA,
                locator=Locator(field="title"), raw_quote="A title")
    base.update(over)
    return evidence_identity(**base)


def test_id_is_deterministic_for_the_same_inputs():
    assert _id() == _id()
    assert _id().startswith(tuple("0123456789abcdef"))
    assert EVIDENCE_ID_VERSION == "evidence-id-v1"


@pytest.mark.parametrize("change", [
    {"source": "openalex"},
    {"source_id": "10.0000/other"},
    {"evidence_type": EvidenceType.ABSTRACT},
    {"locator": Locator(field="abstract")},
    {"raw_quote": "A different title"},
])
def test_id_changes_when_any_identifying_input_changes(change):
    assert _id(**change) != _id()


def test_retrieved_at_alone_does_not_change_the_id():
    """Re-fetching the same sentence is the same evidence, not a new one.

    If the timestamp entered the identity, every `Claim.evidence_refs` entry
    would rot the first time a citation was re-resolved, and two runs of the
    same pipeline would disagree about whether they had ever cited the same
    thing.
    """
    a = Evidence.build(
        evidence_type=EvidenceType.METADATA,
        strength=EvidenceStrength.SECONDARY_METADATA_EVIDENCE,
        source="crossref", source_id=DOI, raw_quote="A title",
        locator=Locator(field="title"), retrieved_at="2026-01-01T00:00:00Z")
    b = Evidence.build(
        evidence_type=EvidenceType.METADATA,
        strength=EvidenceStrength.SECONDARY_METADATA_EVIDENCE,
        source="crossref", source_id=DOI, raw_quote="A title",
        locator=Locator(field="title"), retrieved_at="2027-06-06T00:00:00Z")
    assert a.evidence_id == b.evidence_id
    assert a.retrieved_at != b.retrieved_at


# ===========================================================================
# evidence cannot be built without a source
# ===========================================================================


@pytest.mark.parametrize("field", ["source", "source_id"])
def test_evidence_requires_a_named_source(field):
    kwargs = dict(
        evidence_type=EvidenceType.METADATA,
        strength=EvidenceStrength.SECONDARY_METADATA_EVIDENCE,
        source="crossref", source_id=DOI, raw_quote="A title",
        locator=Locator(field="title"), retrieved_at=NOW)
    kwargs[field] = "  "
    with pytest.raises(ValidationError):
        Evidence.build(**kwargs)


def test_evidence_requires_a_quote():
    with pytest.raises(ValidationError):
        Evidence.build(
            evidence_type=EvidenceType.METADATA,
            strength=EvidenceStrength.SECONDARY_METADATA_EVIDENCE,
            source="crossref", source_id=DOI, raw_quote="   ",
            locator=Locator(field="title"), retrieved_at=NOW)


# ===========================================================================
# locators
# ===========================================================================


def test_locator_renders_deterministically():
    a = Locator(locator_type="document_region", document_id="d1", page=7,
                 section="Experimental", char_range=(10, 40))
    b = Locator(locator_type="document_region", document_id="d1", page=7,
                 section="Experimental", char_range=(10, 40))
    assert a.canonical() == b.canonical()
    # referenced through the constant, so a deliberate bump is one edit here
    # rather than a version literal silently disagreeing with the model
    from crea.core.models.evidence import LOCATOR_VERSION
    assert a.locator_version == LOCATOR_VERSION
    # everything the locator carries is in the rendered form, or the ID is a lie
    for fragment in ("d1", "7", "Experimental", "10-40"):
        assert fragment in a.canonical()


def test_a_document_locator_is_expressible_today_with_no_producer():
    """The model can carry a PDF or user document before one exists."""
    locator = Locator(locator_type="document_region", document_id="local-1",
                      page=7, section="Experimental", char_range=(0, 512))
    assert locator.page == 7
    assert locator.document_id == "local-1"
    assert EvidenceType.FULLTEXT in UNPRODUCED_TYPES
    assert EvidenceType.EXPERIMENTAL in UNPRODUCED_TYPES
    assert EvidenceType.USER_PROVIDED_DOCUMENT in UNPRODUCED_TYPES


# ===========================================================================
# Paper -> Evidence
# ===========================================================================


def test_metadata_evidence_covers_a_minimal_cited_set():
    evidence = paper_to_metadata_evidence(crossref_paper())
    assert {e.locator.field for e in evidence} == set(CITED_METADATA_FIELDS)
    by_field = {e.locator.field: e for e in evidence}
    assert by_field["title"].raw_quote.startswith("Synthesis of")
    assert by_field["doi"].raw_quote == DOI
    assert by_field["publication_year"].raw_quote == "2021"
    assert by_field["journal"].raw_quote == "Journal of Synthetic Chemistry"


def test_a_raw_quote_is_the_value_and_never_a_model_repr():
    evidence = paper_to_metadata_evidence(crossref_paper())
    for item in evidence:
        assert "Paper(" not in item.raw_quote
        assert "BaseModel" not in item.raw_quote
        assert "{" not in item.raw_quote


def test_a_missing_field_yields_no_evidence():
    # Crossref calls it `container-title`; the paper calls it `journal`
    evidence = paper_to_metadata_evidence(
        crossref_paper(title=[], **{"container-title": []}))
    assert {e.locator.field for e in evidence} == {"doi", "publication_year"}


def test_an_absent_doi_yields_no_doi_evidence():
    paper = crossref_paper()
    paper.identifiers.doi = None
    assert all(e.locator.field != "doi" for e in paper_to_metadata_evidence(paper))


def test_derivation_does_not_mutate_the_paper():
    paper = crossref_paper()
    before = copy.deepcopy(paper)
    paper_to_metadata_evidence(paper)
    abstract_to_evidence(paper, paper.abstracts[0])
    assert paper == before


def test_derivation_makes_no_network_call_and_imports_no_provider():
    """A pure function: it transforms an object and touches nothing else.

    Checked on the AST rather than the text, so a docstring that *mentions*
    OpenAlex is not mistaken for a call into it.
    """
    import ast
    import inspect
    import textwrap

    for func in (paper_to_metadata_evidence, abstract_to_evidence):
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for banned in ("httpx", "requests", "urllib.request", "asyncio",
                       "crea.sources.openalex", "crea.sources.crossref",
                       "crea.services.literature"):
            assert banned not in imported, f"{func.__name__} imports {banned}"
        code = ast.unparse(tree)
        assert "await" not in code, f"{func.__name__} awaits something"


def test_evidence_carries_the_papers_provenance():
    paper = crossref_paper()
    paper.raw_hashes["crossref:probe"] = "abc123"
    evidence = paper_to_metadata_evidence(paper)[0]
    assert evidence.paper_id == paper.paper_id
    assert evidence.doi == DOI
    assert evidence.source == "crossref"
    assert evidence.source_id == DOI
    assert evidence.retrieved_at == NOW
    assert evidence.provenance.source == "crossref"
    assert evidence.provenance.source_id == DOI
    assert evidence.provenance.raw_payload_hash == "abc123"
    # and it uses the one Provenance type the project already had
    from crea.core.provenance import Provenance
    assert isinstance(evidence.provenance, Provenance)


def test_a_merged_paper_is_attributed_to_the_source_the_caller_names():
    """A merged Paper no longer knows per field who said what, so we do not guess."""
    paper = crossref_paper()
    paper.sources = ["crossref", "openalex"]
    paper.source_ids = {"crossref": DOI, "openalex": "W1"}

    default = paper_to_metadata_evidence(paper)
    assert {e.source for e in default} == {"crossref"}

    named = paper_to_metadata_evidence(paper, source="openalex", source_id="W1")
    assert {e.source for e in named} == {"openalex"}
    assert {e.source_id for e in named} == {"W1"}


# ===========================================================================
# Abstract -> Evidence
# ===========================================================================


def test_a_crossref_abstract_stays_secondary():
    paper = crossref_paper()
    evidence = abstract_to_evidence(paper, paper.abstracts[0])
    assert evidence.evidence_type is EvidenceType.ABSTRACT
    assert evidence.strength is EvidenceStrength.SECONDARY_METADATA_EVIDENCE
    assert evidence.is_reconstructed is False
    assert evidence.source == AbstractSource.CROSSREF.value
    assert evidence.raw_quote == paper.abstracts[0].text


def test_an_openalex_reconstruction_is_never_promoted():
    paper = openalex_paper()
    record = next(r for r in paper.abstracts
                  if r.source is AbstractSource.OPENALEX_RECONSTRUCTED)
    evidence = abstract_to_evidence(paper, record)
    assert evidence.is_reconstructed is True
    assert evidence.strength is EvidenceStrength.DISCOVERY_EVIDENCE
    assert evidence.strength is not EvidenceStrength.PRIMARY_DOCUMENT_EVIDENCE
    assert evidence.source == AbstractSource.OPENALEX_RECONSTRUCTED.value
    # the flag is on the Evidence itself, not inferred from the source name
    assert evidence.provenance.params["reconstructed"] is True


def test_an_abstract_with_no_provenance_evidence_is_read_as_discovery():
    """No abstract given means we do not assume a publisher deposit."""
    assert strength_for(EvidenceType.ABSTRACT) is EvidenceStrength.DISCOVERY_EVIDENCE


def test_a_blank_abstract_yields_no_evidence():
    paper = crossref_paper()
    record = paper.abstracts[0].model_copy(update={"text": "   "})
    assert abstract_to_evidence(paper, record) is None


# ===========================================================================
# strength is never wrongly upgraded
# ===========================================================================


def test_no_metadata_evidence_is_ever_primary():
    paper = crossref_paper()
    for evidence in paper_to_metadata_evidence(paper):
        assert evidence.strength is EvidenceStrength.SECONDARY_METADATA_EVIDENCE
        assert may_support_a_chemical_fact(
            evidence.evidence_type, evidence.strength) is False


def test_having_a_doi_does_not_create_evidence_by_itself():
    """A DOI is a fact, not a statement. Nothing is produced for it alone."""
    paper = crossref_paper()
    evidence = paper_to_metadata_evidence(paper)
    doi_items = [e for e in evidence if e.locator.field == "doi"]
    assert len(doi_items) == 1
    assert doi_items[0].evidence_type is EvidenceType.METADATA
    assert doi_items[0].strength is not EvidenceStrength.PRIMARY_DOCUMENT_EVIDENCE


def test_openalex_oa_metadata_is_not_primary_evidence():
    paper = unpaywall_paper("is_oa_green")
    for evidence in EvidenceService.registry_from_paper(paper).items.values():
        assert evidence.strength is not EvidenceStrength.PRIMARY_DOCUMENT_EVIDENCE


@pytest.mark.parametrize("evidence_type,expected", [
    (EvidenceType.METADATA, EvidenceStrength.SECONDARY_METADATA_EVIDENCE),
    (EvidenceType.STRUCTURED_DATABASE, EvidenceStrength.SECONDARY_METADATA_EVIDENCE),
    (EvidenceType.FULLTEXT, EvidenceStrength.PRIMARY_DOCUMENT_EVIDENCE),
    (EvidenceType.EXPERIMENTAL, EvidenceStrength.PRIMARY_DOCUMENT_EVIDENCE),
    (EvidenceType.USER_PROVIDED_DOCUMENT, EvidenceStrength.USER_PROVIDED_EVIDENCE),
])
def test_strength_by_type(evidence_type, expected):
    assert strength_for(evidence_type) is expected


# ===========================================================================
# one Evidence, one source
# ===========================================================================


def test_evidence_carries_exactly_one_source():
    evidence = paper_to_metadata_evidence(crossref_paper())[0]
    assert isinstance(evidence.source, str)
    assert isinstance(evidence.source_id, str)
    # multi-source support is a Claim with several refs, never one Evidence
    # with several sources, so there is nothing composite here to check
    assert not hasattr(evidence, "sources")


def test_the_wrong_object_is_rejected_legibly():
    from crea.chem.normalize import normalize

    with pytest.raises(TypeError, match="expected a core.models.literature.Paper"):
        paper_to_metadata_evidence(normalize("CCO"))


# ===========================================================================
# notes are never machine logic
# ===========================================================================


def test_evidence_logic_does_not_read_notes():
    import inspect

    from crea.services import evidence as module

    source = inspect.getsource(module)
    # the word may appear in prose, but nothing may branch on it
    assert "in e.access.notes" not in source
    assert "in paper.access.notes" not in source
    assert 'in rec.notes' not in source
    assert "notes and" not in source

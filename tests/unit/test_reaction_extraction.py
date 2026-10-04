"""Experimental text -> ReactionRecord, and the checks that stand between a
model's proposal and a fact.

The interesting tests here are the ones where the model is wrong. A happy-path
extraction proves very little; what matters is that a fabricated evidence id, a
tampered number, a guessed pressure and a silent resolution of a conflict all
fail.
"""

import tempfile
import pathlib

import pytest

from crea.core.models.evidence import Evidence, EvidenceType
from crea.core.models.literature import EvidenceStrength
from crea.core.models.reaction import (
    ComponentRole,
    ExtractionOutcome,
    ExtractionProposal,
    FieldProposal,
    FieldStatus,
    ReactionRecord,
    YieldType,
)
from crea.core.policy.reaction import POLICY_VERSION, validate_quantity, validate_yield_type
from crea.services.document_evidence import search_document
from crea.services.documents import LocalDocumentService
from crea.services.reaction_extraction import ReactionExtractor
from crea.sources.pdf import PypdfDocumentParser
from tests.fakes import pdfs


# --- the evidence the extractor is allowed to read --------------------------


@pytest.fixture(scope="module")
def workspace():
    return pathlib.Path(tempfile.mkdtemp(prefix="crea-reaction-"))


def evidence_from(pages, query="CuI"):
    """The real path: a synthetic PDF -> the real parser -> real Evidence."""
    directory = pathlib.Path(tempfile.mkdtemp(prefix="crea-reaction-"))
    path = directory / "procedure.pdf"
    path.write_bytes(pdfs.make_pdf(pages))
    parsed = LocalDocumentService(PypdfDocumentParser()).ingest(path)
    hits = search_document(parsed, query)
    assert hits, "the fixture must produce evidence to read"
    return parsed, [h.evidence for h in hits]


def proposal(**overrides) -> ExtractionProposal:
    """A well-behaved extraction of the worked example.

    Every field cites evidence and every number appears in the quote, which is
    what a correct model produces. The tests below then break one of those
    things at a time.
    """
    base = dict(
        description="CuI-catalyzed coupling of phenylacetylene",
        description_evidence_refs=[],
        components=[
            FieldProposal(name="phenylacetylene", value="phenylacetylene",
                          component_role="reactant", status=FieldStatus.EXPLICIT,
                          evidence_refs=[], raw_text="Phenylacetylene (1.0 mmol),"),
            FieldProposal(name="CuI", value="CuI", component_role="catalyst",
                          status=FieldStatus.EXPLICIT, evidence_refs=[],
                          raw_text="CuI (10 mol%),"),
            FieldProposal(name="DBU", value="DBU", component_role="reagent",
                          status=FieldStatus.EXPLICIT, evidence_refs=[],
                          raw_text="DBU (2.0 equiv)"),
            FieldProposal(name="DMF", value="DMF", component_role="solvent",
                          status=FieldStatus.EXPLICIT, evidence_refs=[],
                          raw_text="were dissolved in DMF (5 mL)."),
        ],
        conditions=[
            FieldProposal(name="temperature", value=80, raw_value="80", unit="C",
                          raw_unit="C", status=FieldStatus.EXPLICIT,
                          evidence_refs=[], raw_text="stirred at 80 C"),
            FieldProposal(name="time", value=12, raw_value="12", unit="h",
                          raw_unit="h", status=FieldStatus.EXPLICIT,
                          evidence_refs=[], raw_text="for 12 h."),
            FieldProposal(name="yield_value", value=82, raw_value="82", unit="%",
                          raw_unit="%", status=FieldStatus.EXPLICIT,
                          evidence_refs=[], raw_text="isolated in 82% yield."),
        ],
        yield_type="unknown",
        yield_type_evidence_refs=[],
    )
    base.update(overrides)
    return ExtractionProposal(**base)


def make_evidence(text: str, eid: str = "e1") -> Evidence:
    """A real Evidence object with an arbitrary quote.

    Used where a test needs the quote to contain something a generated PDF would
    not naturally say -- an impossible yield, say.
    """
    from crea.core.models.evidence import Evidence, Locator
    from crea.core.provenance import Provenance

    return Evidence.build(
        evidence_type=EvidenceType.EXPERIMENTAL,
        strength=EvidenceStrength.USER_PROVIDED_EVIDENCE,
        source="user_provided", source_id="doc", raw_quote=text,
        locator=Locator(locator_type="document_region", document_id="doc",
                        page_start=1, page_end=1, section="Experimental"),
        retrieved_at="2026-01-01T00:00:00Z",
        provenance=Provenance(tool="test", source="user_provided"),
    )


def bind(p: ExtractionProposal, evidence) -> ExtractionProposal:
    """Point every field at the first evidence -- the well-behaved case."""
    eid = evidence[0].evidence_id
    data = p.model_dump()
    data["description_evidence_refs"] = [eid]
    for group in ("components", "conditions"):
        for field in data[group]:
            field["evidence_refs"] = [eid]
    return ExtractionProposal.model_validate(data)


# ===========================================================================
# the happy path, end to end from a PDF
# ===========================================================================


def test_the_worked_example_extracts_every_stated_field():
    _parsed, evidence = evidence_from(pdfs.EXPERIMENTAL)
    result = ReactionExtractor(None).extract(
        evidence, document_id="doc", proposal=bind(proposal(), evidence))

    assert result.outcome is ExtractionOutcome.OK, result.rejected
    record = result.records[0]
    c = record.conditions

    assert [x.name for x in c.reactants] == ["phenylacetylene"]
    assert [x.name for x in c.catalysts] == ["CuI"]
    assert [x.name for x in c.reagents] == ["DBU"]
    assert [x.name for x in c.solvents] == ["DMF"]

    assert c.temperature.value == 80 and c.temperature.unit == "C"
    assert c.time.value == 12 and c.time.unit == "h"
    assert c.yield_value.value == 82 and c.yield_value.unit == "%"


def test_every_field_carries_its_own_evidence_reference():
    _parsed, evidence = evidence_from(pdfs.EXPERIMENTAL)
    record = ReactionExtractor(None).extract(
        evidence, document_id="doc", proposal=bind(proposal(), evidence)).records[0]
    eid = evidence[0].evidence_id

    assert record.conditions.temperature.evidence_refs == [eid]
    assert record.conditions.time.evidence_refs == [eid]
    assert record.conditions.yield_value.evidence_refs == [eid]
    for group in (record.conditions.reactants, record.conditions.catalysts,
                  record.conditions.solvents):
        for item in group:
            assert item.evidence_refs == [eid]
    assert record.evidence_refs == [eid]
    assert record.policy_version == POLICY_VERSION


def test_the_original_text_travels_with_the_record():
    _parsed, evidence = evidence_from(pdfs.EXPERIMENTAL)
    record = ReactionExtractor(None).extract(
        evidence, document_id="doc", proposal=bind(proposal(), evidence)).records[0]
    assert "CuI" in record.raw_experimental_text
    assert "82%" in record.raw_experimental_text


# ===========================================================================
# 36: nothing is invented
# ===========================================================================


def test_an_unmentioned_condition_stays_unmentioned():
    """Pressure is the obvious one: a reaction is usually at ambient."""
    _parsed, evidence = evidence_from(pdfs.TEMPERATURE_ONLY, query="stirred")
    result = ReactionExtractor(None).extract(
        evidence, document_id="doc",
        proposal=bind(proposal(), evidence))

    record = result.records[0]
    assert record.conditions.pressure.stated is False
    assert record.conditions.pressure.value is None
    # and a filled-in atmosphere would be the same mistake in a different field
    assert record.conditions.atmosphere == ""


def test_no_smiles_is_invented_from_a_name():
    _parsed, evidence = evidence_from(pdfs.EXPERIMENTAL)
    record = ReactionExtractor(None).extract(
        evidence, document_id="doc", proposal=bind(proposal(), evidence)).records[0]
    for item in record.conditions.reactants + record.conditions.catalysts:
        assert not hasattr(item, "smiles")
        assert item.name == item.name        # a name, left as written


# ===========================================================================
# 37: yield type is earned
# ===========================================================================


def test_a_bare_yield_does_not_become_isolated():
    _parsed, evidence = evidence_from(pdfs.EXPERIMENTAL)
    record = ReactionExtractor(None).extract(
        evidence, document_id="doc", proposal=bind(proposal(), evidence)).records[0]
    assert record.conditions.yield_value.value == 82
    assert record.conditions.yield_type is YieldType.UNKNOWN


def test_isolated_is_recorded_when_the_text_says_so():
    """The same page does say "isolated" -- but the field still needs its own
    evidence, which is the part being tested."""
    _parsed, evidence = evidence_from(pdfs.EXPERIMENTAL)
    data = bind(proposal(), evidence).model_dump()
    data["yield_type"] = "isolated"
    data["yield_type_evidence_refs"] = [evidence[0].evidence_id]
    record = ReactionExtractor(None).extract(
        evidence, document_id="doc",
        proposal=ExtractionProposal.model_validate(data)).records[0]
    assert record.conditions.yield_type is YieldType.ISOLATED


def test_a_yield_type_without_evidence_is_unknown():
    kind, problem = validate_yield_type("isolated", [], "isolated in 82% yield")
    assert kind is YieldType.UNKNOWN
    assert "no supporting evidence" in problem


def test_an_unrecognised_yield_kind_is_unknown_not_guessed():
    kind, problem = validate_yield_type("spectacular", ["e1"], "82% yield")
    assert kind is YieldType.UNKNOWN


# ===========================================================================
# 38: a fabricated evidence id is refused
# ===========================================================================


def test_a_field_citing_an_unknown_evidence_is_rejected():
    _parsed, evidence = evidence_from(pdfs.EXPERIMENTAL)
    data = bind(proposal(), evidence).model_dump()
    data["conditions"][0]["evidence_refs"] = ["made-up-id"]
    data["components"][0]["evidence_refs"] = ["made-up-id"]

    result = ReactionExtractor(None).extract(
        evidence, document_id="doc",
        proposal=ExtractionProposal.model_validate(data))

    reasons = " ".join(r.reason for r in result.rejected)
    assert "made-up-id" in reasons
    record = result.records[0]
    assert record.conditions.temperature.evidence_refs == []
    assert record.conditions.temperature.stated is False
    assert record.conditions.reactants == []


def test_no_evidence_at_all_means_no_record():
    result = ReactionExtractor(None).extract([], document_id="doc")
    assert result.outcome is ExtractionOutcome.REJECTED
    assert result.records == []


def test_everything_rejected_means_no_record():
    _parsed, evidence = evidence_from(pdfs.EXPERIMENTAL)
    data = bind(proposal(), evidence).model_dump()
    for group in ("components", "conditions"):
        for field in data[group]:
            field["evidence_refs"] = ["made-up-id"]
    result = ReactionExtractor(None).extract(
        evidence, document_id="doc",
        proposal=ExtractionProposal.model_validate(data))
    assert result.records == []
    assert result.outcome is ExtractionOutcome.REJECTED


# ===========================================================================
# 39: a tampered number is caught against the quote
# ===========================================================================


def test_a_number_that_is_not_in_the_quote_is_rejected():
    """The evidence says 82%. A proposal of 92% does not get through."""
    _parsed, evidence = evidence_from(pdfs.EXPERIMENTAL)
    data = bind(proposal(), evidence).model_dump()
    data["conditions"][2].update({"value": 92, "raw_value": "92"})

    result = ReactionExtractor(None).extract(
        evidence, document_id="doc",
        proposal=ExtractionProposal.model_validate(data))

    reasons = " ".join(r.reason for r in result.rejected)
    assert "does not appear in the cited text" in reasons
    record = result.records[0]
    # the tampered value is gone, and nothing quietly replaced it with 82
    assert record.conditions.yield_value.value != 92
    assert record.conditions.yield_value.stated is False


def test_a_subtle_digit_change_is_caught():
    _parsed, evidence = evidence_from(pdfs.EXPERIMENTAL)
    data = bind(proposal(), evidence).model_dump()
    data["conditions"][0].update({"value": 8, "raw_value": "8"})   # 80 -> 8
    result = ReactionExtractor(None).extract(
        evidence, document_id="doc",
        proposal=ExtractionProposal.model_validate(data))
    record = result.records[0]
    assert record.conditions.temperature.stated is False


def test_an_out_of_range_yield_is_refused_not_repaired():
    """A yield above 100 is a typo or a misparse. We refuse it; we do not fix it."""
    evidence = [make_evidence("The product was isolated in 182% yield.")]
    data = proposal().model_dump()
    data["conditions"] = [data["conditions"][2]]
    data["conditions"][0].update({"value": 182, "raw_value": "182",
                                  "evidence_refs": [evidence[0].evidence_id],
                                  "raw_text": "isolated in 182% yield"})
    result = ReactionExtractor(None).extract(
        evidence, document_id="doc",
        proposal=ExtractionProposal.model_validate(data))
    reasons = " ".join(r.reason for r in result.rejected)
    assert "outside 0-100" in reasons
    # refused outright: no record for a yield we cannot accept, and in
    # particular not silently rewritten to 82
    assert result.records == [] or result.records[0].conditions.yield_value.stated is False


# ===========================================================================
# 40: units are reported, never converted
# ===========================================================================


def test_units_are_not_converted():
    _parsed, evidence = evidence_from(pdfs.EXPERIMENTAL)
    record = ReactionExtractor(None).extract(
        evidence, document_id="doc", proposal=bind(proposal(), evidence)).records[0]
    t = record.conditions.temperature
    assert t.value == 80
    assert t.unit == "C"            # not 353, not K
    assert t.raw_value == "80"
    assert t.raw_unit == "C"


def test_an_unrecognised_unit_is_kept_not_dropped_and_not_guessed():
    field = FieldProposal(name="temperature", value=80, raw_value="80",
                          unit="furlongs", raw_unit="furlongs",
                          status=FieldStatus.EXPLICIT,
                          evidence_refs=[], raw_text="80 furlongs")
    quotes = "the reaction ran at 80 furlongs"
    quantity, problem = validate_quantity("temperature", field, quotes)
    assert problem is None
    assert quantity.unit == "furlongs"       # kept verbatim
    assert quantity.value == 80               # not reinterpreted


def test_a_bare_number_is_not_assumed_to_be_degrees():
    field = FieldProposal(name="temperature", value=80, raw_value="80", unit="",
                          status=FieldStatus.EXPLICIT, evidence_refs=[],
                          raw_text="80")
    quantity, problem = validate_quantity("temperature", field, "warmed to 80")
    # the value is kept and the unit is marked unknown, not filled in
    assert problem is None
    assert quantity.value == 80
    assert quantity.unit == "UNKNOWN"


# ===========================================================================
# 41/42: conflicts and per-field attribution across several passages
# ===========================================================================


def test_two_passages_that_disagree_are_a_conflict_not_a_coin_flip():
    parsed = pathlib.Path(tempfile.mkdtemp(prefix="crea-conflict-"))
    path = parsed / "c.pdf"
    path.write_bytes(pdfs.make_pdf([pdfs.CONFLICTING[0]]))
    first = search_document(
        LocalDocumentService(PypdfDocumentParser()).ingest(path), "stirred")[0].evidence

    path2 = parsed / "c2.pdf"
    path2.write_bytes(pdfs.make_pdf([pdfs.CONFLICTING[1]]))
    second = search_document(
        LocalDocumentService(PypdfDocumentParser()).ingest(path2), "stirred")[0].evidence

    data = ExtractionProposal(
        description="a procedure",
        description_evidence_refs=[first.evidence_id],
        conditions=[
            FieldProposal(name="temperature", value=80, raw_value="80", unit="C",
                          status=FieldStatus.EXPLICIT,
                          evidence_refs=[first.evidence_id],
                          raw_text="stirred at 80 C"),
            FieldProposal(name="temperature", value=90, raw_value="90", unit="C",
                          status=FieldStatus.EXPLICIT,
                          evidence_refs=[second.evidence_id],
                          raw_text="stirred at 90 C"),
        ],
        yield_type="unknown",
    )
    result = ReactionExtractor(None).extract([first, second], document_id="doc",
                                            proposal=data)
    assert result.conflicts
    conflict = next(c for c in result.conflicts if c.field == "temperature")
    assert set(conflict.values.values()) == {"80 C", "90 C"}
    assert "neither value was chosen" in conflict.note


def test_fields_from_different_passages_keep_their_own_references():
    parsed = pathlib.Path(tempfile.mkdtemp(prefix="crea-multi-"))
    path = parsed / "a.pdf"
    path.write_bytes(pdfs.make_pdf([pdfs.CONFLICTING[0]]))
    a = search_document(LocalDocumentService(PypdfDocumentParser()).ingest(path),
                        "stirred")[0].evidence
    path2 = parsed / "b.pdf"
    path2.write_bytes(pdfs.make_pdf([pdfs.CONFLICTING[1]]))   # 90 C: different text
    b = search_document(LocalDocumentService(PypdfDocumentParser()).ingest(path2),
                        "stirred")[0].evidence
    assert a.evidence_id != b.evidence_id, (
        "the two passages must differ, or their evidence ids coincide and the "
        "per-field attribution this test checks would be vacuous")

    data = ExtractionProposal(
        description="two passages",
        description_evidence_refs=[a.evidence_id],
        conditions=[
            FieldProposal(name="temperature", value=80, raw_value="80", unit="C",
                          status=FieldStatus.EXPLICIT,
                          evidence_refs=[a.evidence_id], raw_text="stirred at 80 C"),
            FieldProposal(name="time", value=12, raw_value="12", unit="h",
                          status=FieldStatus.EXPLICIT,
                          evidence_refs=[b.evidence_id], raw_text="for 12 h"),
        ],
        yield_type="unknown",
    )
    record = ReactionExtractor(None).extract([a, b], document_id="doc",
                                            proposal=data).records[0]
    assert record.conditions.temperature.evidence_refs == [a.evidence_id]
    assert record.conditions.time.evidence_refs == [b.evidence_id]
    # each field points at its own passage, not at a single record-wide source
    assert (record.conditions.temperature.evidence_refs
            != record.conditions.time.evidence_refs)


# ===========================================================================
# roles stay unknown when the text does not assign one
# ===========================================================================


def test_an_unassigned_component_keeps_an_unknown_role():
    _parsed, evidence = evidence_from(pdfs.EXPERIMENTAL)
    data = bind(proposal(), evidence).model_dump()
    data["components"][1]["component_role"] = ""
    record = ReactionExtractor(None).extract(
        evidence, document_id="doc",
        proposal=ExtractionProposal.model_validate(data)).records[0]
    roles = {x.role for x in record.conditions.catalysts + record.conditions.reagents
             + record.conditions.reactants + record.conditions.solvents
             + record.conditions.unassigned}
    assert ComponentRole.UNKNOWN in roles
    # and the substance itself is still there, just not filed under a role
    assert [x.name for x in record.conditions.unassigned] == ["CuI"]
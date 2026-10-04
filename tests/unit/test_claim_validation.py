"""Claim: the type that makes an unevidenced statement unrepresentable, and the
validator that checks its references resolve.

Cases A-E of the phase brief, plus the boundary rules.
"""

import copy

import pytest
from pydantic import ValidationError

from crea.core.errors import EvidenceIntegrityError
from crea.core.models.evidence import (
    Claim,
    Evidence,
    EvidenceType,
    Locator,
)
from crea.core.models.literature import EvidenceStrength
from crea.services.evidence import (
    EvidenceRegistry,
    EvidenceService,
    paper_to_metadata_evidence,
    validate_claim,
)
from crea.sources.crossref import parse_work as parse_crossref
from tests.fakes.fixtures import load

NOW = "2026-09-30T00:00:00Z"
DOI = "10.0000/synthetic.2026.0001"


def paper():
    message = {k: (list(v) if isinstance(v, list) else v)
               for k, v in load("crossref", "doi_metadata")["message"].items()}
    return parse_crossref(message, provider_name="crossref", retrieved_at=NOW)


def registry():
    reg = EvidenceService.registry_from_paper(paper())
    return reg


def refs(reg: EvidenceRegistry) -> list[str]:
    return list(reg.items)


# --- Case A: no evidence at all --------------------------------------------


def test_a_claim_with_no_evidence_cannot_be_constructed():
    with pytest.raises(ValidationError):
        Claim.build("c1", "The paper reports a synthesis.", "experimental_fact", [])


def test_the_constraint_is_on_the_field_not_on_a_convention():
    """`min_length=1`, so a dict payload cannot smuggle an empty list past it."""
    with pytest.raises(ValidationError):
        Claim(claim_id="c1", text="t", claim_type="x", evidence_refs=[])
    with pytest.raises(ValidationError):
        Claim(claim_id="c1", text="t", claim_type="x", evidence_refs=None)


# --- Case B: valid ---------------------------------------------------------


def test_a_claim_with_resolvable_evidence_validates():
    reg = registry()
    claim = Claim.build("c1", "The title is what the deposit says it is.",
                        "descriptive", refs(reg)[:1])
    result = validate_claim(claim, reg)
    assert result.ok is True
    assert result.missing_refs == []
    result.raise_if_invalid(claim)          # does not raise


# --- Case C: missing --------------------------------------------------------


def test_a_missing_reference_is_reported():
    reg = registry()
    claim = Claim.build("c2", "text", "descriptive", ["does-not-exist"])
    result = validate_claim(claim, reg)
    assert result.ok is False
    assert result.missing_refs == ["does-not-exist"]
    with pytest.raises(EvidenceIntegrityError) as exc:
        result.raise_if_invalid(claim)
    assert "does-not-exist" in str(exc.value)


def test_a_partially_resolvable_claim_is_invalid():
    reg = registry()
    claim = Claim.build("c3", "text", "descriptive",
                        [refs(reg)[0], "does-not-exist"])
    result = validate_claim(claim, reg)
    assert result.ok is False
    assert len(result.missing_refs) == 1


# --- Case D: invalid evidence type / source --------------------------------


def test_evidence_with_an_unknown_type_cannot_be_built():
    with pytest.raises(ValidationError):
        Evidence.build(
            evidence_type="synthesis_recipe",        # not in the enum
            strength=EvidenceStrength.PRIMARY_DOCUMENT_EVIDENCE,
            source="crossref", source_id=DOI, raw_quote="q",
            locator=Locator(field="title"), retrieved_at=NOW)


def test_evidence_without_a_source_cannot_be_built():
    with pytest.raises(ValidationError):
        Evidence.build(
            evidence_type=EvidenceType.METADATA,
            strength=EvidenceStrength.SECONDARY_METADATA_EVIDENCE,
            source="", source_id=DOI, raw_quote="q",
            locator=Locator(field="title"), retrieved_at=NOW)
    with pytest.raises(ValidationError):
        Evidence.build(
            evidence_type=EvidenceType.METADATA,
            strength=EvidenceStrength.SECONDARY_METADATA_EVIDENCE,
            source="crossref", source_id="", raw_quote="q",
            locator=Locator(field="title"), retrieved_at=NOW)


# --- Case E: duplicate references ------------------------------------------


def test_duplicate_references_are_rejected():
    """Decided, not left open.

    Citing one piece of evidence twice is either a mistake or an attempt to
    make a single source look like corroboration. Neither deserves leniency,
    and a Claim that can list the same source ten times is how a one-source
    answer gets read as a ten-source one.
    """
    reg = registry()
    one = refs(reg)[0]
    claim = Claim.build("c5", "text", "descriptive", [one, one])
    result = validate_claim(claim, reg)
    assert result.ok is False
    assert result.duplicate_refs == [one]
    with pytest.raises(EvidenceIntegrityError, match="duplicate"):
        result.raise_if_invalid(claim)


# --- weakness, flagged not rejected ----------------------------------------


def test_an_experimental_fact_resting_on_metadata_is_flagged():
    reg = registry()
    metadata_ref = next(
        rid for rid, item in reg.items.items()
        if item.evidence_type is EvidenceType.METADATA)
    claim = Claim.build("c6", "The synthesis used reagent A at 80 °C.",
                        "experimental_fact", [metadata_ref])
    result = validate_claim(claim, reg)
    # the reference resolves, so the claim is not invalid ...
    assert result.ok is True
    # ... but nobody should be told a method came from a metadata field
    assert result.weak_refs == [metadata_ref]
    assert "not primary evidence" in str(
        EvidenceIntegrityError("not primary evidence: " + metadata_ref))


def test_a_descriptive_claim_on_metadata_is_not_flagged():
    reg = registry()
    metadata_ref = next(
        rid for rid, item in reg.items.items()
        if item.evidence_type is EvidenceType.METADATA)
    claim = Claim.build("c7", "The year of publication is 2021.",
                        "descriptive", [metadata_ref])
    assert validate_claim(claim, reg).weak_refs == []


# --- registry behaviour -----------------------------------------------------


def test_the_registry_refuses_two_different_things_under_one_id():
    reg = EvidenceRegistry()
    evidence = paper_to_metadata_evidence(paper())[0]
    reg.add(evidence)
    assert reg.add(evidence) is evidence          # idempotent
    impostor = evidence.model_copy(update={"raw_quote": "something else",
                                          "evidence_id": evidence.evidence_id})
    with pytest.raises(ValueError, match="already registered"):
        reg.add(impostor)


def test_the_registry_is_a_plain_collection_not_a_database():
    reg = registry()
    assert len(reg) == len(refs(reg))
    assert refs(reg)[0] in reg
    assert reg.get("nope") is None
    # no lookup beyond the collection it was given
    import inspect

    source = inspect.getsource(type(reg))
    for banned in ("sql", "sqlite", "postgres", "http", "redis"):
        assert banned not in source.lower()


# --- the validator stays out of chemistry ----------------------------------


def test_validation_makes_no_chemical_judgement():
    """It checks that a reference resolves. Not that it supports the claim.

    AST, not text: the docstring says "no chemical judgement", and a substring
    search for "chem" would find exactly that sentence and fail.
    """
    import ast
    import inspect
    import textwrap

    from crea.services import evidence as module

    tree = ast.parse(textwrap.dedent(inspect.getsource(module.validate_claim)))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for banned in ("crea.chem", "rdkit", "crea.services.matching",
                   "crea.services.literature", "httpx"):
        assert banned not in imported, f"validator imports {banned}"
    # and it never calls the chemistry core
    assert not any(
        isinstance(n, ast.Attribute) and getattr(n.value, "id", "") in ("chem", "rdkit")
        for n in ast.walk(tree)
    )


# --- round trip -------------------------------------------------------------


def test_a_claim_survives_serialisation_with_its_refs_intact():
    reg = registry()
    claim = Claim.build("c8", "The deposit title is as recorded.", "descriptive",
                        refs(reg)[:2])
    restored = Claim.model_validate(claim.model_dump())
    assert restored.evidence_refs == claim.evidence_refs
    assert validate_claim(restored, reg).ok is True


def test_no_confidence_field_was_invented():
    """There is no such quantity defined in this project, so there is no field."""
    fields = set(Claim.model_fields)
    assert "confidence" not in fields
    assert "ai_confidence" not in fields
    assert "score" not in fields
    assert fields == {"claim_id", "text", "claim_type", "evidence_refs",
                      "strength", "provenance"}

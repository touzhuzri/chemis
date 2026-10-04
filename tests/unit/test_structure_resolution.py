"""Structure resolution: the deterministic path, the lookup path, and the honest answers.

Three groups of behaviour are tested here.

**Determinism.** A SMILES or an InChI is parsed by RDKit and nothing else. A
string that does not parse is `INVALID` -- not a repaired version of the user's
intent, and never a suggestion standing in for a finding.

**Lookups.** An InChIKey is a key and is not inverted. A name reaches a
database. Every candidate that comes back carries provenance.

**Absence and failure.** `NOT_FOUND` and `UNAVAILABLE` are different answers
and are produced by different causes. Merging them is the single most
consequential error this layer could make.
"""

from __future__ import annotations

import pytest

from crea.core.errors import ProviderError
from crea.core.models.identity import (
    ChemicalIdentityCandidate,
    IdentityBasis,
    ImageProvenance,
    MatchedBy,
    RecognitionStage,
    StructureInput,
    StructureInputKind,
    StructureResolutionStatus,
)
from crea.services.identity import IdentityResolutionService
from tests.fakes.compound import (
    ASCORBATE,
    ASCORBIC_ACID,
    FailingCompoundProvider,
    PHENYLACETYLENE,
    SODIUM_ASCORBATE,
    FakeCompoundProvider,
)

PA = "C#Cc1ccccc1"
PA_INCHI = "InChI=1S/C8H6/c1-2-8-6-4-3-5-7-8/h1,3-7H"
PA_KEY = "UEXCJVNBTNXOEH-UHFFFAOYSA-N"


def smiles_input(value: str) -> StructureInput:
    return StructureInput(kind="SMILES", value=value)


# ===========================================================================
# §3 -- deterministic SMILES
# ===========================================================================


def test_a_valid_smiles_resolves_and_is_canonicalised():
    """The deterministic path: parse, canonicalise, derive InChI/InChIKey, done.

    Nothing was looked up and nothing was guessed, and the resolution says so
    in `rules_applied` so an auditor can see which path produced the answer.
    """
    service = IdentityResolutionService()
    result = service.resolve(smiles_input("c1ccccc1C#C"))

    assert result.status is StructureResolutionStatus.RESOLVED
    assert result.is_resolved
    selected = result.require_resolved()
    assert selected.canonical_smiles == "C#Cc1ccccc1"
    assert selected.inchikey == PA_KEY
    assert selected.inchi == PA_INCHI
    assert selected.identity_basis is IdentityBasis.EXACT_STRUCTURE
    assert "deterministic_parse_wins" in result.rules_applied
    assert "no_llm_correction" in result.rules_applied
    assert selected.matched_by is MatchedBy.SMILES


def test_an_invalid_smiles_is_invalid_and_not_a_repaired_guess():
    """§3's hard rule: 不能让 LLM "修正"用户 SMILES 后假装成功.

    The failure mode this prevents is subtle and real: a model that "helpfully"
    fixes `c1ccccc` into `c1ccccc1` produces a structure the user did not write,
    reports it as resolved, and every later chemistry result is about a
    molecule nobody asked about.
    """
    service = IdentityResolutionService()
    for bad in ("C#Cc1ccccc", "this is not a smiles", "C(", ""):
        result = service.resolve(smiles_input(bad))
        assert result.status is StructureResolutionStatus.INVALID, bad
        assert result.selected is None
        assert result.candidates == []
        assert "PARSE_FAILED" in result.reason_codes or "EMPTY_INPUT" in result.reason_codes


def test_the_deterministic_path_needs_no_provider():
    """A structure the user supplied does not need a database.

    If resolving a SMILES required a provider, an offline machine could not
    read its own input, and the deterministic path would be a special case
    rather than the first resort.
    """
    service = IdentityResolutionService(compound_provider=None)
    assert service.resolve(smiles_input(PA)).is_resolved


# ===========================================================================
# §4 -- InChI and InChIKey
# ===========================================================================


def test_a_valid_inchi_resolves_deterministically():
    service = IdentityResolutionService()
    result = service.resolve(StructureInput(kind="INCHI", value=PA_INCHI))
    assert result.status is StructureResolutionStatus.RESOLVED
    assert result.require_resolved().canonical_smiles == "C#Cc1ccccc1"


def test_an_inchi_that_is_not_an_inchi_is_invalid():
    """A malformed InChI is not quietly retried as a SMILES.

    Guessing which notation the user meant is the first step toward resolving
    something they did not ask about.
    """
    service = IdentityResolutionService()
    for bad in ("InChI=1S/garbage", "C#Cc1ccccc1", "InChI", ""):
        result = service.resolve(StructureInput(kind="INCHI", value=bad))
        assert result.status is StructureResolutionStatus.INVALID, bad
        assert result.reason_codes


def test_an_inchikey_is_looked_up_and_never_inverted():
    """§4: an InChIKey cannot be assumed to be reverse-derivable.

    There is no code path in this service that turns a hash into a structure.
    The lookup goes to the provider; if the provider is absent the answer is
    `UNAVAILABLE`, which is a very different statement from "this key names
    nothing".
    """
    service = IdentityResolutionService(
        compound_provider=FakeCompoundProvider(by_inchikey={PA_KEY: [PHENYLACETYLENE]})
    )
    result = service.resolve(StructureInput(kind="INCHIKEY", value=PA_KEY))
    assert result.status is StructureResolutionStatus.RESOLVED
    assert result.require_resolved().source == "PUBCHEM"
    assert result.require_resolved().matched_by is MatchedBy.INCHIKEY

    # and with no provider at all, the honest answer is "could not look"
    bare = IdentityResolutionService().resolve(StructureInput(kind="INCHIKEY", value=PA_KEY))
    assert bare.status is StructureResolutionStatus.UNAVAILABLE
    assert "NO_PROVIDER" in bare.reason_codes


def test_a_cid_that_is_not_a_cid_is_invalid_rather_than_sent_anywhere():
    service = IdentityResolutionService(compound_provider=FakeCompoundProvider())
    for bad in ("", "2244; drop table", "aspirin", "-1"):
        result = service.resolve(StructureInput(kind="PUBCHEM_CID", value=bad))
        assert result.status is StructureResolutionStatus.INVALID, bad
        assert "NOT_A_CID" in result.reason_codes or "EMPTY_INPUT" in result.reason_codes


def test_a_cid_lookup_resolves_one_entry():
    service = IdentityResolutionService(
        compound_provider=FakeCompoundProvider(by_cid={"2244": [PHENYLACETYLENE]})
    )
    result = service.resolve(StructureInput(kind="PUBCHEM_CID", value="2244"))
    assert result.is_resolved
    assert result.require_resolved().matched_by is MatchedBy.CID


# ===========================================================================
# §22 -- NOT_FOUND vs UNAVAILABLE
# ===========================================================================


def test_an_absent_entry_is_not_found():
    """PubChem was consulted and holds nothing. That is a finding."""
    service = IdentityResolutionService(compound_provider=FakeCompoundProvider())
    result = service.resolve(StructureInput(kind="NAME", value="zzzznotarealcompoundzzzz"))
    assert result.status is StructureResolutionStatus.NOT_FOUND
    assert "NOT_FOUND" in result.reason_codes


def test_a_failed_lookup_is_unavailable_and_never_not_found():
    """The distinction §22 is about. A timeout is not a negative finding.

    If these were merged, the agent would tell the user "PubChem has never
    heard of this compound" about a request that never completed -- and that
    sentence is a claim about PubChem that nobody established.
    """
    for exc in (ProviderError("boom"), ProviderError("timeout")):
        service = IdentityResolutionService(
            compound_provider=FailingCompoundProvider(exc)
        )
        result = service.resolve(StructureInput(kind="NAME", value="phenylacetylene"))
        assert result.status is StructureResolutionStatus.UNAVAILABLE
        assert "PROVIDER_FAILED" in result.reason_codes
        assert result.status is not StructureResolutionStatus.NOT_FOUND


def test_an_unusable_provider_is_unavailable_before_anything_is_asked():
    from tests.fakes.compound import UnconfiguredCompoundProvider

    service = IdentityResolutionService(compound_provider=UnconfiguredCompoundProvider())
    result = service.resolve(StructureInput(kind="NAME", value="phenylacetylene"))
    assert result.status is StructureResolutionStatus.UNAVAILABLE


# ===========================================================================
# §17 -- several candidates are never resolved by position
# ===========================================================================


def test_several_distinct_structures_are_ambiguous_and_never_a_guess():
    """§17 in its exact wording: 发现 3 个候选结构，需要进一步确认。

    Not "I think you meant L-ascorbic acid". Not `candidates[0]`. The three
    structures below are the real PubChem entries behind the informal name
    "vitamin C" -- the acid, the anion and the sodium salt -- and they differ
    in protonation and in salt form, which is precisely the case the spec says
    no clear rule may collapse.
    """
    service = IdentityResolutionService(
        compound_provider=FakeCompoundProvider(
            by_name={"vitamin C": [ASCORBIC_ACID, ASCORBATE, SODIUM_ASCORBATE]}
        )
    )
    result = service.resolve(StructureInput(kind="NAME", value="vitamin C"))

    assert result.status is StructureResolutionStatus.AMBIGUOUS
    assert result.selected is None, "an ambiguous resolution has no selected structure"
    assert len(result.candidates) == 3
    assert result.structures_distinguished() is True
    assert "MULTIPLE_CANDIDATES" in result.reason_codes
    assert "never_pick_candidate_zero" in result.rules_applied

    with pytest.raises(Exception):
        result.require_resolved()

    # the reason names the count and hands the decision to the user
    assert "3" in result.reason
    assert "user" in result.reason


def test_the_same_structure_reached_twice_is_not_an_ambiguity():
    """One compound under a primary name and a synonym is one answer.

    Counting spellings as candidates would demand the user choose between
    "aspirin" and "ACETYLSALICYLIC ACID", which is not a chemical question.
    """
    twice = ASCORBIC_ACID.model_copy(
        update={"candidate_id": "ide-pubchem-54670067-syn",
                "matched_by": MatchedBy.SYNONYM, "matched_synonym": "vitamin C"}
    )
    service = IdentityResolutionService(
        compound_provider=FakeCompoundProvider(
            by_name={"vitamin C": [ASCORBIC_ACID, twice]}
        )
    )
    result = service.resolve(StructureInput(kind="NAME", value="vitamin C"))
    assert result.status is StructureResolutionStatus.RESOLVED
    assert len(result.candidates) == 1
    assert result.structures_distinguished() is False


def test_two_candidate_records_without_structures_do_not_create_a_false_ambiguity():
    """A synonym list is not a set of competing molecules.

    PubChem's synonym record carries names and no structure. Two of those are
    not two possibilities for the molecule; they are two words.
    """
    words_only = [
        ChemicalIdentityCandidate(
            source="PUBCHEM", source_id="2244",
            matched_by=MatchedBy.SYNONYM, matched_synonym=s,
        )
        for s in ("aspirin", "acetylsalicylic acid")
    ]
    service = IdentityResolutionService(
        compound_provider=FakeCompoundProvider(by_name={"aspirin": words_only})
    )
    result = service.resolve(StructureInput(kind="NAME", value="aspirin"))
    assert result.structures_distinguished() is False


# ===========================================================================
# provenance: every candidate says where it came from
# ===========================================================================


def test_every_candidate_carries_provenance():
    """§7: a candidate with no source is a candidate nobody can check."""
    service = IdentityResolutionService(
        compound_provider=FakeCompoundProvider(
            by_name={"phenylacetylene": [PHENYLACETYLENE]}
        )
    )
    result = service.resolve(StructureInput(kind="NAME", value="phenylacetylene"))
    for candidate in result.candidates:
        assert candidate.source, "every candidate names its source"
        assert candidate.source_id, "every candidate names the record"
        assert candidate.matched_by is not MatchedBy.UNKNOWN, (
            "and says how it was matched"
        )
    assert result.candidates[0].input_name == "phenylacetylene"


def test_a_synonym_hit_records_the_exact_synonym_that_matched():
    """§10: `matched_synonym` is a fact about the lookup, not a grade.

    Without the string, "matched by synonym" cannot be audited and cannot be
    distinguished from "matched by primary name".
    """
    hit = ASCORBIC_ACID.model_copy(
        update={"matched_by": MatchedBy.SYNONYM, "matched_synonym": "vitamin C"}
    )
    service = IdentityResolutionService(
        compound_provider=FakeCompoundProvider(by_name={"vitamin C": [hit]})
    )
    result = service.resolve(StructureInput(kind="NAME", value="vitamin C"))
    candidate = result.require_resolved()
    assert candidate.matched_by is MatchedBy.SYNONYM
    assert candidate.matched_synonym == "vitamin C"
    # and a synonym match is not identity proof
    assert candidate.identity_basis is not IdentityBasis.EXACT_STRUCTURE


# ===========================================================================
# §12–§15 -- the image path
# ===========================================================================


class ScriptedReader:
    """Stands in for the DECIMER worker. Returns a fixed string, or raises."""

    def __init__(self, smiles: str = "", exc: Exception | None = None) -> None:
        self.smiles = smiles
        self.exc = exc
        self.seen: list[ImageProvenance] = []

    def read(self, provenance: ImageProvenance) -> str:
        self.seen.append(provenance)
        if self.exc is not None:
            raise self.exc
        return self.smiles


def image_input() -> StructureInput:
    return StructureInput(
        kind="IMAGE", value="drawing.png",
        image=ImageProvenance(image_sha256="ab" * 32, source="user_provided",
                              recognizer="decimer", recognizer_version="0.4.1"),
    )


def test_an_image_reading_is_a_candidate_and_never_a_resolution():
    """§15's whole point: OCSR 是识别，不是事实验证.

    Even a perfectly parsable reading is held at `CANDIDATE`. Nothing in the
    chain established that the drawing depicts this compound -- the recogniser
    emitted a string, the parser accepted it, and both of those are facts about
    the *string*.
    """
    service = IdentityResolutionService(image_reader=ScriptedReader(PA))
    result = service.resolve(image_input())

    assert result.status is StructureResolutionStatus.CANDIDATE
    assert result.is_resolved is False
    assert "ocr_is_recognition_not_verification" in result.rules_applied
    assert "image_is_never_resolved" in result.rules_applied

    candidate = result.candidates[0]
    assert candidate.recognition_stage is RecognitionStage.STRUCTURE_CANDIDATE
    assert candidate.canonical_smiles == "C#Cc1ccccc1"
    assert candidate.inchikey == PA_KEY
    assert candidate.source == "DECIMER"


def test_an_image_reading_survives_as_an_unrecognised_candidate_when_it_does_not_parse():
    """A recogniser's output that is not a SMILES is a failed reading, not a structure.

    It is still reported, at `UNPARSED`, so the failure is visible rather than
    silently dropped.
    """
    service = IdentityResolutionService(image_reader=ScriptedReader("c1ccccc"))
    result = service.resolve(image_input())

    assert result.status is StructureResolutionStatus.INVALID
    assert result.candidates, "the failed reading is still reported"
    assert result.candidates[0].recognition_stage is RecognitionStage.UNPARSED
    assert "UNPARSED" in result.reason_codes


def test_an_empty_reading_stays_a_candidate_with_no_structure():
    service = IdentityResolutionService(image_reader=ScriptedReader(""))
    result = service.resolve(image_input())
    assert result.status is StructureResolutionStatus.CANDIDATE
    assert result.candidates[0].recognition_stage is RecognitionStage.IMAGE_RECOGNIZED_CANDIDATE
    assert "EMPTY_READING" in result.reason_codes


def test_with_no_recogniser_the_image_path_says_unavailable_rather_than_inventing():
    """§12: DECIMER is optional and must not block Phase 3.6.

    A missing recogniser is a configuration state, reported as `UNAVAILABLE`.
    The one outcome that must not occur is a structure appearing anyway.
    """
    service = IdentityResolutionService(image_reader=None)
    result = service.resolve(image_input())
    assert result.status is StructureResolutionStatus.UNAVAILABLE
    assert "NO_RECOGNIZER" in result.reason_codes
    assert result.candidates == []


def test_a_crashing_recogniser_is_unavailable_not_a_chemistry_answer():
    service = IdentityResolutionService(image_reader=ScriptedReader(exc=RuntimeError("worker died")))
    result = service.resolve(image_input())
    assert result.status is StructureResolutionStatus.UNAVAILABLE
    assert "RECOGNIZER_FAILED" in result.reason_codes


def test_image_provenance_reaches_the_reader_without_losing_its_fields():
    """§16: the audit trail is carried to the recogniser, not built after it."""
    reader = ScriptedReader(PA)
    service = IdentityResolutionService(image_reader=reader)
    service.resolve(image_input())

    assert len(reader.seen) == 1
    provenance = reader.seen[0]
    assert provenance.image_sha256 == "ab" * 32
    assert provenance.source == "user_provided"
    assert provenance.recognizer == "decimer"
    assert provenance.recognizer_version == "0.4.1"
    # the image itself is referenced, never copied into the record
    assert "image_bytes" not in ImageProvenance.model_fields

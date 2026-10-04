"""The identity models: what may be said, and what may not.

These models are the grammar of Phase 3.6. If a distinction cannot be
expressed here, no service can honour it later, so the tests below are about
*expressiveness* as much as about validation: the case "same connectivity,
different stereochemistry" has to be representable and has to not read as
"same molecule".

The single most important property tested here is negative: `CANDIDATE` is not
`RESOLVED`, and `candidates[0]` is never a substitute for a decision.
"""

from __future__ import annotations

import pytest

from crea.core.models.identity import (
    ChemicalIdentityCandidate,
    ChemicalIdentityResolution,
    IdentityBasis,
    IdentityFacets,
    IdentityNotResolved,
    ImageProvenance,
    MatchedBy,
    RecognitionStage,
    StructureInput,
    StructureInputKind,
    StructureResolutionStatus,
)

PHENYLACETYLENE = "C#Cc1ccccc1"
ASPIRIN_KEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"


def candidate(smiles: str = PHENYLACETYLENE, inchikey: str = ASPIRIN_KEY,
              inchi: str = "", **kwargs) -> ChemicalIdentityCandidate:
    return ChemicalIdentityCandidate(
        source="PUBCHEM",
        source_id=kwargs.pop("source_id", "2244"),
        canonical_smiles=smiles,
        inchi=inchi,
        inchikey=inchikey,
        **kwargs,
    )


# ===========================================================================
# §1 / §2 -- the shapes
# ===========================================================================


def test_the_status_vocabulary_is_complete():
    """Every status §1 names must exist, and `candidate` must be its own value.

    A missing status is not a small gap: it forces the layer above to overload
    one of the survivors, and the overloaded one is always `candidate` -- which
    is then read as `resolved` the first time somebody is in a hurry.
    """
    values = {status.value for status in StructureResolutionStatus}
    assert {
        "input", "candidate", "resolved", "ambiguous",
        "invalid", "not_found", "unavailable",
    } <= values
    assert StructureResolutionStatus.CANDIDATE is not StructureResolutionStatus.RESOLVED


def test_structure_input_takes_every_kind_the_spec_names():
    """SMILES / InCHI / InChIKey / CID / NAME / IMAGE, and the name form works.

    §2 writes `StructureInput(kind="NAME", ...)`, uppercase. The enum's values
    are lowercase like the rest of the codebase. A caller must not have to know
    which of the two the constructor wanted.
    """
    for kind in ("SMILES", "INCHI", "INCHIKEY", "PUBCHEM_CID", "NAME", "IMAGE"):
        inp = StructureInput(kind=kind, value="x")
        assert inp.kind in tuple(StructureInputKind)
        # the lowercase spelling works too, and means the same thing
        assert StructureInput(kind=kind.lower(), value="x").kind is inp.kind

    # and the exact call from the spec
    inp = StructureInput(kind="NAME", value="phenylacetylene")
    assert inp.kind is StructureInputKind.NAME
    assert inp.value == "phenylacetylene"


def test_a_candidate_without_a_structure_is_a_name_hit_not_a_molecule():
    """A synonym record is not a competing molecule.

    PubChem returns synonym rows carrying a name and no structure. Counting
    those as candidates for the *molecule* would manufacture ambiguity where
    the source gave a word list.
    """
    name_only = candidate(smiles="", inchikey="", source_id="2244",
                          matched_by=MatchedBy.SYNONYM, matched_synonym="aspirin")
    assert name_only.describes_structure() is False
    assert candidate().describes_structure() is True


# ===========================================================================
# the rule the whole phase rests on
# ===========================================================================


def test_a_candidate_is_not_a_resolution():
    """`require_resolved` refuses before a structure has been established.

    This is the mechanical form of §1's "不要把 candidate 自动当 resolved". If a
    caller can get a structure out of an unresolved resolution, the enum is
    decoration and the LLM can walk a hallucinated SMILES into `retrosynthesize`.
    """
    for status in (StructureResolutionStatus.INPUT,
                   StructureResolutionStatus.CANDIDATE,
                   StructureResolutionStatus.AMBIGUOUS,
                   StructureResolutionStatus.INVALID,
                   StructureResolutionStatus.NOT_FOUND,
                   StructureResolutionStatus.UNAVAILABLE):
        resolution = ChemicalIdentityResolution(
            status=status,
            input=StructureInput(kind="NAME", value="phenylacetylene"),
            candidates=[candidate()],
        )
        assert resolution.is_resolved is False
        with pytest.raises(IdentityNotResolved) as excinfo:
            resolution.require_resolved()
        assert excinfo.value.status is status


def test_require_resolved_returns_the_selected_structure_and_only_that_one():
    """When there is a decision, it is the selected candidate -- not the list head.

    Passing `candidates` in a different order must not change the answer, which
    is the property `candidates[0]` would silently violate.
    """
    chosen = candidate(smiles="C", inchikey="X", source_id="1")
    other = candidate(smiles="CC", inchikey="Y", source_id="2")

    for order in ([chosen, other], [other, chosen]):
        resolution = ChemicalIdentityResolution(
            status=StructureResolutionStatus.RESOLVED,
            input=StructureInput(kind="SMILES", value="C"),
            candidates=order,
            selected=chosen,
        )
        assert resolution.require_resolved() is chosen


def test_statuses_are_never_collapsed_into_a_ranking():
    """`NOT_FOUND` and `UNAVAILABLE` answer different questions and stay different.

    "They have never heard of it" and "we could not ask" lead to opposite next
    steps, and the model must keep them apart even though both mean "no
    structure came back".
    """
    assert StructureResolutionStatus.NOT_FOUND is not StructureResolutionStatus.UNAVAILABLE
    assert StructureResolutionStatus.AMBIGUOUS is not StructureResolutionStatus.CANDIDATE


# ===========================================================================
# §11 -- identity basis, and the four facets
# ===========================================================================


def test_same_connectivity_and_same_stereo_are_different_answers():
    """The headline case of §11, expressed in facets.

    Two structures with one skeleton and two stereochemistries share a
    connectivity and do not share a molecule. Both facts must be representable
    at once, and the derived basis must not round the pair off to `exact`.
    """
    facets = IdentityFacets(
        same_connectivity=True,
        same_stereo=False,
        same_isotope=None,
        same_tautomer=None,
    )
    assert facets.basis() is IdentityBasis.SAME_CONNECTIVITY
    assert facets.basis() is not IdentityBasis.EXACT_STRUCTURE

    # and the reverse: stereo agrees, something else does not
    facets = IdentityFacets(
        same_connectivity=True,
        same_stereo=True,
        same_isotope=True,
        same_tautomer=False,
    )
    assert facets.basis() is IdentityBasis.SAME_STEREO


def test_the_four_facets_are_four_fields_not_one_verdict():
    """`same_isotope` and `same_tautomer` must be distinguishable from each other.

    §11 asks for `same_connectivity`, `same_stereo`, `same_isotope` and
    `same_tautomer` to be kept apart. A single "same: bool" cannot express
    "same skeleton, different tautomer", which is a real and common case.
    """
    only_tautomer = IdentityFacets(
        same_connectivity=True, same_stereo=True,
        same_isotope=True, same_tautomer=False,
    )
    only_isotope = IdentityFacets(
        same_connectivity=True, same_stereo=True,
        same_isotope=False, same_tautomer=True,
    )
    assert only_tautomer.same_tautomer is False
    assert only_tautomer.same_isotope is True
    assert only_isotope.same_isotope is False
    assert only_isotope.same_tautomer is True
    # the two differ in their recorded facts, not merely in a label
    assert only_tautomer != only_isotope


def test_no_comparison_recorded_reads_as_unknown_and_not_as_agreement():
    """`None` is "not asked", and it is not quietly `True` or `False`."""
    untouched = IdentityFacets()
    assert untouched.same_connectivity is None
    assert untouched.same_stereo is None
    assert untouched.same_isotope is None
    assert untouched.same_tautomer is None
    assert untouched.basis() is IdentityBasis.UNKNOWN


def test_a_different_skeleton_is_unknown_not_same_anything():
    """Different connectivity is not "same molecule with differences" -- it is two molecules.

    Reporting `SAME_CONNECTIVITY` for a pair with different skeletons would be
    the most dangerous possible error here, because it reads as agreement.
    """
    facets = IdentityFacets(
        same_connectivity=False, same_stereo=True,
        same_isotope=True, same_tautomer=True,
    )
    assert facets.basis() is IdentityBasis.UNKNOWN


def test_provider_identity_is_its_own_basis_and_says_what_was_not_done():
    """A database asserting identity is not a comparison we performed.

    `PROVIDER_IDENTITY` must stay distinct from `EXACT_STRUCTURE`, because the
    first is a citation and the second is a measurement. Conflating them is how
    "PubChem has this record" becomes "we verified this structure".
    """
    assert IdentityBasis.PROVIDER_IDENTITY is not IdentityBasis.EXACT_STRUCTURE
    assert IdentityBasis.UNKNOWN is not IdentityBasis.EXACT_STRUCTURE
    assert IdentityBasis.SAME_CONNECTIVITY is not IdentityBasis.SAME_STEREO


# ===========================================================================
# §15 / §16 -- the image path's vocabulary
# ===========================================================================


def test_recognition_stages_exist_and_are_not_a_resolution_status():
    """`IMAGE_RECOGNIZED_CANDIDATE` is a stage, not an outcome.

    §15 requires the first layer of an image reading to be
    `IMAGE_RECOGNIZED_CANDIDATE`. It must not be reachable as a
    `StructureResolutionStatus`, or a recogniser's guess would sit in the same
    vocabulary as `RESOLVED`.
    """
    stages = {stage.value for stage in RecognitionStage}
    assert {
        "image_recognized_candidate", "structure_candidate", "unparsed",
    } <= stages
    assert "image_recognized_candidate" not in {
        status.value for status in StructureResolutionStatus
    }


def test_image_provenance_records_every_field_the_spec_names_without_copying_the_image():
    """§16's field list, and the deliberate absence of image bytes."""
    fields = ImageProvenance.model_fields
    for name in ("image_sha256", "source", "retrieved_at", "recognizer",
                 "recognizer_version", "model_version", "raw_smiles",
                 "normalized_smiles"):
        assert name in fields, f"image provenance must record {name}"
    # a reference, not a copy
    assert "file_reference" in fields
    assert "image_bytes" not in fields and "image_data" not in fields


def test_a_resolution_carries_the_input_it_was_asked_about():
    """The question is recorded alongside the answer.

    Without it a later reader cannot tell which of several similar requests a
    given resolution belongs to, and an audit becomes guesswork.
    """
    inp = StructureInput(kind="IMAGE", value="structure.png",
                         image=ImageProvenance(image_sha256="ab" * 32))
    resolution = ChemicalIdentityResolution(status=StructureResolutionStatus.CANDIDATE, input=inp)
    assert resolution.input.image.image_sha256 == "ab" * 32
    assert resolution.input.kind is StructureInputKind.IMAGE


# ===========================================================================
# ambiguity, expressed in the model
# ===========================================================================


def test_several_records_for_one_structure_are_not_an_ambiguity():
    """PubChem returns one compound under many synonyms. That is one answer.

    `structures_distinguished` exists so that a resolution carrying five
    candidate *records* for one structure does not demand the user choose
    between spellings.
    """
    same = candidate(source_id="2244", synonyms=["aspirin"])
    same_again = candidate(source_id="2244-syn", matched_by=MatchedBy.SYNONYM,
                           matched_synonym="aspirin")
    resolution = ChemicalIdentityResolution(
        status=StructureResolutionStatus.AMBIGUOUS,
        input=StructureInput(kind="NAME", value="aspirin"),
        candidates=[same, same_again],
    )
    assert resolution.structures_distinguished() is False

    other = candidate(smiles="CCO", inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N", source_id="702")
    resolution.candidates = [same, same_again, other]
    assert resolution.structures_distinguished() is True

"""Chemical mention resolution: the boundary between text and entity.

Phase 3.8.2 (§18/§44). What is under test is the boundary itself:

* a mention that a trusted source establishes becomes an entity;
* a synonym of an established identity is the *same* entity;
* `compound 7` is paper-local and never a permanent name;
* several surviving candidates stay AMBIGUOUS, none chosen;
* nothing uncertain ever becomes a name-only entity;
* every outcome carries its chain of reasons, and none of them is
  "a model said so" -- there is no model here at all.

Precision over recall (§45): `UNRESOLVED` is a correct answer.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from crea.core.ids import content_hash
from crea.core.models.entity import ChemicalMention, MentionResolutionStatus
from crea.core.models.identity import StructureInput
from crea.core.models.mention_resolution import (
    MENTION_RESOLUTION_VERSION,
    ContextReference,
    MentionResolutionBasis,
)
from crea.core.models.evidence import Evidence, EvidenceStrength, EvidenceType, Locator
from crea.services.entity_registry import ChemicalEntityRegistry
from crea.services.identity import IdentityResolutionService
from crea.services.mention_resolution import (
    ANALYTICS_EXCLUSIONS,
    ChemicalMentionResolver,
    ParentheticalContext,
    StaticMentionContext,
    extract_chemical_mentions,
    is_numbered_reference,
)
from tests.fakes.compound import (
    ASCORBATE,
    ASCORBIC_ACID,
    PHENYLACETYLENE,
    SODIUM_ASCORBATE,
    FakeCompoundProvider,
)

PHENYL_INCHI = "InChI=1S/C8H6/c1-2-8-6-4-3-5-7-8/h1,3-7H"


def _resolver(provider=None, *, context_sources=(), held_evidence=None):
    registry = ChemicalEntityRegistry()
    identity = IdentityResolutionService(compound_provider=provider)
    return ChemicalMentionResolver(
        registry, identity=identity, context_sources=context_sources,
        held_evidence=held_evidence), registry


def _mention(text, **kwargs):
    return ChemicalMention(mention_id=content_hash(text, 12), raw_text=text,
                           normalized_text=text.strip().lower(), **kwargs)


# ---------------------------------------------------------------------------
# A / B -- names and synonyms
# ---------------------------------------------------------------------------


def test_A_a_plain_name_becomes_the_entity_the_provider_established():
    resolver, registry = _resolver(FakeCompoundProvider(
        by_name={"phenylacetylene": [PHENYLACETYLENE]}))

    result = resolver.resolve(_mention("phenylacetylene"))

    assert result.status is MentionResolutionStatus.RESOLVED
    assert result.entity_id == "CHEM-00000001"
    assert result.basis is MentionResolutionBasis.PROVIDER_NAME_MATCH
    assert result.evidence.identity_key.startswith("crea-identity-key-v1:InChI=1S/C8H6/")
    assert any("provider name match" in n for n in result.evidence.notes)
    assert result.mention.entity_id == "CHEM-00000001"


def test_B_a_synonym_of_an_established_identity_is_the_same_entity():
    registry = ChemicalEntityRegistry()
    quiet = PHENYLACETYLENE.model_copy(update={"synonyms": [], "input_name": None})
    resolver = ChemicalMentionResolver(
        registry,
        identity=IdentityResolutionService(compound_provider=FakeCompoundProvider(
            by_name={
                "phenylacetylene": [quiet.model_copy(update={"input_name": "phenylacetylene"})],
                "ethynylbenzene": [quiet.model_copy(update={"input_name": "ethynylbenzene"})],
            })))

    first = resolver.resolve(_mention("phenylacetylene"))
    second = resolver.resolve(_mention("ethynylbenzene"))

    assert first.entity_id == second.entity_id == "CHEM-00000001"
    assert len(registry) == 1
    names = registry.get("CHEM-00000001").names
    assert "phenylacetylene" in names and "ethynylbenzene" in names


def test_a_registered_name_resolves_without_consulting_the_provider():
    """§9: the cheap path first. The second occurrence must not cost a
    provider call."""
    provider = FakeCompoundProvider(by_name={"phenylacetylene": [PHENYLACETYLENE]})
    resolver, registry = _resolver(provider)

    resolver.resolve(_mention("phenylacetylene"))
    calls_after_first = len(provider.queries)
    second = resolver.resolve(_mention("Phenylacetylene"))

    assert second.entity_id == "CHEM-00000001"
    assert second.basis is MentionResolutionBasis.REGISTRY_NAME_MATCH
    assert len(provider.queries) == calls_after_first


# ---------------------------------------------------------------------------
# C -- paper-local numbered references
# ---------------------------------------------------------------------------


def test_C_compound_7_never_becomes_a_permanent_name():
    """"compound 7" resolved from context stays a mention, not a name."""
    context = StaticMentionContext({
        ("paper-b", "compound 7"): ContextReference(
            name="phenylacetylene", context_quote="Compound 7 (phenylacetylene)",
            source="paper", source_id="paper-b"),
    })
    resolver, registry = _resolver(
        FakeCompoundProvider(by_name={"phenylacetylene": [PHENYLACETYLENE]}),
        context_sources=(context,))

    result = resolver.resolve(_mention("compound 7", source_id="paper-b"))

    assert result.entity_id == "CHEM-00000001"
    assert result.basis is MentionResolutionBasis.CONTEXTUAL_REFERENCE
    assert result.evidence.context_quote == "Compound 7 (phenylacetylene)"
    # the paper-local label is not a permanent alias of the entity
    assert "compound 7" not in registry.get("CHEM-00000001").names


def test_compound_7_without_context_stays_unresolved():
    resolver, registry = _resolver(
        FakeCompoundProvider(by_name={"phenylacetylene": [PHENYLACETYLENE]}))

    result = resolver.resolve(_mention("compound 7", source_id="paper-a"))

    assert result.status is MentionResolutionStatus.UNRESOLVED
    assert result.entity_id is None
    assert result.basis is MentionResolutionBasis.UNRESOLVED
    assert len(registry) == 0
    assert "context" in " ".join(result.evidence.notes)


def test_compound_7_is_paper_local_and_does_not_leak_between_papers():
    """Paper A's unresolved `compound 7` must not borrow Paper B's
    definition (§17)."""
    context = StaticMentionContext({
        ("paper-b", "compound 7"): ContextReference(
            name="phenylacetylene", context_quote="Compound 7 (phenylacetylene)",
            source="paper", source_id="paper-b"),
    })
    resolver, _registry = _resolver(
        FakeCompoundProvider(by_name={"phenylacetylene": [PHENYLACETYLENE]}),
        context_sources=(context,))

    in_a = resolver.resolve(_mention("compound 7", source_id="paper-a"))
    in_b = resolver.resolve(_mention("compound 7", source_id="paper-b"))

    assert in_a.status is MentionResolutionStatus.UNRESOLVED
    assert in_b.entity_id == "CHEM-00000001"
    assert in_a.entity_id is None


def test_a_parenthetical_context_names_the_reference():
    resolver, _registry = _resolver(
        FakeCompoundProvider(by_name={"phenylacetylene": [PHENYLACETYLENE]}),
        context_sources=(ParentheticalContext(),))
    mention = _mention("Compound 7", source_id="paper-b",
                       context_after=" (phenylacetylene) was obtained as an oil.")

    result = resolver.resolve(mention)

    assert result.entity_id == "CHEM-00000001"
    assert result.basis is MentionResolutionBasis.CONTEXTUAL_REFERENCE
    assert "phenylacetylene" in result.evidence.context_quote
    assert result.evidence.source_id == "paper-b"


# ---------------------------------------------------------------------------
# D -- ambiguity stays ambiguous
# ---------------------------------------------------------------------------


def test_D_several_candidates_are_ambiguous_and_none_is_chosen():
    resolver, registry = _resolver(FakeCompoundProvider(by_name={
        "vitamin C": [ASCORBIC_ACID, ASCORBATE, SODIUM_ASCORBATE]}))

    result = resolver.resolve(_mention("vitamin C"))

    assert result.status is MentionResolutionStatus.AMBIGUOUS
    assert result.entity_id is None
    assert result.mention.entity_id is None
    assert len(result.candidates) == 3
    # every candidate is preserved as information, none as a decision
    assert all(c.entity_id is None for c in result.candidates)
    assert all(c.inchi.startswith("InChI=") for c in result.candidates)
    assert len(registry) == 0
    # the refusal carries its reason, verbatim from the identity layer
    assert result.evidence.notes, "an ambiguous refusal must record why"


def test_a_name_matching_two_registered_entities_is_ambiguous():
    registry = ChemicalEntityRegistry()
    service = IdentityResolutionService(compound_provider=FakeCompoundProvider(
        by_name={"phenylacetylene": [PHENYLACETYLENE]}))
    resolver = ChemicalMentionResolver(registry, identity=service)
    resolver.resolve(_mention("phenylacetylene"))
    # a second entity that also carries the name (as a registry label)
    other = registry.register_resolution(service.resolve(
        StructureInput.of("SMILES", "F/C=C/F")))
    registry.add_name(other.entity_id, "phenylacetylene")

    result = resolver.resolve(_mention("phenylacetylene"))

    assert result.status is MentionResolutionStatus.AMBIGUOUS
    assert result.entity_id is None
    assert {c.entity_id for c in result.candidates} == {"CHEM-00000001", other.entity_id}


# ---------------------------------------------------------------------------
# E -- nothing uncertain becomes an entity
# ---------------------------------------------------------------------------


def test_E_a_name_with_no_structure_source_creates_nothing():
    resolver, registry = _resolver(FakeCompoundProvider())  # holds nothing

    result = resolver.resolve(_mention("mysteryene"))

    assert result.status is MentionResolutionStatus.NOT_FOUND
    assert result.entity_id is None
    assert len(registry) == 0


def test_an_unreachable_provider_is_unresolved_not_a_guess():
    from tests.fakes.compound import FailingCompoundProvider

    resolver, registry = _resolver(FailingCompoundProvider())
    result = resolver.resolve(_mention("phenylacetylene"))

    assert result.status is MentionResolutionStatus.UNRESOLVED
    assert result.entity_id is None
    assert len(registry) == 0


def test_no_identity_service_at_all_is_unresolved():
    resolver = ChemicalMentionResolver(ChemicalEntityRegistry(), identity=None)
    result = resolver.resolve(_mention("phenylacetylene"))

    assert result.status is MentionResolutionStatus.UNRESOLVED
    assert "no structure source" in " ".join(result.evidence.notes)


# ---------------------------------------------------------------------------
# explicit structure outranks names (§23)
# ---------------------------------------------------------------------------


def test_an_explicit_smiles_on_the_mention_wins():
    resolver, registry = _resolver(
        FakeCompoundProvider(by_name={"phenylacetylene": [PHENYLACETYLENE]}))

    result = resolver.resolve(_mention(
        "compound 7", explicit_smiles="C#Cc1ccccc1", source_id="paper-a"))

    assert result.entity_id == "CHEM-00000001"
    assert result.basis is MentionResolutionBasis.EXPLICIT_STRUCTURE
    assert any("explicit structure" in n for n in result.evidence.notes)
    # one entity, not two: the explicit structure is the same identity
    assert len(registry) == 1


def test_an_explicit_inchi_that_does_not_parse_is_unresolved():
    resolver, registry = _resolver()
    result = resolver.resolve(_mention("compound 7", explicit_inchi="not-an-inchi"))

    assert result.status is MentionResolutionStatus.UNRESOLVED
    assert result.entity_id is None
    assert len(registry) == 0


# ---------------------------------------------------------------------------
# external identifiers (§10/§11)
# ---------------------------------------------------------------------------


def test_a_pubchem_cid_mention_resolves_through_identity_policy():
    resolver, registry = _resolver(FakeCompoundProvider(
        by_cid={"10821": [PHENYLACETYLENE]}))

    result = resolver.resolve(_mention("PubChem CID 10821"))

    assert result.entity_id == "CHEM-00000001"
    assert result.basis is MentionResolutionBasis.PROVIDER_EXTERNAL_ID
    assert registry.find_by_external_id("pubchem", "10821") is not None
    assert result.entity_id != "10821"


def test_a_registered_cid_mention_needs_no_provider():
    provider = FakeCompoundProvider(by_cid={"10821": [PHENYLACETYLENE]})
    resolver, registry = _resolver(provider)
    resolver.resolve(_mention("PubChem CID 10821"))
    calls = len(provider.queries)

    again = resolver.resolve(_mention("PubChem CID 10821"))

    assert again.basis is MentionResolutionBasis.REGISTRY_EXTERNAL_ID
    assert len(provider.queries) == calls


# ---------------------------------------------------------------------------
# provenance and evidence (§15/F)
# ---------------------------------------------------------------------------


def test_F_user_pdf_provenance_survives_resolution():
    locator = Locator(locator_type="document_region", document_id="doc-1",
                      page_start=5, section="Experimental")
    held = Evidence(
        evidence_id="ev-1", evidence_type=EvidenceType.EXPERIMENTAL,
        strength=EvidenceStrength.USER_PROVIDED_EVIDENCE,
        source="user_provided", source_id="doc-1",
        retrieved_at="2026-10-02T00:00:00+00:00",
        raw_quote="phenylacetylene (1) was obtained...", locator=locator)
    resolver, _registry = _resolver(
        FakeCompoundProvider(by_name={"phenylacetylene": [PHENYLACETYLENE]}),
        held_evidence=[held])
    mention = _mention("phenylacetylene", source="user_provided",
                       source_id="doc-1", locator=locator,
                       evidence_refs=["ev-1", "ev-gone"])

    result = resolver.resolve(mention)
    evidence = result.evidence

    assert result.entity_id == "CHEM-00000001"
    assert evidence.locator.document_id == "doc-1"
    assert evidence.locator.page_start == 5
    assert evidence.source == "user_provided"
    assert evidence.source_id == "doc-1"
    assert evidence.evidence_refs == ["ev-1"]
    assert evidence.unresolved_evidence_refs == ["ev-gone"]  # listed, not dropped
    assert evidence.provenance is not None
    assert evidence.provenance.source == "user_provided"
    assert evidence.provenance.tool_version == MENTION_RESOLUTION_VERSION


# ---------------------------------------------------------------------------
# extraction (§20-22)
# ---------------------------------------------------------------------------


def test_extraction_finds_numbered_references_case_insensitively():
    text = ("Compound 7 was obtained as an oil. Intermediate 3b and product 4 "
            "were purified; substrate 8 and compound 12a were used as such.")
    mentions = extract_chemical_mentions(text)

    texts = sorted(m.normalized_text for m in mentions)
    assert texts == ["compound 12a", "compound 7", "intermediate 3b",
                     "product 4", "substrate 8"]


def test_extraction_ignores_bare_numbers_and_units():
    text = "The mixture (7 mL) was stirred for 7 h at 80 °C in 82% yield."
    mentions = extract_chemical_mentions(text)

    assert mentions == []
    # and no known-name match turns a quantity into a chemical
    assert all("mL" not in m.raw_text and "%" not in m.raw_text
               for m in mentions)


def test_extraction_matches_known_names_but_refuses_analytics_terms():
    text = "The NMR and HPLC of compound 7 in THF were recorded; GC showed clean."
    mentions = extract_chemical_mentions(
        text, known_names=["THF", "NMR", "DMF"])
    texts = sorted(m.normalized_text for m in mentions)

    assert texts == ["compound 7", "thf"]
    assert "nmr" not in texts and "hplc" not in texts and "gc" not in texts
    assert ANALYTICS_EXCLUSIONS == frozenset(
        {"NMR", "HPLC", "GC", "TLC", "MS", "IR", "UV"})


def test_extraction_records_scope_and_context():
    text = "A solution of compound 7 in THF was stirred overnight."
    mentions = extract_chemical_mentions(
        text, known_names=["THF"], source="paper", source_id="paper-b")
    numbered = next(m for m in mentions if m.normalized_text == "compound 7")

    assert numbered.source == "paper"
    assert numbered.source_id == "paper-b"
    assert "in THF" in numbered.context_after
    assert numbered.context_before == "A solution of "
    assert is_numbered_reference("compound 7a")
    assert not is_numbered_reference("7 mL")


def test_extraction_context_is_bounded():
    from crea.core.models.entity import MAX_CONTEXT_CHARS

    text = "x" * 5000 + " compound 7 " + "y" * 5000
    mention = extract_chemical_mentions(text)[0]

    assert len(mention.context_before) <= MAX_CONTEXT_CHARS
    assert len(mention.context_after) <= MAX_CONTEXT_CHARS


# ---------------------------------------------------------------------------
# the model boundary
# ---------------------------------------------------------------------------


def test_a_resolution_refuses_an_entity_id_without_resolved_status():
    from pydantic import ValidationError

    from crea.core.models.mention_resolution import ChemicalMentionResolution

    mention = _mention("compound 7")
    with pytest.raises(ValidationError):
        ChemicalMentionResolution(
            mention=mention, status=MentionResolutionStatus.AMBIGUOUS,
            entity_id="CHEM-00000001")

    ok = ChemicalMentionResolution(
        mention=mention.model_copy(update={
            "entity_id": "CHEM-00000001",
            "resolution_status": MentionResolutionStatus.RESOLVED}),
        status=MentionResolutionStatus.RESOLVED, entity_id="CHEM-00000001")
    assert ok.entity_id == "CHEM-00000001"


# ---------------------------------------------------------------------------
# G / §43 -- no model, no shortcuts
# ---------------------------------------------------------------------------


def test_G_the_resolution_layer_contains_no_model_and_no_shortcuts():
    """An LLM cannot manufacture an identity here: the module does not import
    one, and the code contains none of the shortcuts §43 forbids. (The
    docstrings may *name* those shortcuts in order to refuse them, so the
    checks read the syntax tree, not the prose.)"""
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "crea"
    for path in (root / "services" / "mention_resolution.py",
                 root / "core" / "models" / "mention_resolution.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
            # candidates[0] / candidates[-1] / candidates[1:]: a positional
            # choice among candidates is exactly the ambiguity cheat
            elif isinstance(node, ast.Subscript):
                value = getattr(node.value, "id", None) or getattr(node.value, "attr", None)
                if value in ("candidates", "candidate_list", "candidate"):
                    index = node.slice
                    if isinstance(index, ast.Constant) and isinstance(index.value, (int, type(None))):
                        offenders.append(f"{path.name}:{node.lineno}: {value}[{index.value!r}]")
                    elif isinstance(index, ast.Slice):
                        offenders.append(f"{path.name}:{node.lineno}: {value}[...]")
            # hash( of anything except the sanctioned content_hash: an id
            # derived from mention text is a §43 non-starter
            elif isinstance(node, ast.Call):
                func = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if func == "hash":
                    offenders.append(f"{path.name}:{node.lineno}: hash(...)")

        assert not (imported & {"crea.llm", "openai", "anthropic", "llm"}), (
            f"{path.name} reaches a language model")
        assert not offenders, f"forbidden shortcuts in {path.name}: {offenders}"

    # and the enum refuses the middle ground a model would need
    assert MentionResolutionBasis.UNRESOLVED.value == "unresolved"

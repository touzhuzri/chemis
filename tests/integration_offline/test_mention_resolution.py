"""Mention resolution through the real identity layer and real documents.

Phase 3.8.2. The chain under test is the one the knowledge graph will
depend on:

    Paper text -> ChemicalMention -> resolution -> ChemicalEntity -> CHEM-id

with every link deterministic: extraction is pattern matching, resolution
is identity policy, the entity is the registry's. A user PDF's evidence
travels with its mention, so the chain can be read backwards later:

    Document -> Evidence -> Mention -> Entity
"""

from __future__ import annotations

from crea.core.models.entity import ChemicalMention, MentionResolutionStatus
from crea.core.models.identity import StructureInput
from crea.core.models.mention_resolution import MentionResolutionBasis
from crea.services.entity_registry import ChemicalEntityRegistry
from crea.services.identity import IdentityResolutionService
from crea.services.mention_resolution import (
    ChemicalMentionResolver,
    extract_chemical_mentions,
)
from tests.fakes.compound import PHENYLACETYLENE, FakeCompoundProvider

PAPER_TEXT = (
    "Phenylacetylene was purchased and used as such. Ethynylbenzene "
    "(the same starting material) was distilled before use. "
    "Compound 7 was obtained according to the procedure described above. "
    "The mixture was stirred in THF (7 mL) for 7 h; the NMR was clean."
)


def _mention(text, **kwargs):
    return ChemicalMention(mention_id=text.replace(" ", "-")[:24],
                           raw_text=text, normalized_text=text.strip().lower(),
                           **kwargs)


def test_a_paper_passage_walks_from_text_to_entities():
    """Names resolve through the identity layer; the paper-local number does
    not; the quantities never become mentions."""
    registry = ChemicalEntityRegistry()
    provider = FakeCompoundProvider(by_name={
        "phenylacetylene": [PHENYLACETYLENE],
        "ethynylbenzene": [PHENYLACETYLENE.model_copy(
            update={"input_name": "ethynylbenzene", "synonyms": []})],
    })
    resolver = ChemicalMentionResolver(
        registry, identity=IdentityResolutionService(compound_provider=provider))

    mentions = extract_chemical_mentions(
        PAPER_TEXT, known_names=["phenylacetylene", "ethynylbenzene"],
        source="paper", source_id="paper-a")
    results = [resolver.resolve(m) for m in mentions]
    by_text = {r.mention.normalized_text: r for r in results}

    # the two names are one identity, one entity
    assert by_text["phenylacetylene"].entity_id == "CHEM-00000001"
    assert by_text["ethynylbenzene"].entity_id == "CHEM-00000001"
    assert len(registry) == 1
    # the paper-local number is unresolved without its definition
    compound_7 = by_text["compound 7"]
    assert compound_7.status is MentionResolutionStatus.UNRESOLVED
    assert compound_7.entity_id is None
    # quantities and instruments never became mentions at all
    assert "7 ml" not in by_text and "nmr" not in by_text and "thf" not in by_text


def test_a_second_pass_resolves_from_the_registry_not_the_provider():
    provider = FakeCompoundProvider(by_name={
        "phenylacetylene": [PHENYLACETYLENE],
        "ethynylbenzene": [PHENYLACETYLENE.model_copy(
            update={"input_name": "ethynylbenzene", "synonyms": []})],
    })
    registry = ChemicalEntityRegistry()
    resolver = ChemicalMentionResolver(
        registry, identity=IdentityResolutionService(compound_provider=provider))

    resolver.resolve(_mention("phenylacetylene"))
    calls_after_first = len(provider.queries)
    again = resolver.resolve(_mention("ethynylbenzene", source_id="paper-c"))

    assert again.entity_id == "CHEM-00000001"
    assert again.basis is MentionResolutionBasis.REGISTRY_NAME_MATCH
    assert len(provider.queries) == calls_after_first


def test_an_explicit_smiles_converges_with_the_provider_name():
    """The same identity by two routes is one entity (§24: one key, no
    second identity hash)."""
    registry = ChemicalEntityRegistry()
    service = IdentityResolutionService(compound_provider=FakeCompoundProvider(
        by_name={"phenylacetylene": [PHENYLACETYLENE]}))
    resolver = ChemicalMentionResolver(registry, identity=service)

    explicit = resolver.resolve(_mention(
        "compound 7", explicit_smiles="C#Cc1ccccc1", source_id="paper-b"))
    named = resolver.resolve(_mention("phenylacetylene"))

    assert explicit.basis is MentionResolutionBasis.EXPLICIT_STRUCTURE
    assert named.entity_id == explicit.entity_id == "CHEM-00000001"
    assert len(registry) == 1
    assert registry.get("CHEM-00000001").identity_key.startswith(
        "crea-identity-key-v1:InChI=1S/C8H6/")


def test_the_document_chain_keeps_provenance_from_pdf_to_entity(tmp_path):
    """Document -> Evidence -> Mention -> Entity, each link readable."""
    from crea.services.document_evidence import search_document
    from crea.agent.workspace import DocumentLibrary
    from crea.services.documents import LocalDocumentService
    from crea.sources.pdf import PypdfDocumentParser
    from tests.fakes import pdfs

    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    path = tmp_path / "procedure.pdf"
    path.write_bytes(pdfs.make_pdf(pdfs.EXPERIMENTAL))
    parsed = library.ingest(path)
    document_id = parsed.document.document_id

    evidence = search_document(parsed, "CuI")[0].evidence
    assert evidence.source == "user_provided"

    registry = ChemicalEntityRegistry()
    resolver = ChemicalMentionResolver(
        registry,
        identity=IdentityResolutionService(compound_provider=FakeCompoundProvider(
            by_name={"Phenylacetylene": [PHENYLACETYLENE]})),
        held_evidence=[evidence])
    mention = _mention("Phenylacetylene", source="user_provided",
                       source_id=document_id, locator=evidence.locator,
                       evidence_refs=[evidence.evidence_id])

    result = resolver.resolve(mention)

    assert result.entity_id == "CHEM-00000001"
    # the chain is readable in both directions
    assert result.evidence.evidence_refs == [evidence.evidence_id]
    assert result.evidence.locator.document_id == document_id
    assert result.evidence.source == "user_provided"
    assert result.evidence.provenance.source == "user_provided"
    entity = registry.get(result.entity_id)
    assert entity.identity_key.startswith("crea-identity-key-v1:")


def test_extractions_and_resolutions_never_invent_a_structure():
    """Nothing in the chain may mint an entity from a word alone: with no
    provider and no context, the whole passage stays unregistered."""
    registry = ChemicalEntityRegistry()
    resolver = ChemicalMentionResolver(
        registry, identity=IdentityResolutionService(
            compound_provider=FakeCompoundProvider()))

    mentions = extract_chemical_mentions(
        PAPER_TEXT, known_names=["phenylacetylene", "ethynylbenzene"])
    results = [resolver.resolve(m) for m in mentions]

    assert all(r.entity_id is None for r in results)
    assert all(r.status in (MentionResolutionStatus.NOT_FOUND,
                            MentionResolutionStatus.UNRESOLVED)
               for r in results)
    assert len(registry) == 0

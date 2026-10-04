"""ReactionBuilder and ReactionRegistry: record -> reaction, kept honest.

Phase 3.9. What is under test is the join: a policy-validated record and
the mention layer's answers become a Reaction whose every component knows
its mention and its entity, whose status is computed from evidence and
gaps, and whose registry merges only what is provably the same report of
the same chemistry. No model is consulted anywhere in this file.

Records come in two shapes: a full procedure (catalyst, solvent, an
unassigned substance) for the role rules, and a minimal one (reactant +
product, both resolvable) for status and deduplication -- because a key
carrying any unresolved name is tainted and the registry must keep both
reactions (§21), which would obscure the merge rule under test.
"""

from __future__ import annotations

from crea.core.models.entity import ChemicalMention, MentionResolutionStatus
from crea.core.models.evidence import Evidence, EvidenceStrength, EvidenceType, Locator
from crea.core.models.reaction import (
    Component,
    ComponentRole,
    FieldStatus,
    Quantity,
    ReactionConditions,
    ReactionRecord,
)
from crea.core.models.reaction_knowledge import ReactionStatus
from crea.services.entity_registry import ChemicalEntityRegistry
from crea.services.identity import IdentityResolutionService
from crea.services.mention_resolution import ChemicalMentionResolver
from crea.services.reaction_builder import ReactionBuilder, ReactionRegistry
from tests.fakes.compound import PHENYLACETYLENE, FakeCompoundProvider

PHENYL = "C#Cc1ccccc1"
DIYNE = "C(C#Cc1ccccc1)#Cc1ccccc1"


def _evidence(eid="ev-1", *, etype=EvidenceType.EXPERIMENTAL,
              strength=EvidenceStrength.USER_PROVIDED_EVIDENCE,
              source="user_provided", source_id="doc-1"):
    return Evidence(
        evidence_id=eid, evidence_type=etype, strength=strength,
        source=source, source_id=source_id,
        retrieved_at="2026-10-02T00:00:00+00:00",
        raw_quote="phenylacetylene was coupled to the product",
        locator=Locator(locator_type="document_region", document_id=source_id,
                        page_start=3, section="Experimental"))


def _record(*, minimal=False, product_name=None, refs=None, document_id="doc-1"):
    if minimal:
        conditions = ReactionConditions(
            reactants=[Component(name="phenylacetylene",
                                       evidence_refs=list(refs or []))])
    else:
        conditions = ReactionConditions(
            reactants=[Component(name="phenylacetylene",
                                       evidence_refs=list(refs or []))],
            catalysts=[Component(name="CuI", role=ComponentRole.CATALYST,
                                       evidence_refs=list(refs or []))],
            solvents=[Component(name="THF", role=ComponentRole.SOLVENT,
                                      evidence_refs=list(refs or []))],
            unassigned=[Component(name="DBU")],
            temperature=Quantity(value=80, raw_value="80", unit="C",
                                 status=FieldStatus.EXPLICIT,
                                 evidence_refs=list(refs or [])),
            pressure=Quantity(status=FieldStatus.NOT_FOUND))
    return ReactionRecord(
        reaction_id="rxn-1", document_id=document_id, conditions=conditions,
        evidence_refs=list(refs or []))


def _stack(held=None):
    entities = ChemicalEntityRegistry()
    resolver = ChemicalMentionResolver(
        entities,
        identity=IdentityResolutionService(compound_provider=FakeCompoundProvider(
            by_name={"phenylacetylene": [PHENYLACETYLENE]})),
        held_evidence=held)
    builder = ReactionBuilder(resolver, entity_registry=entities,
                              held_evidence=held)
    return builder, entities, resolver


def _product(text="the product", **kwargs):
    return ChemicalMention(mention_id="p-" + text.replace(" ", "-"),
                           raw_text=text, normalized_text=text.lower(),
                           **kwargs)


def _build(builder, *, refs=None, product="the product", paper_id="paper-a",
           minimal=True, document_id="doc-1", product_kwargs=None):
    return builder.build(
        _record(minimal=minimal, refs=refs, document_id=document_id),
        paper_id=paper_id,
        product_mentions=[_product(product, **(product_kwargs or {}))])


# ---------------------------------------------------------------------------
# roles and components
# ---------------------------------------------------------------------------


def test_roles_come_from_the_record_and_unknown_stays_unknown():
    builder, _entities, _r = _stack()
    reaction = builder.build(
        _record(refs=["ev-1"]),
        paper_id="paper-a",
        product_mentions=[_product("the product", explicit_smiles=DIYNE)])

    roles = {c.role for c in reaction.components()}
    assert ComponentRole.REACTANT in roles
    assert ComponentRole.CATALYST in roles
    assert ComponentRole.SOLVENT in roles
    # DBU's job the source never said: UNKNOWN, never guessed (§7/§8)
    assert [c.name for c in reaction.unassigned] == ["DBU"]
    assert reaction.unassigned[0].role is ComponentRole.UNKNOWN
    assert [c.name for c in reaction.solvents] == ["THF"]


def test_every_component_carries_its_mention_and_its_entity():
    builder, entities, _r = _stack()
    reaction = _build(builder, product_kwargs={"explicit_smiles": DIYNE})

    for component in reaction.components():
        assert component.mention is not None, component.name
        assert component.mention.mention_id   # §62: which mention, answerable
    phenyl = next(c for c in reaction.reactants if c.name == "phenylacetylene")
    assert phenyl.entity_id == "CHEM-00000001"
    assert phenyl.identity_key.startswith("crea-identity-key-v1:")
    # the structure is the entity's established canonical form, not the
    # spelling the record used
    assert phenyl.canonical_smiles == entities.get(phenyl.entity_id).canonical_smiles
    assert phenyl.canonical_smiles != ""

    registry = ReactionRegistry()
    registry.register(reaction)
    assert registry.find_by_component_entity("CHEM-00000001")[
        0].reaction_id == reaction.reaction_id


# ---------------------------------------------------------------------------
# status is computed (§9/§10/§11)
# ---------------------------------------------------------------------------


def test_an_unresolved_product_makes_the_reaction_partial_not_discarded():
    builder, _e, _r = _stack()
    reaction = _build(builder, refs=["ev-1"], product="compound 8")

    assert reaction.status is ReactionStatus.PARTIAL
    product = reaction.products[0]
    assert product.entity_id is None
    assert product.mention.resolution_status is MentionResolutionStatus.UNRESOLVED
    assert any("compound 8" in note for note in reaction.notes)
    # the reaction survives with everything else intact (§9)
    assert reaction.reactants and reaction.evidence_refs == ["ev-1"]


def test_user_document_evidence_lifts_a_complete_reaction_to_experimental():
    held = [_evidence("ev-1")]
    builder, _e, _r = _stack(held=held)
    reaction = _build(builder, refs=["ev-1"],
                      product_kwargs={"explicit_smiles": DIYNE})

    assert reaction.status is ReactionStatus.EXPERIMENTALLY_SUPPORTED
    assert reaction.evidence_refs == ["ev-1"]
    assert reaction.locator is not None and reaction.locator.page_start == 3
    # the reaction's provenance names the record's origin; the *evidence*
    # itself is user-provided (held, and cited by ref)
    assert reaction.provenance.source == "document"
    assert held[0].source == "user_provided"


def test_metadata_evidence_never_makes_a_reaction_evidence_backed():
    meta = _evidence("ev-m", etype=EvidenceType.METADATA,
                     strength=EvidenceStrength.SECONDARY_METADATA_EVIDENCE,
                     source="crossref", source_id="W1")
    builder, _e, _r = _stack(held=[meta])
    reaction = _build(builder, refs=["ev-m"],
                      product_kwargs={"explicit_smiles": DIYNE})

    assert reaction.status is ReactionStatus.STRUCTURED
    assert reaction.status is not ReactionStatus.EVIDENCE_BACKED


def test_a_non_primary_quote_makes_a_reaction_evidence_backed_not_experimental():
    # a quote that is real evidence but not primary/user document evidence:
    # evidence-backed yes (§10), experimentally supported no (§11 -- that
    # needs Experimental/Methods/SI text or a defined primary evidence)
    held = [_evidence("ev-f", etype=EvidenceType.FULLTEXT,
                      strength=EvidenceStrength.SECONDARY_METADATA_EVIDENCE,
                      source="openalex", source_id="W1")]
    builder, _e, _r = _stack(held=held)
    reaction = _build(builder, refs=["ev-f"],
                      product_kwargs={"explicit_smiles": DIYNE})

    assert reaction.status is ReactionStatus.EVIDENCE_BACKED


def test_unresolved_evidence_refs_are_kept_but_make_the_reaction_partial():
    held = [_evidence("ev-1")]
    builder, _e, _r = _stack(held=held)
    reaction = _build(builder, refs=["ev-1", "ev-gone"],
                      product_kwargs={"explicit_smiles": DIYNE})

    assert reaction.evidence_refs == ["ev-1"]
    assert reaction.unresolved_evidence_refs == ["ev-gone"]
    assert any("do not resolve" in note for note in reaction.notes)
    # a layer that cannot be answered is a gap, not a quiet pass (§62)
    assert reaction.status is ReactionStatus.PARTIAL


def test_the_provenance_questions_are_answerable():
    held = [_evidence("ev-1")]
    builder, _e, _r = _stack(held=held)
    reaction = builder.build(
        _record(minimal=True, refs=["ev-1"]),
        paper_id="paper-a", doi="10.0000/synthetic.2026.0001",
        product_mentions=[_product("the product", explicit_smiles=DIYNE)])

    assert reaction.paper_id == "paper-a"
    assert reaction.doi == "10.0000/synthetic.2026.0001"
    assert reaction.document_id == "doc-1"
    assert reaction.evidence_refs == ["ev-1"]
    phenyl = next(c for c in reaction.reactants if c.name == "phenylacetylene")
    assert phenyl.mention.mention_id and phenyl.entity_id

    # a reaction with nowhere to point says so (§62)
    nowhere = builder.build(
        ReactionRecord(reaction_id="rxn-x", document_id="",
                       conditions=ReactionConditions(), evidence_refs=[]),
        product_mentions=[_product("compound 8")])
    assert any("no paper and no document" in note for note in nowhere.notes)
    assert nowhere.status is ReactionStatus.PARTIAL


# ---------------------------------------------------------------------------
# deduplication (§21)
# ---------------------------------------------------------------------------


def test_the_same_report_of_the_same_chemistry_merges():
    builder, _e, _r = _stack(held=[_evidence("ev-1")])
    registry = ReactionRegistry()

    kept, merged = registry.register(_build(
        builder, refs=["ev-1"], product_kwargs={"explicit_smiles": DIYNE}))
    kept2, merged2 = registry.register(_build(
        builder, refs=["ev-1"], product_kwargs={"explicit_smiles": DIYNE}))

    assert merged is False and merged2 is True
    assert kept2.reaction_id == kept.reaction_id
    assert len(registry) == 1


def test_the_same_chemistry_in_two_papers_stays_two_reactions():
    builder, _e, _r = _stack(held=[_evidence("ev-1")])
    registry = ReactionRegistry()
    registry.register(_build(builder, refs=["ev-1"], paper_id="paper-a",
                             product_kwargs={"explicit_smiles": DIYNE}))
    registry.register(_build(builder, refs=["ev-1"], paper_id="paper-b",
                             product_kwargs={"explicit_smiles": DIYNE}))

    assert len(registry) == 2
    assert len(registry.find_by_paper("paper-a")) == 1
    assert len(registry.find_by_paper("paper-b")) == 1


def test_a_name_based_reaction_is_never_merged():
    """"compound 7" in two papers may be two compounds (§21): keep both.

    The two reports carry their own record ids and paper scopes, so they
    are distinguishable; what the name-taint refuses is *merging* them on
    the strength of the words matching.
    """
    builder, _e, _r = _stack(held=[_evidence("ev-1")])
    registry = ReactionRegistry()
    first = _build(builder, refs=["ev-1"], product="compound 7",
                   paper_id="paper-a")
    second = builder.build(
        _record(minimal=True, refs=["ev-1"], document_id="doc-2"),
        paper_id="paper-b",
        product_mentions=[_product("compound 7")])
    first = first.model_copy(update={"reaction_id": "RXN-1"})
    second = second.model_copy(update={"reaction_id": "RXN-2"})

    registry.register(first)
    registry.register(second)

    assert len(registry) == 2

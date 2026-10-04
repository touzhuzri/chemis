"""Reaction knowledge models: key, status, and the boundaries they enforce.

Phase 3.9. The models answer three questions deterministically: what makes
two reactions the same *report* (the key, §22), how far a reaction's support
got (the status, §10), and what each component keeps of its provenance
(mention -> entity, §62). Nothing here consults a model or a similarity
threshold.
"""

from __future__ import annotations

import pytest

from crea.core.models.entity import ChemicalMention
from crea.core.models.reaction import ComponentRole
from crea.core.models.reaction_linking import LinkBasis, ReactionEvidenceLevel
from crea.core.models.reaction_knowledge import (
    REACTION_KEY_VERSION,
    Reaction,
    ReactionComponent,
    ReactionStatus,
    RouteStepReactionLink,
    key_is_identity_based,
    reaction_key,
)
from crea.core.models.route import ReactionMatchType
from crea.services.reaction_builder import evaluate_status, evidence_level_for

KEY_A = f"{REACTION_KEY_VERSION}:InChI=1S/C8H6/c1-2-8-6-4-3-5-7-8/h1,3-7H"


def _component(role=ComponentRole.REACTANT, name="phenylacetylene",
               entity_id="CHEM-00000001", key=KEY_A, smiles="C#Cc1ccccc1"):
    return ReactionComponent(role=role, name=name, entity_id=entity_id,
                             identity_key=key, canonical_smiles=smiles)


# ---------------------------------------------------------------------------
# reaction key (§22)
# ---------------------------------------------------------------------------


def test_the_reaction_key_is_deterministic_and_order_insensitive():
    """Reagent/solvent order never forks a reaction (§22)."""
    a = reaction_key([KEY_A, KEY_A], ["e:" + KEY_A], ["e:k1", "e:k2"])
    b = reaction_key([KEY_A, KEY_A], ["e:" + KEY_A], ["e:k2", "e:k1"])

    assert a == b
    assert a.startswith(f"{REACTION_KEY_VERSION}:")
    assert reaction_key([], []) != reaction_key([KEY_A], [])


def test_a_name_based_key_is_marked_and_never_merges():
    """hash(raw_text) is forbidden (§22/§43): an unresolved name enters the
    key *marked* as a name, and such a key never merges two reactions."""
    tainted = reaction_key(["name:compound 7"], ["e:" + KEY_A])
    clean = reaction_key(["e:" + KEY_A], ["e:" + KEY_A])

    assert "name:" in tainted
    assert not key_is_identity_based(tainted)
    assert key_is_identity_based(clean)
    assert tainted != clean


def test_the_key_version_is_pinned():
    assert REACTION_KEY_VERSION == "crea-reaction-key-v1"
    assert reaction_key([], []).startswith("crea-reaction-key-v1:")


# ---------------------------------------------------------------------------
# status rules (§10/§11)
# ---------------------------------------------------------------------------


def test_a_bare_proposal_is_a_candidate():
    reaction = Reaction(reaction_id="RXN-1")
    assert reaction.status is ReactionStatus.CANDIDATE
    assert evaluate_status(record_validated=False,
                           evidence_level=ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE,
                           gaps=[]) is ReactionStatus.CANDIDATE


def test_a_validated_record_with_nothing_behind_it_is_structured():
    assert evaluate_status(record_validated=True,
                           evidence_level=ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE,
                           gaps=[]) is ReactionStatus.STRUCTURED


def test_metadata_alone_never_makes_a_reaction_evidence_backed():
    """§11: a title mentioning a reaction is not evidence for it."""
    assert evaluate_status(record_validated=True,
                           evidence_level=ReactionEvidenceLevel.LITERATURE_DISCOVERY,
                           gaps=[]) is ReactionStatus.STRUCTURED


def test_quote_level_evidence_makes_a_reaction_evidence_backed():
    assert evaluate_status(record_validated=True,
                           evidence_level=ReactionEvidenceLevel.LITERATURE_REACTION_SUPPORT,
                           gaps=[]) is ReactionStatus.EVIDENCE_BACKED


@pytest.mark.parametrize("rung", [
    ReactionEvidenceLevel.EXPERIMENTAL_REACTION_SUPPORT,
    ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT,
])
def test_only_an_experimental_rung_makes_a_reaction_experimentally_supported(rung):
    assert evaluate_status(record_validated=True, evidence_level=rung,
                           gaps=[]) is ReactionStatus.EXPERIMENTALLY_SUPPORTED


def test_gaps_win_over_everything_and_keep_the_reaction():
    """§9/§62: an unresolvable part makes the reaction PARTIAL -- kept, not
    dropped, and never quietly complete."""
    for rung in (ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT,
                 ReactionEvidenceLevel.LITERATURE_REACTION_SUPPORT):
        assert evaluate_status(record_validated=True, evidence_level=rung,
                               gaps=["product resolved to no entity"]
                               ) is ReactionStatus.PARTIAL


def test_the_evidence_ladder_is_the_existing_one():
    """§17: no second strength system. The mapping only assigns existing
    rungs, and metadata stops at discovery (§11)."""

    class FakeEvidence:
        def __init__(self, etype, source="openalex", strength="discovery_evidence"):
            self.evidence_type = type("T", (), {"value": etype})()
            self.source = source
            self.strength = type("S", (), {"value": strength})()

    assert evidence_level_for([]) is ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE
    assert evidence_level_for([FakeEvidence("metadata")]) is (
        ReactionEvidenceLevel.LITERATURE_DISCOVERY)
    assert evidence_level_for([FakeEvidence("abstract")]) is (
        ReactionEvidenceLevel.LITERATURE_DISCOVERY)
    assert evidence_level_for([FakeEvidence("fulltext")]) is (
        ReactionEvidenceLevel.LITERATURE_REACTION_SUPPORT)
    assert evidence_level_for(
        [FakeEvidence("experimental", strength="primary_document_evidence")]) is (
        ReactionEvidenceLevel.EXPERIMENTAL_REACTION_SUPPORT)
    assert evidence_level_for(
        [FakeEvidence("experimental", source="user_provided",
                      strength="user_provided_evidence")]) is (
        ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT)


# ---------------------------------------------------------------------------
# components and provenance (§7/§9/§62)
# ---------------------------------------------------------------------------


def test_a_component_keeps_the_role_the_source_gave_it():
    unknown = _component(role=ComponentRole.UNKNOWN, name="CuI",
                         entity_id=None, key="", smiles="")
    assert unknown.role is ComponentRole.UNKNOWN
    # a name-based component contributes a marked key material
    assert unknown.key_material() == "name:cui"
    assert _component().key_material().startswith("e:")


def test_a_product_may_stay_unresolved_and_the_reaction_survives():
    product = ReactionComponent(
        role=ComponentRole.PRODUCT, name="compound 8",
        mention=ChemicalMention(mention_id="m-1", raw_text="compound 8"))
    reaction = Reaction(reaction_id="RXN-2", reactants=[_component()],
                        products=[product],
                        status=ReactionStatus.PARTIAL)

    assert reaction.products[0].entity_id is None
    assert reaction.products[0].mention.resolution_status.value == "unresolved"
    assert reaction.status is ReactionStatus.PARTIAL
    assert len(reaction.components()) == 2


def test_a_route_link_records_a_comparison_not_a_promotion():
    link = RouteStepReactionLink(link_id="l-1", step_id="s-1", reaction_id="RXN-1")

    assert link.screening_only is True          # anything unmeasured screens only
    assert link.link_basis is LinkBasis.UNVERIFIED_STRUCTURE
    assert link.match_type is ReactionMatchType.NO_MATCH
    exact = RouteStepReactionLink(
        link_id="l-2", step_id="s-1", reaction_id="RXN-1",
        match_type=ReactionMatchType.EXACT,
        link_basis=LinkBasis.EXACT_STRUCTURE, screening_only=False)
    assert exact.screening_only is False

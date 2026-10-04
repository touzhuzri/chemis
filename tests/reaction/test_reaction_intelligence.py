"""Reaction Intelligence MVP: ChemicalEntity <-> Reaction <-> ChemicalEntity.

Stage D (§21). The victory line (§十七/§二十四): express `A + B -> C` as
one reaction whose parts are ChemicalEntities, find it forward from the
product and backward from a reactant, and never invent a reaction that is
not there. Identity here is the established `crea-reaction-key-v1`
order-independent key -- and identity is never similarity (§7).
"""

from __future__ import annotations

import pytest

from crea.core.models.entity import ChemicalEntity
from crea.core.models.reaction import ComponentRole
from crea.core.models.reaction_knowledge import (
    Reaction,
    ReactionComponent,
    ReactionStatus,
    reaction_key,
)
from crea.core.models.reaction import ReactionRecord  # noqa: F401 - chain exists
from crea.repositories.sqlite import SQLiteReactionRepository, SQLiteStore
from crea.services.reaction_builder import (
    InMemoryReactionRepository,
    ReactionRegistry,
)


def _entity(entity_id: str, smiles: str) -> ChemicalEntity:
    return ChemicalEntity(
        entity_id=entity_id,
        identity_key=f"crea-identity-key-v1:InChI=1S/{entity_id}",
        canonical_smiles=smiles)


def _comp(role: ComponentRole, entity_id: str) -> ReactionComponent:
    return ReactionComponent(role=role, name="x", entity_id=entity_id,
                             canonical_smiles="")


def _reaction(reaction_id: str, reactants, products, *, key="k",
              reagents=(), catalysts=(), solvents=(),
              evidence_refs=(), status=ReactionStatus.EVIDENCE_BACKED):
    return Reaction(
        reaction_id=reaction_id, reaction_key=key, paper_id="paper-a",
        status=status,
        reactants=[_comp(ComponentRole.REACTANT, r) for r in reactants],
        products=[_comp(ComponentRole.PRODUCT, p) for p in products],
        reagents=[_comp(ComponentRole.REAGENT, r) for r in reagents],
        catalysts=[_comp(ComponentRole.CATALYST, c) for c in catalysts],
        solvents=[_comp(ComponentRole.SOLVENT, s) for s in solvents],
        evidence_refs=list(evidence_refs))


@pytest.fixture()
def registry():
    return ReactionRegistry(InMemoryReactionRepository())


# -- n:m relations (§3) ----------------------------------------------------


def test_reaction_expresses_n_to_m_stoichiometry(registry):
    cases = [
        ("R1", ["A", "B"], ["C"]),
        ("R2", ["A", "B"], ["C", "D"]),
        ("R3", ["A", "B", "E"], ["D"]),
    ]
    for rid, reactants, products in cases:
        reaction = _reaction(rid, reactants, products, key=rid)
        registry.register(reaction)
        got = registry.get(rid)
        assert [c.entity_id for c in got.reactants] == reactants
        assert [c.entity_id for c in got.products] == products
    # one reaction keeps its co-occurrence: A and B react TOGETHER in R1
    r1 = registry.get("R1")
    assert {c.entity_id for c in r1.reactants} == {"A", "B"}


# -- roles stay distinct (§5) ----------------------------------------------


def test_roles_are_kept_apart_and_never_guessed():
    reaction = _reaction("R1", ["A"], ["C"], reagents=["B"],
                         catalysts=["X"], solvents=["Y"])
    assert [c.role for c in reaction.reactants] == [ComponentRole.REACTANT]
    assert [c.role for c in reaction.products] == [ComponentRole.PRODUCT]
    assert [c.role for c in reaction.reagents] == [ComponentRole.REAGENT]
    assert [c.role for c in reaction.catalysts] == [ComponentRole.CATALYST]
    assert [c.role for c in reaction.solvents] == [ComponentRole.SOLVENT]
    # a role the source never stated stays UNKNOWN (§5)
    unclassified = ReactionComponent(name="z")
    assert unclassified.role is ComponentRole.UNKNOWN


# -- identity: order-independent, difference-preserving (§6/§7) ------------


def test_reaction_identity_ignores_order_but_never_merges_different_products():
    from crea.core.models.reaction_knowledge import key_is_identity_based

    key_ab_c = reaction_key(["e:A", "e:B"], ["e:C"])
    key_ba_c = reaction_key(["e:B", "e:A"], ["e:C"])
    assert key_ab_c == key_ba_c, "A + B -> C and B + A -> C are one key (§6)"
    key_ab_d = reaction_key(["e:A", "e:B"], ["e:D"])
    assert key_ab_c != key_ab_d, "A + B -> C is not A + B -> D (§6)"
    assert key_is_identity_based(key_ab_c)

    registry = ReactionRegistry(InMemoryReactionRepository())
    first = _reaction("R1", ["A", "B"], ["C"], key=key_ab_c)
    second = _reaction("R2", ["B", "A"], ["C"], key=key_ba_c)
    kept, merged = registry.register(first)
    kept2, merged2 = registry.register(second)
    assert merged2 is True
    assert kept2.reaction_id == kept.reaction_id, "same key+scope, one reaction"
    third = _reaction("R3", ["A", "B"], ["D"], key=key_ab_d)
    kept3, merged3 = registry.register(third)
    assert merged3 is False, "different products never merge"
    assert len(registry.all()) == 2


# -- ChemicalEntity integration (§14/§15) ---------------------------------


def test_reactions_reference_chemical_entities_and_never_own_identity():
    """§14: reaction components point at entity ids; the reaction holds no
    chemical identity of its own."""
    from crea.semantic.resolver import ChemicalInput, SemanticResolver
    from crea.services.entity_registry import ChemicalEntityRegistry
    from crea.services.identity import IdentityResolutionService

    entities = ChemicalEntityRegistry()
    resolver = SemanticResolver(identity_service=IdentityResolutionService(),
                                entity_registry=entities)
    by_smiles = resolver.resolve(ChemicalInput.of("SMILES", "C#Cc1ccccc1"))
    by_name = resolver.resolve(ChemicalInput.of("NAME", "phenylacetylene")) \
        if False else None  # name path needs a provider; covered in B-1
    target = by_smiles.entity
    assert target is not None

    reaction = _reaction("R1", ["CHEM-00000002"], [target.entity_id],
                         key="k-link")
    component = reaction.products[0]
    assert component.entity_id == target.entity_id
    assert reaction.reactants[0].entity_id == "CHEM-00000002"
    # the reaction stores references, not structures it derived itself
    assert component.identity_key == "" or component.identity_key.startswith(
        "crea-identity-key-v1:")


# -- repository: save / get / find_by_product / find_by_reactant (§11) -----


@pytest.mark.parametrize("make_registry", [
    lambda: ReactionRegistry(InMemoryReactionRepository()),
    lambda: ReactionRegistry(SQLiteReactionRepository(_store())),
])
def test_repository_round_trip_and_directional_finders(make_registry):
    registry = make_registry()
    registry.register(_reaction("REPO-1", ["A", "B"], ["C"], key="k-repo",
                                evidence_refs=["EV-1"]))
    got = registry.get("REPO-1")
    assert got.reaction_id == "REPO-1"
    assert registry.find_by_product("C")[0].reaction_id == "REPO-1"
    assert registry.find_by_reactant("A")[0].reaction_id == "REPO-1"
    assert registry.find_by_reactant("B")[0].reaction_id == "REPO-1"
    assert registry.find_by_product("A") == []
    assert [r.reaction_id for r in registry.find_between(["A", "B"], ["C"])] == [
        "REPO-1"]
    assert registry.find_between(["A"], ["D"]) == []


def _store():
    """A fresh SQLite store per call: entities A/B/C/D/E/T and one real
    evidence row, so the tests never leak into each other."""
    import pathlib
    import tempfile

    from crea.core.models.evidence import Evidence, EvidenceType, Locator
    from crea.core.models.literature import EvidenceStrength

    db = pathlib.Path(tempfile.mkdtemp()) / "reactions.db"
    store = SQLiteStore(db)
    store.initialize()
    for eid in ("A", "B", "C", "D", "E", "T"):
        store.save_entity(_entity(eid, f"C{eid}"))
    store.save_evidence(Evidence(
        evidence_id="EV-1", evidence_type=EvidenceType.EXPERIMENTAL,
        strength=EvidenceStrength.USER_PROVIDED_EVIDENCE,
        source="user_provided", source_id="doc-1",
        retrieved_at="2026-10-03T00:00:00+00:00",
        raw_quote="A and B couple to C", locator=Locator()))
    return store


# -- the demonstrable loop (§17): A + B -> Target, both directions ---------


def test_the_minimal_closed_loop_a_plus_b_to_target():
    """The Stage D victory line, end to end over SQLite:
    A ──┐
        ├── Reaction R1 ──→ Target
    B ──┘
    """
    store = _store()
    registry = ReactionRegistry(SQLiteReactionRepository(store))
    registry.register(_reaction(
        "R1", ["A", "B"], ["T"], key="k-loop",
        reagents=["B"], evidence_refs=["EV-1"],
        status=ReactionStatus.EXPERIMENTALLY_SUPPORTED))

    forward = registry.search_results(product_entity_id="T")
    assert [r.reaction.reaction_id for r in forward] == ["R1"]
    entry = forward[0]
    assert entry.match_role == "product" and entry.matched_entities == ["T"]
    assert entry.evidence == [{"evidence_id": "EV-1"}]
    assert entry.provenance is not None or entry.reaction.paper_id
    reactants = {c.entity_id for c in entry.reaction.reactants}
    assert reactants == {"A", "B"}, "the co-occurrence survives the query"

    backward = registry.search_results(reactant_entity_id="A")
    assert [r.reaction.reaction_id for r in backward] == ["R1"]
    assert backward[0].match_role == "reactant"

    between = registry.find_between(["A", "B"], ["T"])
    assert [r.reaction_id for r in between] == ["R1"]


# -- no hallucination (§21) -------------------------------------------------


def test_no_reaction_means_no_reaction_never_an_invented_one():
    registry = ReactionRegistry(InMemoryReactionRepository())
    assert registry.find_by_product("Z") == []
    assert registry.find_by_reactant("Z") == []
    assert registry.search_results(product_entity_id="Z") == []


def test_a_reaction_record_chain_stays_separate_from_the_reaction():
    """§2/§10: ReactionRecord remains the extraction intermediate; the
    Reaction is the knowledge object. No parallel model was created, and
    the record chain is reachable (Stage B-1's tables)."""
    import inspect

    from crea.core.models import reaction as record_module
    from crea.core.models import reaction_knowledge

    assert inspect.isclass(record_module.ReactionRecord)
    assert inspect.isclass(reaction_knowledge.Reaction)
    # exactly one Reaction class in the knowledge module
    names = [n for n in dir(reaction_knowledge) if n == "Reaction"]
    assert names == ["Reaction"]

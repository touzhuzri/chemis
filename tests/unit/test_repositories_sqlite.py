"""The SQLite repository: schema, identity, dedup, atomicity, reopen.

Phase 3.11 §50. What persistence means here is one sentence: save, close,
reopen, get -- must succeed. Everything else in this module is the rules
that keep the knowledge honest while that happens: identities are the
domain's, evidence never rewrites, mentions keep their scope, one reaction
is never half-stored, and a database this code does not speak is refused
rather than "helpfully" migrated.
"""

from __future__ import annotations

import sqlite3

import pytest

from crea.core.models.entity import ChemicalEntity, ChemicalMention
from crea.core.models.evidence import Evidence, EvidenceType, Locator
from crea.core.models.literature import EvidenceStrength, Paper
from crea.core.models.reaction import ComponentRole
from crea.core.models.reaction_knowledge import (
    Reaction,
    ReactionComponent,
    ReactionStatus,
    RouteStepReactionLink,
    reaction_key,
)
from crea.core.models.route import (
    CandidateRoute,
    MoleculeRole,
    MoleculeSource,
    ReactionMatchType,
    ReactionStep,
    RouteMolecule,
    StepStatus,
    SupportLevel,
)
from crea.repositories.errors import (
    ConstraintViolation,
    Duplicate,
    NotFound,
    RepositoryError,
    SchemaError,
    UNSUPPORTED_SCHEMA_VERSION,
)
from crea.repositories.sqlite import (
    SCHEMA_VERSION,
    SQLiteChemicalEntityRepository,
    SQLiteReactionRepository,
    SQLiteStore,
    dump_json,
)


# -- builders ---------------------------------------------------------------


def make_entity(index: int, *, name: str = "", inchi_suffix: str = "") -> ChemicalEntity:
    inchi = f"InChI=1S/C{index}{inchi_suffix}/c1-2-3/h1H"
    return ChemicalEntity(
        entity_id=f"CHEM-{index:08d}",
        identity_key=f"crea-identity-key-v1:{inchi}",
        canonical_smiles=f"C{'C' * index}",
        inchi=inchi,
        inchikey=f"KEY{index:023d}",
        names=[name] if name else [],
        created_at=f"2026-10-02T00:00:0{index % 10}+00:00",
    )


def make_evidence(text: str = "82% yield", *, source_id: str = "doc-1") -> Evidence:
    return Evidence.build(
        evidence_type=EvidenceType.EXPERIMENTAL,
        strength=EvidenceStrength.USER_PROVIDED_EVIDENCE,
        source="user_provided", source_id=source_id,
        raw_quote=text,
        locator=Locator(document_id=source_id, section="Experimental"),
        retrieved_at="2026-10-02T00:00:00+00:00",
    )


def make_reaction(reaction_id: str = "RXN-test-1", *, key: str = "",
                  evidence_refs=(), components=()) -> Reaction:
    key = key or reaction_key(["e:crea-identity-key-v1:InChI=1S/C1/c1-2-3/h1H"],
                              ["e:crea-identity-key-v1:InChI=1S/C2/c1-2-3/h1H"])
    return Reaction(
        reaction_id=reaction_id,
        reaction_key=key,
        reactants=[c for c in components if c.role is ComponentRole.REACTANT],
        products=[c for c in components if c.role is ComponentRole.PRODUCT],
        status=ReactionStatus.EVIDENCE_BACKED,
        paper_id="paper-a",
        doi="10.0000/a",
        source_id="paper-a",
        evidence_refs=list(evidence_refs),
        created_at="2026-10-02T00:00:00+00:00",
    )


def make_route(route_id: str = "route-1") -> CandidateRoute:
    target = RouteMolecule(molecule_id="m-target", canonical_smiles="C#Cc1ccccc1",
                           role=MoleculeRole.TARGET, source=MoleculeSource.USER_INPUT)
    step = ReactionStep(
        step_id=f"{route_id}-s1", step_index=0,
        products=[RouteMolecule(molecule_id="m-p", canonical_smiles="C#Cc1ccccc1")],
        reactants=[RouteMolecule(molecule_id="m-r", canonical_smiles="C#Cc1ccccc1")],
        status=StepStatus.CANDIDATE, support_level=SupportLevel.NO_SUPPORT)
    return CandidateRoute(route_id=route_id, target=target, steps=[step])


# -- schema -----------------------------------------------------------------


def test_initialize_is_idempotent_and_stamps_the_schema_version(tmp_path):
    store = SQLiteStore(tmp_path / "crea.db")
    store.initialize()
    store.initialize()  # running it again must change nothing and not fail
    assert store.schema_version == SCHEMA_VERSION == 1
    store.close()


def test_a_future_schema_version_is_refused_not_rewritten(tmp_path):
    path = tmp_path / "crea.db"
    store = SQLiteStore(path)
    store.initialize()
    store._execute("UPDATE schema_meta SET value = '99' WHERE key = 'schema_version'")
    store.close()

    reopened = SQLiteStore(path)
    with pytest.raises(SchemaError) as excinfo:
        reopened.initialize()
    assert excinfo.value.code == UNSUPPORTED_SCHEMA_VERSION
    assert "UNSUPPORTED_SCHEMA_VERSION" in str(excinfo.value)
    # the database was not "helpfully" downgraded behind the caller's back
    row = reopened._query("SELECT value FROM schema_meta WHERE key = 'schema_version'")
    assert row[0]["value"] == "99"
    reopened.close()


# -- entities (§10-§12) -----------------------------------------------------


def test_entities_round_trip_through_close_and_reopen(tmp_path):
    path = tmp_path / "crea.db"
    store = SQLiteStore(path)
    store.initialize()
    entity = make_entity(1, name="phenylacetylene")
    store.save_entity(entity)
    store.close()

    reopened = SQLiteStore(path)
    reopened.initialize()
    loaded = reopened.get_entity("CHEM-00000001")
    assert loaded is not None
    assert loaded.identity_key == entity.identity_key
    assert loaded.names == ["phenylacetylene"]
    assert loaded.canonical_smiles == "CC"
    assert reopened.find_entity_by_identity_key(entity.identity_key).entity_id == (
        "CHEM-00000001")
    assert reopened.get_entity("CHEM-99999999") is None
    reopened.close()


def test_re_registering_an_identity_reuses_the_entity_and_merges(tmp_path):
    """§11: the second registration must not mint CHEM-00000002. This is
    the persistence-compatibility regression test for the registry: the
    same semantics as in-memory, now surviving a restart."""
    from crea.core.models.identity import (
        ChemicalIdentityCandidate,
        ChemicalIdentityResolution,
        IdentityBasis,
        StructureInput,
        StructureInputKind,
        StructureResolutionStatus,
    )
    from crea.services.entity_registry import ChemicalEntityRegistry

    def resolution(name: str, synonym: str | None = None):
        inchi = "InChI=1S/C8H8/c1-2-8-6-4-3-5-7-8/h1,3-7H,2H2"
        candidate = ChemicalIdentityCandidate(
            candidate_id="c1", source="PUBCHEM", source_id="733",
            input_name=name, synonyms=[synonym] if synonym else [],
            canonical_smiles="C#Cc1ccccc1", inchi=inchi,
            identity_basis=IdentityBasis.EXACT_STRUCTURE)
        return ChemicalIdentityResolution(
            status=StructureResolutionStatus.RESOLVED,
            input=StructureInput(kind=StructureInputKind.NAME, value=name),
            candidates=[candidate], selected=candidate)

    path = tmp_path / "crea.db"
    store = SQLiteStore(path)
    store.initialize()
    registry = ChemicalEntityRegistry(SQLiteChemicalEntityRepository(store))
    first = registry.register_resolution(resolution("phenylacetylene"))
    assert first.entity_id == "CHEM-00000001"
    store.close()

    # a new process: the same identity comes back
    store = SQLiteStore(path)
    store.initialize()
    registry = ChemicalEntityRegistry(SQLiteChemicalEntityRepository(store))
    again = registry.register_resolution(resolution("ethynylbenzene"))
    assert again.entity_id == "CHEM-00000001", "one identity is one entity (§11)"
    loaded = store.get_entity("CHEM-00000001")
    assert "phenylacetylene" in loaded.names
    assert "ethynylbenzene" in loaded.names, "the merge persists (§92)"
    assert len(store.list_entities()) == 1

    # a NEW identity continues the numbering where the store left off (§12)
    third = registry.register_resolution(_other_resolution())
    assert third.entity_id == "CHEM-00000002"
    store.close()


def _other_resolution():
    from crea.core.models.identity import (
        ChemicalIdentityCandidate,
        ChemicalIdentityResolution,
        IdentityBasis,
        StructureInput,
        StructureInputKind,
        StructureResolutionStatus,
    )
    inchi = "InChI=1S/C8H8/c1-2-8-6-4-3-5-7-8/h2,3-7H,1H2"
    candidate = ChemicalIdentityCandidate(
        candidate_id="c2", source="PUBCHEM", source_id="7501",
        input_name="styrene", canonical_smiles="C=Cc1ccccc1", inchi=inchi,
        identity_basis=IdentityBasis.EXACT_STRUCTURE)
    return ChemicalIdentityResolution(
        status=StructureResolutionStatus.RESOLVED,
        input=StructureInput(kind=StructureInputKind.NAME, value="styrene"),
        candidates=[candidate], selected=candidate)


def test_name_search_returns_every_match_and_creates_nothing(tmp_path):
    store = SQLiteStore(tmp_path / "crea.db")
    store.initialize()
    a = make_entity(1, name="Phenylacetylene")
    b = make_entity(2, name="phenylacetylene")
    store.save_entity(a)
    store.save_entity(b)
    found = store.find_entities_by_name("phenylacetylene")
    assert [e.entity_id for e in found] == ["CHEM-00000001", "CHEM-00000002"]
    assert store.find_entities_by_name("not-a-name") == []
    assert len(store.list_entities()) == 2, "a search never registers (§30)"
    store.close()


# -- evidence (§18/§19/§48) -------------------------------------------------


def test_evidence_is_deduplicated_by_id_and_never_rewritten(tmp_path):
    store = SQLiteStore(tmp_path / "crea.db")
    store.initialize()
    evidence = make_evidence("82% yield")
    returned = store.save_evidence(evidence)
    assert returned.evidence_id == evidence.evidence_id

    # the same citation again: the stored evidence comes back unchanged
    again = store.save_evidence(evidence)
    assert again.evidence_id == evidence.evidence_id
    assert len(store.list_evidence()) == 1

    # and an impostor with the same id but different text cannot rewrite
    # the quote (§48) -- the stored row stands
    impostor = evidence.model_copy(update={"raw_quote": "99% yield"})
    kept = store.save_evidence(impostor)
    assert kept.raw_quote == "82% yield"
    assert store.get_evidence(evidence.evidence_id).raw_quote == "82% yield"
    store.close()


# -- mentions (§20/§21) -----------------------------------------------------


def test_mentions_keep_their_scope_across_reopen(tmp_path):
    """Paper A's "compound 7" is not Paper B's (§76 at storage level)."""
    path = tmp_path / "crea.db"
    store = SQLiteStore(path)
    store.initialize()
    store.save_entity(make_entity(1, name="A"))
    store.save_entity(make_entity(2, name="B"))
    a = ChemicalMention(mention_id="m-a", text="compound 7", source_id="paper-a",
                        resolution_status="resolved", entity_id="CHEM-00000001")
    b = ChemicalMention(mention_id="m-b", text="compound 7", source_id="paper-b",
                        resolution_status="resolved", entity_id="CHEM-00000002")
    store.save_mention(a)
    store.save_mention(b)
    store.close()

    reopened = SQLiteStore(path)
    reopened.initialize()
    assert reopened.get_mention("m-a").entity_id == "CHEM-00000001"
    assert reopened.get_mention("m-b").entity_id == "CHEM-00000002"
    assert reopened.get_mention("m-a").raw_text == "compound 7"
    reopened.close()


# -- papers (§16/§17/§94) ---------------------------------------------------


def test_papers_dedup_on_doi_and_keep_conflict_information(tmp_path):
    store = SQLiteStore(tmp_path / "crea.db")
    store.initialize()
    first = Paper(paper_id="10.0000/a", title="T", retrieved_at="2026-10-02T00:00:00+00:00",
                  sources=["openalex"], merge_conflicts=["title:openalex|crossref"])
    store.save_paper(first)
    second = first.model_copy(update={
        "title": "T2", "sources": ["crossref"],
        "merge_conflicts": ["journal:openalex|crossref"]})
    store.save_paper(second)

    loaded = store.get_paper("10.0000/a")
    assert loaded.title == "T2", "the merged view wins"
    assert loaded.sources == ["openalex", "crossref"], "sources accumulate (§91)"
    assert loaded.merge_conflicts == ["title:openalex|crossref",
                                      "journal:openalex|crossref"], (
        "conflict information is never dropped (§94)")

    # NO_DOI works are not merged with each other (§17)
    store.save_paper(Paper(paper_id="w1", retrieved_at="t"))
    store.save_paper(Paper(paper_id="w2", retrieved_at="t"))
    assert len(store.list_papers()) == 3
    store.close()


# -- reactions (§13-§15, §51) -----------------------------------------------


def test_a_reaction_saves_with_its_components_and_citations(tmp_path):
    store = SQLiteStore(tmp_path / "crea.db")
    store.initialize()
    entity = make_entity(1, name="phenylacetylene")
    store.save_entity(entity)
    evidence = make_evidence()
    store.save_evidence(evidence)
    mention = ChemicalMention(mention_id="m-1", text="phenylacetylene",
                              source_id="paper-a", resolution_status="resolved",
                              entity_id=entity.entity_id)
    component = ReactionComponent(role=ComponentRole.REACTANT,
                                  name="phenylacetylene", mention=mention,
                                  entity_id=entity.entity_id,
                                  identity_key=entity.identity_key,
                                  canonical_smiles=entity.canonical_smiles)
    reaction = make_reaction("RXN-1", components=[component],
                             evidence_refs=[evidence.evidence_id])
    stored = store.save_reaction(reaction)
    assert stored.reaction_id == "RXN-1"

    loaded = store.get_reaction("RXN-1")
    assert loaded is not None
    assert loaded.status is ReactionStatus.EVIDENCE_BACKED
    assert [c.name for c in loaded.reactants] == ["phenylacetylene"]
    assert loaded.reactants[0].mention is not None
    assert loaded.reactants[0].mention.mention_id == "m-1"
    assert loaded.reactants[0].entity_id == entity.entity_id
    assert store.evidence_for_reaction("RXN-1")[0].evidence_id == evidence.evidence_id
    assert store.find_reactions_by_component_entity(entity.entity_id)[0].reaction_id == "RXN-1"
    store.close()


def test_a_dangling_citation_rolls_the_whole_reaction_back(tmp_path):
    """§79: deliberately break the evidence FK mid-save. The reaction must
    not survive in pieces -- no row, no components, no citations."""
    store = SQLiteStore(tmp_path / "crea.db")
    store.initialize()
    store.save_entity(make_entity(1))
    mention = ChemicalMention(mention_id="m-1", text="x", source_id="paper-a")
    reaction = make_reaction(
        "RXN-bad",
        components=[ReactionComponent(role=ComponentRole.REACTANT,
                                      name="x", mention=mention)],
        evidence_refs=["EVIDENCE-does-not-exist"])
    with pytest.raises(ConstraintViolation):
        store.save_reaction(reaction)
    assert store.get_reaction("RXN-bad") is None
    assert store._query("SELECT COUNT(*) AS n FROM reaction_components")[0]["n"] == 0
    assert store._query("SELECT COUNT(*) AS n FROM reaction_evidence")[0]["n"] == 0
    store.close()


def test_reaction_dedup_follows_the_key_semantics_exactly(tmp_path):
    """§15/§21: identity-based keys merge within one paper scope; a
    name-tainted key never forces two reports together, and the same key
    in another paper is another reaction."""
    store = SQLiteStore(tmp_path / "crea.db")
    store.initialize()
    registry = ReactionRegistry_over(store)

    identity_key = reaction_key(["e:k1"], ["e:k2"])
    first = make_reaction("RXN-a", key=identity_key)
    kept, merged = registry.register(first)
    assert kept.reaction_id == "RXN-a" and not merged

    # same scope, same identity key, different report -> merges (§93)
    second = make_reaction("RXN-b", key=identity_key)
    kept, merged = registry.register(second)
    assert merged and kept.reaction_id == "RXN-a"

    # same key, other paper -> two reactions kept
    third = make_reaction("RXN-c", key=identity_key)
    third.paper_id = "paper-b"
    third.doi = "10.0000/b"
    kept, merged = registry.register(third)
    assert not merged and kept.reaction_id == "RXN-c"
    assert len(store.list_reactions()) == 2

    # name-tainted key: two reports stay two, even in one scope (§15)
    tainted = reaction_key(["name:foo"], ["e:k2"])
    registry.register(make_reaction("RXN-d", key=tainted))
    registry.register(make_reaction("RXN-e", key=tainted))
    assert len(store.list_reactions()) == 4
    store.close()


def ReactionRegistry_over(store):
    from crea.services.reaction_builder import ReactionRegistry

    return ReactionRegistry(SQLiteReactionRepository(store))


# -- routes and links (§22/§23/§68) -----------------------------------------


def test_routes_steps_and_links_round_trip(tmp_path):
    store = SQLiteStore(tmp_path / "crea.db")
    store.initialize()
    store.save_evidence(make_evidence())
    store.save_entity(make_entity(1))
    mention = ChemicalMention(mention_id="m-1", text="x", source_id="paper-a",
                              resolution_status="resolved",
                              entity_id="CHEM-00000001")
    reaction = make_reaction(
        "RXN-1",
        components=[ReactionComponent(role=ComponentRole.REACTANT, name="x",
                                      mention=mention, entity_id="CHEM-00000001")],
        evidence_refs=[make_evidence().evidence_id])
    store.save_reaction(reaction)
    route = make_route()
    store.save_route(route)
    link = RouteStepReactionLink(
        link_id="link-1", step_id="route-1-s1", reaction_id="RXN-1",
        match_type=ReactionMatchType.EXACT, screening_only=False,
        source_id="paper-a")
    store.save_route_reaction_link(link)

    loaded = store.get_route("route-1")
    assert loaded is not None
    assert loaded.steps[0].step_id == "route-1-s1"
    assert loaded.target.role is MoleculeRole.TARGET

    recovered = store.links_for_reaction("RXN-1")
    assert [(r, l.link_id) for r, l in recovered] == [("route-1", "link-1")]
    assert recovered[0][1].screening_only is False
    assert store.get_reaction("RXN-1") is not None
    store.close()


def test_a_link_to_an_unstored_step_is_refused(tmp_path):
    store = SQLiteStore(tmp_path / "crea.db")
    store.initialize()
    store.save_evidence(make_evidence())
    store.save_reaction(make_reaction("RXN-1"))
    link = RouteStepReactionLink(link_id="link-x", step_id="no-such-step",
                                 reaction_id="RXN-1")
    with pytest.raises(NotFound):
        store.save_route_reaction_link(link)
    store.close()


# -- sessions (§24/§25/§44) -------------------------------------------------


def test_sessions_survive_reopen_and_never_overwrite_each_other(tmp_path):
    from crea.core.models.research import ResearchTask
    from crea.core.models.session import ResearchSession, WorkflowStatus

    path = tmp_path / "crea.db"
    store = SQLiteStore(path)
    store.initialize()
    store.save_entity(make_entity(1))
    store.save_evidence(make_evidence())
    store.save_reaction(make_reaction("RXN-1"))
    for index in (1, 2):
        session = ResearchSession(
            session_id=f"session-{index}",
            task=ResearchTask(task_id=f"t{index}", question=f"q{index}"),
            status=WorkflowStatus.COMPLETED,
            chemical_entity_ids=["CHEM-00000001"],
            reaction_ids=["RXN-1"],
            evidence_ids=[make_evidence().evidence_id],
        )
        store.save_session(session)
    store.close()

    reopened = SQLiteStore(path)
    reopened.initialize()
    one = reopened.get_session("session-1")
    assert one is not None
    assert one.task.question == "q1"
    assert one.chemical_entity_ids == ["CHEM-00000001"]
    assert one.reaction_ids == ["RXN-1"]
    assert len(reopened.list_sessions()) == 2, "sessions accumulate (§44)"
    reopened.close()


# -- query discipline (§29/§59) ---------------------------------------------


def test_listings_are_in_a_deterministic_order(tmp_path):
    store = SQLiteStore(tmp_path / "crea.db")
    store.initialize()
    for index in (3, 1, 2):
        store.save_entity(make_entity(index, name=f"n{index}"))
    assert [e.entity_id for e in store.list_entities()] == [
        "CHEM-00000001", "CHEM-00000002", "CHEM-00000003"]
    store.close()


def test_json_serialization_is_stable_and_sorted(tmp_path):
    assert dump_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_a_closed_store_refuses_rather_than_leaking_sqlite_errors(tmp_path):
    store = SQLiteStore(tmp_path / "crea.db")
    store.initialize()
    store.close()
    store.close()  # idempotent
    with pytest.raises(RepositoryError):
        store.get_entity("CHEM-00000001")


def test_foreign_keys_are_enforced_on_the_connection(tmp_path):
    store = SQLiteStore(tmp_path / "crea.db")
    store.initialize()
    with pytest.raises((ConstraintViolation, Duplicate)):
        store._execute(
            "INSERT INTO entity_names (entity_id, name, position) VALUES (?,?,?)",
            ("CHEM-nope", "x", 0))
    row = store._query("PRAGMA foreign_keys")
    assert row[0][0] == 1
    store.close()

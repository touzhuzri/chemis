"""KnowledgeBase -- the one door into persistent chemical research knowledge.

Phase 3.11. One research run produces entities, mentions, papers, evidence,
reactions, routes and a session; the next run must be able to *reuse* what
those established instead of minting `CHEM-00000099` for a compound that
already has an id (§41). This facade is how that happens: a single entry
point for storing validated domain objects, for asking deterministic
questions of them, and for committing one research result as a unit (§52).

What it is not (§106/§45): a cache, and not memory about people. It holds
chemical research facts -- each with its source, its retrieved_at, its
provenance (§62) -- and nothing else. There is no user profile here, no
preference field, no chat history, and no delete API (§90): research
evidence accumulates.

The write rule (§107) is the same as everywhere else in CREA: only a
validated domain object reaches storage. An LLM proposes, the policy
validates, the identity layer resolves, and whatever survives that arrives
here as a `ChemicalEntity` / `Reaction` / `Evidence` / ... -- never as raw
model JSON. Queries are read-only (§108): nothing here summarizes an
object and writes the summary back over it.

Layering: this is a service. SQL lives in `crea.repositories`; chemistry
lives in `crea.chem`; providers and models live nowhere near this file
(§84/§85).
"""

from __future__ import annotations

from pathlib import Path

from crea.core.models.entity import ChemicalEntity, ChemicalMention
from crea.core.models.evidence import Evidence
from crea.core.models.literature import Paper
from crea.core.models.reaction_knowledge import Reaction, RouteStepReactionLink
from crea.core.models.route import CandidateRoute
from crea.core.models.session import ResearchSession
from crea.repositories.errors import NotFound
from crea.repositories.sqlite import (
    SQLiteChemicalEntityRepository,
    SQLiteReactionRepository,
    SQLiteStore,
    default_db_path,
)


class KnowledgeBase:
    """Persistent chemical research knowledge, behind one facade.

    `KnowledgeBase(db_path=...)` names its database explicitly (tests use
    `tmp_path`; nothing in this class has a hardcoded location). Given no
    path at all it falls back to `data/crea.db` under the working
    directory -- configurable always, assumed never (§5/§37/§98).

        with KnowledgeBase(db_path) as kb:
            kb.initialize()
            kb.persist_research_result(...)

    or simply `with KnowledgeBase(db_path) as kb:` -- entering initializes
    and exiting closes the connection (§96). The instance is a thin
    coordinator over one `SQLiteStore`; the two registries' existing
    protocols are served by that same store, so a `ChemicalEntityRegistry`
    built from this base and one built in-memory behave identically apart
    from remembering (§4).
    """

    def __init__(self, db_path=None, *, store: SQLiteStore | None = None) -> None:
        self._store = (store if store is not None
                       else SQLiteStore(db_path if db_path is not None
                                        else default_db_path()))
        self._entities_registry = None
        self._reactions_registry = None

    # -- lifecycle ----------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._store.path

    def initialize(self) -> None:
        """Idempotent schema setup. Refuses an unknown schema version (§6-§8)."""
        self._store.initialize()

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "KnowledgeBase":
        self.initialize()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def store(self) -> SQLiteStore:
        return self._store

    def transaction(self):
        """One unit of work over everything this base writes (§51/§52)."""
        return self._store.transaction()

    # -- registries over the persistent store (§41) -------------------------

    def entity_registry(self):
        """A `ChemicalEntityRegistry` that remembers across processes.

        Re-registering an established identity returns `CHEM-00000001`
        again and merges names/identifiers into it (§11); a new identity
        continues the numbering where the database left off (§12).
        """
        from crea.services.entity_registry import ChemicalEntityRegistry

        if self._entities_registry is None:
            self._entities_registry = ChemicalEntityRegistry(
                SQLiteChemicalEntityRepository(self._store))
        return self._entities_registry

    def reaction_registry(self):
        """A `ReactionRegistry` whose dedup is the persistent one (§15/§21)."""
        from crea.services.reaction_builder import ReactionRegistry

        if self._reactions_registry is None:
            self._reactions_registry = ReactionRegistry(
                SQLiteReactionRepository(self._store))
        return self._reactions_registry

    def workspace_repository(self):
        """A `WorkspaceRepository` over this store (Stage F §21).

        Workspaces are conversation containers, not research knowledge; they
        live in the same database file but behind their own repository, and
        nothing here treats a chat message as a chemical fact.
        """
        from crea.repositories.sqlite import SQLiteWorkspaceRepository

        return SQLiteWorkspaceRepository(self._store)

    # -- storing knowledge --------------------------------------------------

    def save_entity(self, entity: ChemicalEntity) -> ChemicalEntity:
        return self._store.save_entity(entity)

    def save_mention(self, mention: ChemicalMention) -> ChemicalMention:
        return self._store.save_mention(mention)

    def save_paper(self, paper: Paper) -> Paper:
        return self._store.save_paper(paper)

    def save_evidence(self, evidence: Evidence) -> Evidence:
        return self._store.save_evidence(evidence)

    def save_reaction(self, reaction: Reaction) -> Reaction:
        return self._store.save_reaction(reaction)

    def save_reaction_record(self, record, *, reaction_id: str):
        """Store the experimental record behind a reaction (§25-§27).

        The record is the intermediate extraction result and the reaction
        the stable knowledge object; this is the chain between them, never
        a second reaction system. Conditions keep their raw values.
        """
        return self._store.save_reaction_record(record, reaction_id=reaction_id)

    def get_reaction_records(self, reaction_id: str) -> list:
        """Which experimental records this reaction rests on (§28)."""
        return self._store.get_reaction_records(reaction_id)

    def list_reaction_records(self) -> list:
        return self._store.list_reaction_records()

    def save_route(self, route: CandidateRoute) -> CandidateRoute:
        return self._store.save_route(route)

    def save_route_reaction_link(self, link: RouteStepReactionLink,
                                 *, route_id: str | None = None
                                 ) -> RouteStepReactionLink:
        return self._store.save_route_reaction_link(link, route_id=route_id)

    def link_paper_mention(self, paper_id: str, mention_id: str) -> None:
        self._store.link_paper_mention(paper_id, mention_id)

    def save_session(self, session: ResearchSession) -> ResearchSession:
        return self._store.save_session(session)

    # -- reaction evidence (Stage 3.4) --------------------------------------

    def save_reaction_evidence(self, evidence):
        """Store one ReactionEvidence record (Stage 3.4)."""
        return self._store.save_reaction_evidence(evidence)

    def get_reaction_evidence(self, evidence_id: str):
        return self._store.get_reaction_evidence(evidence_id)

    def list_reaction_evidence(self, reaction_template_id: str):
        return self._store.list_reaction_evidence(reaction_template_id)

    def find_reaction_evidence_by_source(self, source_type: str, source_id: str):
        """Evidence citing a given source -- the reverse query (Stage 4.4)."""
        return self._store.find_reaction_evidence_by_source(source_type, source_id)

    # -- research sessions (Stage 7.1) --------------------------------------

    def save_study(self, study):
        return self._store.save_study(study)

    def get_study(self, study_id: str):
        return self._store.get_study(study_id)

    def list_studies(self, status=None, limit: int = 100, offset: int = 0):
        return self._store.list_studies(status, limit, offset)

    def save_study_reference(self, study_id: str, reference):
        return self._store.save_study_reference(study_id, reference)

    def delete_study_reference(self, study_id: str, reference_id: str) -> bool:
        return self._store.delete_study_reference(study_id, reference_id)

    def list_study_references(self, study_id: str):
        return self._store.list_study_references(study_id)

    def append_study_event(self, study_id: str, event):
        return self._store.append_study_event(study_id, event)

    def list_study_events(self, study_id: str):
        return self._store.list_study_events(study_id)

    def save_study_artifact(self, study_id: str, artifact):
        return self._store.save_study_artifact(study_id, artifact)

    def get_study_artifact(self, study_id: str, artifact_id: str):
        return self._store.get_study_artifact(study_id, artifact_id)

    def list_study_artifacts(self, study_id: str):
        return self._store.list_study_artifacts(study_id)

    def get_study_detail(self, study_id: str):
        return self._store.get_study_detail(study_id)

    # -- research workflows (Stage 7.2) -------------------------------------

    def save_workflow(self, workflow):
        return self._store.save_workflow(workflow)

    def get_workflow(self, workflow_id: str):
        return self._store.get_workflow(workflow_id)

    def list_workflows(self, study_id: str):
        return self._store.list_workflows(study_id)

    def save_task(self, task):
        return self._store.save_task(task)

    def get_task(self, task_id: str):
        return self._store.get_task(task_id)

    def list_tasks(self, workflow_id: str):
        return self._store.list_tasks(workflow_id)

    def get_workflow_detail(self, workflow_id: str):
        return self._store.get_workflow_detail(workflow_id)

    # -- queries (§29-§34, §65-§69) ----------------------------------------

    def get_entity(self, entity_id: str) -> ChemicalEntity:
        entity = self._store.get_entity(entity_id)
        if entity is None:
            raise NotFound(f"no entity {entity_id!r} in the knowledge base")
        return entity

    def find_entity_by_identity_key(self, key: str) -> ChemicalEntity | None:
        return self._store.find_entity_by_identity_key(key)

    def find_entity_by_external_id(self, namespace: str,
                                   identifier: str) -> ChemicalEntity | None:
        return self._store.find_entity_by_external_id(namespace, identifier)

    def search_entities_by_name(self, name: str) -> list[ChemicalEntity]:
        """Every entity carrying this name (§30/§31). Never creates one."""
        return self._store.find_entities_by_name(name)

    def list_entities(self) -> list[ChemicalEntity]:
        return self._store.list_entities()

    def find_entity_by_inchikey(self, inchikey: str) -> ChemicalEntity | None:
        """The record whose Standard InChIKey matches (Stage 2.2)."""
        return self._store.find_entity_by_inchikey(inchikey)

    def count_entities(self) -> int:
        return self._store.count_entities()

    def delete_entity(self, entity_id: str) -> bool:
        """Remove a molecule record from the library (Stage 2.2)."""
        return self._store.delete_entity(entity_id)

    def get_paper(self, paper_id: str) -> Paper:
        paper = self._store.get_paper(paper_id)
        if paper is None:
            raise NotFound(f"no paper {paper_id!r} in the knowledge base")
        return paper

    def find_paper_by_doi(self, doi: str) -> Paper | None:
        return self._store.find_paper_by_doi(doi)

    def list_papers(self) -> list[Paper]:
        return self._store.list_papers()

    def get_evidence(self, evidence_id: str) -> Evidence:
        evidence = self._store.get_evidence(evidence_id)
        if evidence is None:
            raise NotFound(f"no evidence {evidence_id!r} in the knowledge base")
        return evidence

    def list_evidence(self) -> list[Evidence]:
        return self._store.list_evidence()

    def get_evidence_for_reaction(self, reaction_id: str) -> list[Evidence]:
        """Only citations that actually resolve (§67/§77).

        A reaction whose evidence never landed is not evidence-backed, and
        this list is exactly the distinction: nothing is returned for a ref
        that does not exist.
        """
        return self._store.evidence_for_reaction(reaction_id)

    def get_mention(self, mention_id: str) -> ChemicalMention:
        mention = self._store.get_mention(mention_id)
        if mention is None:
            raise NotFound(f"no mention {mention_id!r} in the knowledge base")
        return mention

    def list_mentions(self) -> list[ChemicalMention]:
        return self._store.list_mentions()

    def get_mentions_for_paper(self, paper_id: str) -> list[ChemicalMention]:
        """Paper -> ChemicalMention (§20/§76). Scopes never collapse."""
        return self._store.mentions_for_paper(paper_id)

    def get_papers_for_entity(self, entity_id: str) -> list[Paper]:
        """Paper -> ChemicalMention -> ChemicalEntity (§65)."""
        return self._store.papers_for_entity(entity_id)

    def get_reaction(self, reaction_id: str) -> Reaction:
        reaction = self._store.get_reaction(reaction_id)
        if reaction is None:
            raise NotFound(f"no reaction {reaction_id!r} in the knowledge base")
        return reaction

    def search_reactions(self, *, entity_id: str | None = None,
                         product_entity_id: str | None = None,
                         reactant_entity_ids: list[str] | None = None,
                         paper_id: str | None = None, doi: str | None = None,
                         reaction_key: str | None = None,
                         structure: str | None = None,
                         structure_mode: str = "exact") -> list[Reaction]:
        """Deterministic filtered reaction search (§32/§33).

        At least one filter, or `INVALID_QUERY` -- never a full dump. The
        semantics are the `ReactionRegistry`'s own, delegated rather than
        restated; structure filters go through the chemistry abstraction,
        with SQLite only narrowing candidates (§33).
        """
        return self.reaction_registry().search(
            entity_id=entity_id, product_entity_id=product_entity_id,
            reactant_entity_ids=reactant_entity_ids, paper_id=paper_id,
            doi=doi, reaction_key=reaction_key, structure=structure,
            structure_mode=structure_mode)

    def get_reactions_for_paper(self, paper_id: str) -> list[Reaction]:
        return self._store.find_reactions_by_paper(paper_id)

    def list_reactions(self) -> list[Reaction]:
        return self._store.list_reactions()

    def get_route(self, route_id: str) -> CandidateRoute:
        route = self._store.get_route(route_id)
        if route is None:
            raise NotFound(f"no route {route_id!r} in the knowledge base")
        return route

    def list_routes(self) -> list[CandidateRoute]:
        return self._store.list_routes()

    def get_routes_for_reaction(self, reaction_id: str
                                ) -> list[tuple[str, RouteStepReactionLink]]:
        """(route_id, link) for stored step<->reaction links only (§68)."""
        return self._store.links_for_reaction(reaction_id)

    def get_routes_for_entity(self, entity_id: str) -> list[dict]:
        """`{route_id, relation_role, step_id}` -- role and context kept (§69)."""
        return self._store.routes_for_entity(entity_id)

    def get_session(self, session_id: str) -> ResearchSession:
        session = self._store.get_session(session_id)
        if session is None:
            raise NotFound(f"no session {session_id!r} in the knowledge base")
        return session

    def list_sessions(self) -> list[ResearchSession]:
        """Every session kept, never overwritten (§44)."""
        return self._store.list_sessions()

    # -- one research result, committed together (§52) ----------------------

    def persist_research_result(self, *, session: ResearchSession,
                                entities=(), mentions=(), papers=(),
                                evidence=(), reactions=(), records=(),
                                routes=(), links=()) -> None:
        """Commit one research run's knowledge in one unit of work.

        Everything the run established goes in inside a single transaction:
        entities, mentions, papers, evidence, reactions (each with its
        components and citations, atomic on its own too, §51), the
        experimental records behind them (§25-§28), routes, the measured
        step<->reaction links, and finally the session that points at all
        of it (§25). If anything fails, none of it lands -- a database
        never holds half a reaction.

        `records` are `(reaction_id, ReactionRecord)` pairs or
        `ReactionRecord`s whose own id matches a reaction's `record_id`.
        Links may be plain `RouteStepReactionLink`s (the route is then read
        from the stored step) or `(route_id, link)` pairs when the caller
        already knows the triple.
        """
        with self._store.transaction():
            for entity in entities:
                self._store.save_entity(entity)
            for mention in mentions:
                self._store.save_mention(mention)
            for paper in papers:
                self._store.save_paper(paper)
            for item in evidence:
                self._store.save_evidence(item)
            for reaction in reactions:
                self._store.save_reaction(reaction)
                # Paper -> Mention, whenever the reaction says which paper
                # it was reported in AND that paper is stored (§65). The
                # relation row is never a dangling pointer: a reaction may
                # name a paper the knowledge base has not seen yet -- its
                # own field keeps that claim, and the relation appears once
                # the paper itself is stored. Nothing is inferred from a
                # title (§17).
                paper_id = reaction.paper_id or reaction.doi
                if paper_id and (self._store.get_paper(paper_id) is not None
                                 or self._store.find_paper_by_doi(paper_id)
                                 is not None):
                    resolved = self._store.get_paper(paper_id)
                    key = resolved.paper_id if resolved is not None else paper_id
                    for component in reaction.components():
                        if component.mention is not None:
                            self._store.link_paper_mention(
                                key, component.mention.mention_id)
            for item in records:
                if isinstance(item, tuple):
                    reaction_id, record = item
                    self._store.save_reaction_record(record,
                                                     reaction_id=reaction_id)
                else:
                    # find the reaction that carries this record id
                    for reaction in reactions:
                        if reaction.record_id == item.reaction_id:
                            self._store.save_reaction_record(
                                item, reaction_id=reaction.reaction_id)
                            break
            for route in routes:
                self._store.save_route(route)
            for item in links:
                if isinstance(item, tuple):
                    route_id, link = item
                    self._store.save_route_reaction_link(
                        link, route_id=route_id)
                else:
                    self._store.save_route_reaction_link(item)
            self._store.save_session(session)

    # -- export (§70/§71) ---------------------------------------------------

    def export_dict(self) -> dict:
        """The logical knowledge, in deterministic order (§70).

        Stable ordering (registration order where it exists, stable ids
        where it does not, §29), stable ids, and only knowledge: no
        credentials, no environment, no raw provider payloads (§109). This
        is a knowledge export -- explicitly *not* a database backup (§71);
        `crea.db` remains the storage.
        """
        return {
            "format": "crea-knowledge-export-v1",
            "schema_version": self._store.schema_version,
            "entities": [e.model_dump(mode="json")
                         for e in self._store.list_entities()],
            "mentions": [m.model_dump(mode="json")
                         for m in self._store.list_mentions()],
            "papers": [p.model_dump(mode="json")
                       for p in self._store.list_papers()],
            "evidence": [e.model_dump(mode="json")
                         for e in self._store.list_evidence()],
            "reactions": [r.model_dump(mode="json")
                          for r in self._store.list_reactions()],
            "reaction_records": [r.model_dump(mode="json")
                                 for r in self._store.list_reaction_records()],
            "routes": [r.model_dump(mode="json")
                       for r in self._store.list_routes()],
            "sessions": [s.model_dump(mode="json")
                         for s in self._store.list_sessions()],
        }

    def export(self, output_path) -> Path:
        """Write `knowledge.json` (UTF-8, deterministic bytes)."""
        import json

        target = Path(output_path)
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.export_dict(), sort_keys=True,
                             ensure_ascii=False, indent=2)
        target.write_text(payload + "\n", encoding="utf-8")
        return target


__all__ = ["KnowledgeBase"]

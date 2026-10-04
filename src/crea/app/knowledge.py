"""Knowledge use cases: browsing the research knowledge base.

Phase 4 (§60/§97/§99). The workbench's read side. Every answer is a DTO
of ids and values produced by the domain/query layers -- this module
orchestrates, it does not compute chemistry, run SQL, or render HTML.

Read-only by design (§93/§94): evidence and reactions have no update use
case here. Corrections are a future append-only mechanism, not an edit
endpoint.

Provenance navigation (§97) is first-class: `entity_navigation` and
`reaction_navigation` walk the real relation chains

    Reaction -> Evidence -> Paper/Document -> Mention -> Entity
    Entity -> Mentions -> Reactions -> Papers

as id links -- never a fabricated graph (§98).
"""

from __future__ import annotations

from pathlib import Path

from crea.app.dto import (
    ConflictObservationView,
    ConflictView,
    EntityView,
    EvidenceView,
    FacetView,
    GapView,
    KnowledgeStateView,
    MentionView,
    PaperView,
    ProvenanceChain,
    ReactionComponentView,
    ReactionView,
    RecordView,
    RouteStepView,
    RouteView,
    SearchResultView,
    SessionView,
)


class KnowledgeApplication:
    """Read-side use cases over the knowledge base.

    Each call opens a short-lived store (§53: no shared SQLite connection
    across threads) and closes it again; queries are cheap and the
    lifecycle can never leak a connection into an HTTP worker.
    """

    def __init__(self, db_path) -> None:
        self._db_path = Path(db_path)

    # -- plumbing -----------------------------------------------------------

    def _kb(self):
        from crea.services.knowledge_base import KnowledgeBase

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        return kb

    def _with(self, fn):
        kb = self._kb()
        try:
            return fn(kb)
        finally:
            kb.close()

    # -- entities (§17/§60) -------------------------------------------------

    def entity(self, entity_id: str) -> EntityView:
        from crea.repositories.errors import NotFound

        def read(kb):
            entity = kb.get_entity(entity_id)
            return EntityView(
                entity_id=entity.entity_id,
                identity_key=entity.identity_key,
                identity_basis=entity.identity_basis.value,
                canonical_smiles=entity.canonical_smiles,
                inchi=entity.inchi,
                inchikey=entity.inchikey,
                molecular_formula=entity.molecular_formula,
                molecular_weight=entity.molecular_weight,
                names=list(entity.names),
                external_identifiers=[
                    {"namespace": i.namespace, "identifier": i.identifier,
                     "source": i.source}
                    for i in entity.external_identifiers],
                provenance=(entity.provenance.model_dump(mode="json")
                            if entity.provenance else None),
                created_at=entity.created_at)

        try:
            return self._with(read)
        except NotFound as exc:
            raise KeyError(str(exc)) from exc

    def entity_navigation(self, entity_id: str) -> ProvenanceChain:
        """Entity -> Mentions -> Reactions -> Papers (§97), as id links."""

        def read(kb):
            entity = kb.get_entity(entity_id)
            mentions = [m for m in kb.list_mentions()
                        if m.entity_id == entity_id]
            reactions = kb.search_reactions(entity_id=entity_id)
            papers = kb.get_papers_for_entity(entity_id)
            return ProvenanceChain(
                kind="entity", id=entity_id,
                label=", ".join(entity.names) or entity_id,
                links={
                    "mentions": [m.mention_id for m in mentions],
                    "reactions": [r.reaction_id for r in reactions],
                    "papers": [p.paper_id for p in papers],
                    "routes": [h["route_id"]
                               for h in kb.get_routes_for_entity(entity_id)],
                })

        try:
            return self._with(read)
        except Exception as exc:  # NotFound
            raise KeyError(str(exc)) from exc

    def entity_mentions(self, entity_id: str) -> list[MentionView]:
        def read(kb):
            return [self._mention_view(m) for m in kb.list_mentions()
                    if m.entity_id == entity_id]
        return self._with(read)

    # -- reactions (§26-§28/§94) -------------------------------------------

    def reaction(self, reaction_id: str) -> ReactionView:
        def read(kb):
            reaction = kb.get_reaction(reaction_id)
            return self._reaction_view(reaction, kb)
        try:
            return self._with(read)
        except Exception as exc:
            raise KeyError(str(exc)) from exc

    def reaction_navigation(self, reaction_id: str) -> ProvenanceChain:
        """Reaction -> Evidence -> Paper/Document -> Mention -> Entity."""

        def read(kb):
            reaction = kb.get_reaction(reaction_id)
            evidence = kb.get_evidence_for_reaction(reaction_id)
            mentions = [c.mention.mention_id for c in reaction.components()
                        if c.mention is not None]
            entities = [c.entity_id for c in reaction.components()
                        if c.entity_id]
            papers = []
            if reaction.paper_id:
                papers.append(reaction.paper_id)
            if reaction.doi and reaction.doi not in papers:
                papers.append(reaction.doi)
            return ProvenanceChain(
                kind="reaction", id=reaction_id,
                label=reaction.status.value,
                links={
                    "evidence": [e.evidence_id for e in evidence],
                    "records": ([reaction.record_id] if reaction.record_id
                                else []),
                    "papers": papers,
                    "documents": ([reaction.document_id]
                                  if reaction.document_id else []),
                    "mentions": mentions,
                    "entities": entities,
                })

        try:
            return self._with(read)
        except Exception as exc:
            raise KeyError(str(exc)) from exc

    def reaction_record(self, reaction_id: str) -> list[RecordView]:
        def read(kb):
            kb.get_reaction(reaction_id)  # exists or KeyError below
            out = []
            for record in kb.get_reaction_records(reaction_id):
                out.append(RecordView(
                    record_id=record.reaction_id,
                    reaction_id=reaction_id,
                    source=record.source,
                    document_id=record.document_id,
                    paper_id=record.paper_id,
                    reaction_description=record.reaction_description,
                    conditions=record.conditions.model_dump(mode="json"),
                    evidence_ids=list(record.evidence_refs),
                    conflicts=[c.model_dump(mode="json")
                               for c in record.conflicts],
                    extractor=record.extractor))
            return out
        try:
            return self._with(read)
        except Exception as exc:
            raise KeyError(str(exc)) from exc

    # -- evidence (§18/§93: read-only) -------------------------------------

    def evidence(self, evidence_id: str) -> EvidenceView:
        def read(kb):
            item = kb.get_evidence(evidence_id)
            return self._evidence_view(item)
        try:
            return self._with(read)
        except Exception as exc:
            raise KeyError(str(exc)) from exc

    def evidence_for_reaction(self, reaction_id: str) -> list[EvidenceView]:
        def read(kb):
            return [self._evidence_view(e)
                    for e in kb.get_evidence_for_reaction(reaction_id)]
        return self._with(read)

    # -- papers -------------------------------------------------------------

    def paper(self, paper_id: str) -> PaperView:
        def read(kb):
            paper = kb.get_paper(paper_id)
            return PaperView(paper_id=paper.paper_id, doi=paper.doi,
                             title=paper.title, journal=paper.journal,
                             year=paper.year, url=paper.url)
        try:
            return self._with(read)
        except Exception as exc:
            raise KeyError(str(exc)) from exc

    def papers_for_entity(self, entity_id: str) -> list[PaperView]:
        def read(kb):
            return [PaperView(paper_id=p.paper_id, doi=p.doi, title=p.title,
                              journal=p.journal, year=p.year, url=p.url)
                    for p in kb.get_papers_for_entity(entity_id)]
        return self._with(read)

    # -- routes -------------------------------------------------------------

    def route(self, route_id: str) -> RouteView:
        def read(kb):
            route = kb.get_route(route_id)
            steps = []
            for step in route.steps:
                links = [{"link_id": l.link_id, "reaction_id": l.reaction_id,
                          "match_type": l.match_type.value,
                          "screening_only": l.screening_only}
                         for l in kb.store.links_for_step(step.step_id)]
                steps.append(RouteStepView(
                    step_id=step.step_id, step_index=step.step_index,
                    status=step.status.value,
                    support_level=step.support_level.value,
                    reactant_smiles=[m.canonical_smiles or m.smiles
                                     for m in step.reactants],
                    product_smiles=[m.canonical_smiles or m.smiles
                                    for m in step.products],
                    linked_reactions=links))
            return RouteView(
                route_id=route.route_id,
                target_smiles=route.target.canonical_smiles or route.target.smiles,
                score=route.score, score_source=route.score_source.value,
                source=route.source, steps=steps)
        try:
            return self._with(read)
        except Exception as exc:
            raise KeyError(str(exc)) from exc

    # -- sessions (§96) -----------------------------------------------------

    def sessions(self) -> list[SessionView]:
        def read(kb):
            return [self._session_view(s) for s in kb.list_sessions()]
        return self._with(read)

    def session(self, session_id: str) -> SessionView:
        def read(kb):
            return self._session_view(kb.get_session(session_id))
        try:
            return self._with(read)
        except Exception as exc:
            raise KeyError(str(exc)) from exc

    def session_model(self, session_id: str):
        """The domain ResearchSession, for the report renderer (§99).

        The renderer consumes domain/query results; it never opens a
        database itself -- this is the query half of that pair.
        """
        def read(kb):
            return kb.get_session(session_id)
        try:
            return self._with(read)
        except Exception as exc:
            raise KeyError(str(exc)) from exc

    # -- knowledge state (§20/§21) -----------------------------------------

    def coverage(self, entity_id: str | None = None,
                 name: str | None = None) -> KnowledgeStateView:
        def read(kb):
            target = entity_id
            if target is None and name:
                found = kb.search_entities_by_name(name)
                if len(found) == 1:
                    target = found[0].entity_id
            from crea.services.research_intelligence import (
                assess_coverage, find_gaps, synthesize_fields,
            )

            coverage = assess_coverage(kb, entity_id=target, target=name or "")
            gaps = find_gaps(coverage)
            synthesis = synthesize_fields(kb, entity_id=target) if target \
                else None
            return KnowledgeStateView(
                entity_id=target,
                facets=[FacetView(facet=f.facet.value, state=f.state.value,
                                  detail=f.detail,
                                  object_ids=list(f.object_ids))
                        for f in coverage.facets],
                gaps=[GapView(facet=g.facet.value, reason=g.reason,
                              suggested_action=g.suggested_action)
                      for g in gaps],
                conflicts=[self._conflict_view(c)
                           for c in (synthesis.conflicts if synthesis else [])])
        return self._with(read)

    # -- search (§99/§100: labelled EXACT / SCREENING) ----------------------

    def search(self, *, kind: str, name: str | None = None,
               entity_id: str | None = None, reaction_id: str | None = None,
               paper_id: str | None = None,
               structure: str | None = None) -> list[SearchResultView]:
        def read(kb):
            kind_norm = (kind or "").strip().lower()
            results: list[SearchResultView] = []
            if kind_norm == "entity":
                if name:
                    for entity in kb.search_entities_by_name(name):
                        results.append(SearchResultView(
                            kind="entity", id=entity.entity_id,
                            label=", ".join(entity.names) or entity.entity_id,
                            match="EXACT",
                            object={"canonical_smiles":
                                    entity.canonical_smiles}))
                if entity_id:
                    try:
                        entity = kb.get_entity(entity_id)
                        results.append(SearchResultView(
                            kind="entity", id=entity.entity_id,
                            label=entity.entity_id, match="EXACT"))
                    except Exception:  # noqa: BLE001 - miss is empty, not error
                        pass
            elif kind_norm == "reaction":
                found = []
                try:
                    found = kb.search_reactions(
                        entity_id=entity_id, paper_id=paper_id,
                        structure=structure,
                        structure_mode=("substructure" if structure
                                        else "exact"))
                except ValueError:
                    found = []
                for reaction in found:
                    results.append(SearchResultView(
                        kind="reaction", id=reaction.reaction_id,
                        label=reaction.status.value,
                        # a structure screen is screening, never a confirmed
                        # match (§99)
                        match=("SCREENING" if structure else "EXACT"),
                        object={"paper_id": reaction.paper_id,
                                "doi": reaction.doi}))
            elif kind_norm == "paper":
                if paper_id:
                    try:
                        paper = kb.get_paper(paper_id)
                        results.append(SearchResultView(
                            kind="paper", id=paper.paper_id,
                            label=paper.title or paper.paper_id))
                    except Exception:  # noqa: BLE001
                        pass
                elif entity_id:
                    for paper in kb.get_papers_for_entity(entity_id):
                        results.append(SearchResultView(
                            kind="paper", id=paper.paper_id,
                            label=paper.title or paper.paper_id))
            elif kind_norm == "evidence" and reaction_id:
                for item in kb.get_evidence_for_reaction(reaction_id):
                    results.append(SearchResultView(
                        kind="evidence", id=item.evidence_id,
                        label=item.raw_quote[:60]))
            else:
                raise ValueError(
                    "INVALID_QUERY: kind must be entity|reaction|paper|"
                    "evidence with at least one filter")
            return results
        return self._with(read)

    # -- export (§70 of 3.11) ----------------------------------------------

    def export(self) -> dict:
        return self._with(lambda kb: kb.export_dict())

    # -- view builders ------------------------------------------------------

    @staticmethod
    def _mention_view(mention) -> MentionView:
        return MentionView(
            mention_id=mention.mention_id, raw_text=mention.raw_text,
            source=mention.source, source_id=mention.source_id,
            resolution_status=mention.resolution_status.value,
            entity_id=mention.entity_id,
            context_before=mention.context_before,
            context_after=mention.context_after)

    @staticmethod
    def _evidence_view(item) -> EvidenceView:
        return EvidenceView(
            evidence_id=item.evidence_id,
            evidence_type=item.evidence_type.value,
            strength=item.strength.value,
            source=item.source, source_id=item.source_id,
            retrieved_at=item.retrieved_at, raw_quote=item.raw_quote,
            paper_id=item.paper_id, doi=item.doi,
            locator=item.locator.model_dump(mode="json") if item.locator else None,
            provenance=(item.provenance.model_dump(mode="json")
                        if item.provenance else None))

    @staticmethod
    def _reaction_view(reaction, kb) -> ReactionView:
        components = []
        for bucket in ("reactants", "products", "reagents", "catalysts",
                       "solvents", "unassigned"):
            for component in getattr(reaction, bucket):
                components.append(ReactionComponentView(
                    role=component.role.value, name=component.name,
                    entity_id=component.entity_id,
                    identity_key=component.identity_key,
                    canonical_smiles=component.canonical_smiles,
                    mention_id=(component.mention.mention_id
                                if component.mention is not None else None)))
        return ReactionView(
            reaction_id=reaction.reaction_id,
            reaction_key=reaction.reaction_key,
            status=reaction.status.value,
            record_id=reaction.record_id,
            paper_id=reaction.paper_id, doi=reaction.doi,
            source_id=reaction.source_id,
            document_id=reaction.document_id,
            components=components,
            evidence_ids=list(reaction.evidence_refs),
            unresolved_evidence_refs=list(reaction.unresolved_evidence_refs),
            notes=list(reaction.notes))

    @staticmethod
    def _conflict_view(conflict) -> ConflictView:
        return ConflictView(
            field=conflict.field, kind=conflict.kind, note=conflict.note,
            observations=[ConflictObservationView(
                value=o.value, raw_value=o.raw_value, unit=o.unit,
                raw_unit=o.raw_unit, evidence_id=o.evidence_id,
                paper_id=o.paper_id, doi=o.doi, reaction_id=o.reaction_id,
                record_id=o.record_id)
                for o in conflict.observations])

    @staticmethod
    def _session_view(session) -> SessionView:
        return SessionView(
            session_id=session.session_id,
            question=session.task.question,
            status=session.status.value,
            created_at=session.created_at,
            phases=list(session.phases),
            entity_ids=list(session.chemical_entity_ids),
            mention_ids=list(session.chemical_mention_ids),
            reaction_ids=list(session.reaction_ids),
            evidence_ids=list(session.evidence_ids),
            route_ids=list(session.route_ids),
            source_ids=[s.identifier for s in session.sources])


__all__ = ["KnowledgeApplication"]

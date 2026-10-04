"""ReactionRecord -> Reaction: the deterministic half of reaction knowledge.

Phase 3.9. The extraction policy already validated every field of a
`ReactionRecord`; the mention layer already decides what a name means. This
service joins the two into a `Reaction` -- and nothing else. It does not
re-extract anything with a model (§12: the record is the extraction; a
second pass would overwrite validated data with fresh guesses), it does not
guess a missing role into something plausible (§7: unknown stays unknown),
and it does not promote a status because a title looked suggestive (§11).

Where the pieces come from:

    ReactionRecord           what the procedure text said (policy-validated)
    ChemicalMentionResolver  what each name means (Phase 3.8.2)
    held Evidence            what the record's references resolve to
    Paper / Document scope   where this was reported

Status is computed, never chosen (§10): PARTIAL when parts are unresolvable,
EXPERIMENTALLY_SUPPORTED only on an experimental rung with everything
established, EVIDENCE_BACKED only on quote-level evidence, STRUCTURED for a
validated record with nothing behind it, CANDIDATE for a bare proposal.
Metadata that mentions a reaction never moves these rungs (§11).

Storage is a repository protocol with an in-memory implementation (§58):
swap in SQLite later without touching this file. Deduplication (§21) merges
only reactions that are provably the same report of the same chemistry --
same paper scope, same identity-based key -- and keeps both whenever any
part is name-based or the papers differ. Two kept reactions beat one wrong
merge.
"""

from __future__ import annotations

from crea.core.naming import normalize_for_search

from typing import Iterable, Protocol

from crea.core.ids import content_hash
from crea.core.models.entity import ChemicalMention
from crea.core.models.reaction import ComponentRole, ReactionRecord
from crea.core.models.reaction_linking import ReactionEvidenceLevel
from crea.core.models.reaction_knowledge import (
    REACTION_KEY_VERSION,
    Reaction,
    ReactionComponent,
    ReactionStatus,
    key_is_identity_based,
    reaction_key,
)
from crea.core.provenance import Provenance

#: Experimental rungs: the only ones that may make a reaction
#: experimentally supported (§17 -- the existing ladder, reused verbatim).
_EXPERIMENTAL_RUNGS = (
    ReactionEvidenceLevel.EXPERIMENTAL_REACTION_SUPPORT,
    ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT,
)

_RUNG_ORDER = {
    ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE: 0,
    ReactionEvidenceLevel.LITERATURE_DISCOVERY: 1,
    ReactionEvidenceLevel.LITERATURE_REACTION_SUPPORT: 2,
    ReactionEvidenceLevel.EXPERIMENTAL_REACTION_SUPPORT: 3,
    ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT: 3,
}

#: Roles that sit in their own bucket on a Reaction. Ligand/base/additive
#: keep the role the source gave them and live in `unassigned` -- the
#: Reaction model has no ligand bucket, and inventing one mid-phase would be
#: a schema change chasing a table layout.
_BUCKET_ROLES = (ComponentRole.REACTANT, ComponentRole.REAGENT,
                 ComponentRole.CATALYST, ComponentRole.SOLVENT)


def evidence_level_for(evidence_items: Iterable) -> ReactionEvidenceLevel:
    """Where the held evidence puts this reaction on the existing ladder.

    Reuses `ReactionEvidenceLevel` (§17) rather than inventing a strength
    system: user-document experimental evidence sits on its own rung,
    primary/quoted experimental text on the experimental rung, a full-text
    quote on the literature rung, and metadata/abstract -- a title saying a
    reaction exists -- stops at discovery (§11). Nothing gets promoted by
    being mentioned.
    """
    items = list(evidence_items)
    if not items:
        return ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE

    experimental_types = {"experimental", "user_provided_document"}
    quote_types = {"fulltext", "experimental", "user_provided_document"}
    level = ReactionEvidenceLevel.RETROSYNTHESIS_CANDIDATE

    for item in items:
        etype = getattr(getattr(item, "evidence_type", None), "value", "") or ""
        source = (getattr(item, "source", "") or "").lower()
        strength = getattr(getattr(item, "strength", None), "value", "") or ""
        if etype in experimental_types or strength in (
                "primary_document_evidence", "user_provided_evidence"):
            rung = (ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT
                    if source == "user_provided"
                    else ReactionEvidenceLevel.EXPERIMENTAL_REACTION_SUPPORT)
        elif etype in quote_types:
            rung = ReactionEvidenceLevel.LITERATURE_REACTION_SUPPORT
        elif etype in ("metadata", "abstract", "structured_database"):
            rung = ReactionEvidenceLevel.LITERATURE_DISCOVERY
        else:
            continue
        if _RUNG_ORDER[rung] > _RUNG_ORDER[level]:
            level = rung
    return level


def evaluate_status(*, record_validated: bool,
                    evidence_level: ReactionEvidenceLevel,
                    gaps: list[str]) -> ReactionStatus:
    """The status a reaction earned. Computed from facts, never assumed.

    Precedence is deliberate (§10/§62): gaps first -- a reaction with
    unresolvable parts is PARTIAL whatever else stands behind it, because
    pretending completeness is the failure this phase exists to prevent.
    Then the evidence ladder. Metadata alone earns nothing (§11).
    """
    if gaps:
        return ReactionStatus.PARTIAL
    if evidence_level in _EXPERIMENTAL_RUNGS:
        return ReactionStatus.EXPERIMENTALLY_SUPPORTED
    if evidence_level is ReactionEvidenceLevel.LITERATURE_REACTION_SUPPORT:
        return ReactionStatus.EVIDENCE_BACKED
    return ReactionStatus.STRUCTURED if record_validated else ReactionStatus.CANDIDATE


def product_mentions_from_document(record, document_text: str,
                                   resolver, *, source: str = "",
                                   source_id: str = ""):
    """The document's occurrences of this record's product names.

    A product name like `compound 8` is paper-local; its meaning lives in
    the surrounding text (`Compound 8 (InChI=...)`). So the mentions handed
    to the builder are the ones the document actually contains -- context
    included -- and resolution still runs the identity layer on each (§9/§13).
    """
    from crea.services.mention_resolution import extract_chemical_mentions

    if not document_text:
        return []
    product_names = {normalize_for_search(item.name)
                     for item in record.conditions.products}
    if not product_names:
        return []
    known = []
    for item in (record.conditions.reactants + record.conditions.products
                 + record.conditions.reagents + record.conditions.catalysts
                 + record.conditions.ligands + record.conditions.solvents
                 + record.conditions.unassigned):
        if item.name:
            known.append(item.name)
    mentions = extract_chemical_mentions(
        document_text, known_names=known, source=source, source_id=source_id)
    return [m for m in mentions
            if (m.normalized_text or "") in product_names]


class ReactionBuilder:
    """Joins a validated record and its mention resolutions into a Reaction."""

    def __init__(self, mention_resolver, *, entity_registry=None,
                 held_evidence: Iterable | None = None) -> None:
        self._resolver = mention_resolver
        self._entities = entity_registry
        self._held_map = (
            {getattr(e, "evidence_id", e): e for e in held_evidence}
            if held_evidence is not None else None)

    def build(self, record: ReactionRecord, *, paper_id: str = "", doi: str = "",
              source_id: str = "", product_mentions=()) -> Reaction:
        # -- components: the record's roles, the mention layer's identities
        components = self._components_from_record(record, source_id or paper_id)
        products = [c for c in components if c.role is ComponentRole.PRODUCT]
        components = [c for c in components if c.role is not ComponentRole.PRODUCT]
        for mention in product_mentions or ():
            resolution = self._resolver.resolve(mention)
            component = self._component(
                ComponentRole.PRODUCT, resolution.mention.raw_text, resolution)
            # the document occurrence of a name carries its context and is
            # the same component the record named -- replace, never duplicate
            name_key = normalize_for_search(component.name)
            replaced = False
            for index, existing in enumerate(products):
                if normalize_for_search(existing.name) == name_key:
                    products[index] = component
                    replaced = True
                    break
            if not replaced:
                products.append(component)

        gaps = self._gaps(components, products, paper_id, record)
        by_role: dict[ComponentRole, list[ReactionComponent]] = {}
        for component in components:
            by_role.setdefault(component.role, []).append(component)

        # -- evidence: what the references resolve to, and what they do not
        refs, unresolved_refs = self._evidence_refs(record, components, products)
        level = evidence_level_for(self._held_items(refs))
        if unresolved_refs:
            gaps.append("evidence references that do not resolve: "
                        + ", ".join(unresolved_refs))

        # -- the deduplication key: identity materials, role-grouped and
        #    sorted inside; the order of addition never forks a reaction (§22)
        leftovers = [c for role, cs in by_role.items()
                     if role not in _BUCKET_ROLES for c in cs]
        key = reaction_key(
            [c.key_material() for c in by_role.get(ComponentRole.REACTANT, [])],
            [c.key_material() for c in products],
            [c.key_material() for c in by_role.get(ComponentRole.REAGENT, [])
             + by_role.get(ComponentRole.CATALYST, [])
             + by_role.get(ComponentRole.SOLVENT, []) + leftovers],
        )
        scope = (doi or paper_id or source_id
                 or record.document_id or record.reaction_id)
        reaction_id = "RXN-" + content_hash(
            f"{record.reaction_id}|{scope}|{key}", 16)

        status = evaluate_status(record_validated=True,
                                 evidence_level=level, gaps=gaps)
        return Reaction(
            reaction_id=reaction_id,
            reaction_key=key,
            reactants=by_role.get(ComponentRole.REACTANT, []),
            products=products,
            reagents=by_role.get(ComponentRole.REAGENT, []),
            catalysts=by_role.get(ComponentRole.CATALYST, []),
            solvents=by_role.get(ComponentRole.SOLVENT, []),
            unassigned=leftovers,
            status=status,
            record_id=record.reaction_id,
            paper_id=paper_id,
            doi=doi,
            source_id=source_id,
            document_id=record.document_id or "",
            evidence_refs=refs,
            unresolved_evidence_refs=unresolved_refs,
            locator=self._locator_of(refs),
            provenance=Provenance(
                tool="reaction-builder", tool_version=REACTION_KEY_VERSION,
                source=source_id or record.source,
                source_id=record.reaction_id),
            notes=gaps,
        )

    # -- pieces -------------------------------------------------------------

    def _components_from_record(self, record, scope: str) -> list[ReactionComponent]:
        conditions = record.conditions
        out = []
        for items, role in ((conditions.reactants, ComponentRole.REACTANT),
                            (conditions.products, ComponentRole.PRODUCT),
                            (conditions.reagents, ComponentRole.REAGENT),
                            (conditions.catalysts, ComponentRole.CATALYST),
                            (conditions.ligands, ComponentRole.LIGAND),
                            (conditions.solvents, ComponentRole.SOLVENT),
                            (conditions.unassigned, ComponentRole.UNKNOWN)):
            for item in items:
                # the record's role stands: a substance whose job the source
                # never said keeps UNKNOWN (§7)
                mention = ChemicalMention(
                    mention_id=content_hash(
                        f"{record.reaction_id}|{role.value}|{item.name}", 16),
                    raw_text=item.name,
                    normalized_text=normalize_for_search(item.name),
                    source=record.source, source_id=scope,
                    evidence_refs=list(item.evidence_refs or []))
                resolution = self._resolver.resolve(mention)
                # the record's role *list* is the role statement; a
                # Component's own role field defaults to UNKNOWN and cannot
                # distinguish an explicit unknown from an unset one
                out.append(self._component(role, item.name, resolution))
        return out

    def _component(self, role, name, resolution) -> ReactionComponent:
        entity_id = resolution.entity_id
        smiles = ""
        if resolution.mention is not None and resolution.mention.explicit_smiles:
            smiles = resolution.mention.explicit_smiles
        if entity_id and self._entities is not None:
            smiles = self._entities.get(entity_id).canonical_smiles or smiles
        return ReactionComponent(
            role=role, name=name, mention=resolution.mention,
            entity_id=entity_id,
            identity_key=resolution.evidence.identity_key,
            canonical_smiles=smiles)

    def _gaps(self, components, products, paper_id, record) -> list[str]:
        gaps = []
        for component in components + products:
            mention = component.mention
            if mention is None or mention.entity_id is None:
                status = mention.resolution_status.value if mention else "no mention"
                gaps.append(
                    f"component {component.name!r} resolved to no entity ({status})")
        if not products:
            gaps.append("no product was identified in the source")
        if not paper_id and not (record.document_id or ""):
            gaps.append("no paper and no document to point at")
        return gaps

    def _evidence_refs(self, record, components, products):
        refs: list[str] = []
        for source in (list(record.evidence_refs),
                       *[c.mention.evidence_refs for c in components + products
                         if c.mention is not None]):
            for ref in source or []:
                if ref not in refs:
                    refs.append(ref)
        if self._held_map is None:
            return refs, []
        kept = [r for r in refs if r in self._held_map]
        return kept, [r for r in refs if r not in self._held_map]

    def _held_items(self, refs):
        if self._held_map is None:
            return []
        return [self._held_map[r] for r in refs if r in self._held_map]

    def _locator_of(self, refs):
        for item in self._held_items(refs):
            locator = getattr(item, "locator", None)
            if locator is not None:
                return locator
        return None


class ReactionRepository(Protocol):
    """What the registry needs from storage. The future SQLite swap point (§58)."""

    def get(self, reaction_id: str) -> Reaction | None: ...
    def add(self, reaction: Reaction) -> None: ...
    def find_by_scope_key(self, paper_scope: str, reaction_key: str) -> Reaction | None: ...
    def find_by_paper(self, paper_id: str) -> list[Reaction]: ...
    def find_by_doi(self, doi: str) -> list[Reaction]: ...
    def find_by_component_entity(self, entity_id: str) -> list[Reaction]: ...
    def find_by_product(self, entity_id: str) -> list[Reaction]: ...
    def find_by_reactant(self, entity_id: str) -> list[Reaction]: ...
    def find_between(self, reactant_ids: list[str],
                     product_ids: list[str]) -> list[Reaction]: ...
    def all(self) -> list[Reaction]: ...


class InMemoryReactionRepository:
    """Everything in dicts. For tests and offline runs; gone with the process."""

    def __init__(self) -> None:
        self._by_id: dict[str, Reaction] = {}
        self._by_scope_key: dict[tuple[str, str], Reaction] = {}
        self._by_paper: dict[str, list[Reaction]] = {}
        self._by_doi: dict[str, list[Reaction]] = {}
        self._by_entity: dict[str, list[Reaction]] = {}

    def get(self, reaction_id: str) -> Reaction | None:
        return self._by_id.get(reaction_id)

    def add(self, reaction: Reaction) -> None:
        # Idempotent on purpose (Phase 3.11): `add` means "store this
        # reaction's state", and the registry re-issues it after a merge.
        # Index entries are appended once per reaction, so a re-add can
        # never double a paper's or an entity's reaction list.
        is_new = reaction.reaction_id not in self._by_id
        self._by_id[reaction.reaction_id] = reaction
        self._by_scope_key.setdefault(
            (reaction.paper_scope(), reaction.reaction_key), reaction)
        if not is_new:
            return
        if reaction.paper_id:
            self._by_paper.setdefault(reaction.paper_id, []).append(reaction)
        if reaction.doi:
            self._by_doi.setdefault(reaction.doi, []).append(reaction)
        for component in reaction.components():
            if component.entity_id:
                self._by_entity.setdefault(component.entity_id, []).append(reaction)

    def find_by_scope_key(self, paper_scope: str, reaction_key: str) -> Reaction | None:
        return self._by_scope_key.get((paper_scope, reaction_key))

    def find_by_paper(self, paper_id: str) -> list[Reaction]:
        return list(self._by_paper.get(paper_id, []))

    def find_by_doi(self, doi: str) -> list[Reaction]:
        return list(self._by_doi.get(doi, []))

    def find_by_component_entity(self, entity_id: str) -> list[Reaction]:
        return list(self._by_entity.get(entity_id, []))

    def find_by_product(self, entity_id: str) -> list[Reaction]:
        return [r for r in self._by_id.values()
                if entity_id in {c.entity_id for c in r.products}]

    def find_by_reactant(self, entity_id: str) -> list[Reaction]:
        return [r for r in self._by_id.values()
                if entity_id in {c.entity_id for c in r.reactants}]

    def find_between(self, reactant_ids: list[str],
                     product_ids: list[str]) -> list[Reaction]:
        out = []
        for reaction in self._by_id.values():
            reactants = {c.entity_id for c in reaction.reactants}
            products = {c.entity_id for c in reaction.products}
            if set(reactant_ids) <= reactants and set(product_ids) <= products:
                out.append(reaction)
        return out

    def all(self) -> list[Reaction]:
        return list(self._by_id.values())


class ReactionRegistry:
    """Remembers reactions; merges only what is provably the same (§21)."""

    def __init__(self, repository: ReactionRepository | None = None) -> None:
        self._repository = repository or InMemoryReactionRepository()

    def register(self, reaction: Reaction) -> tuple[Reaction, bool]:
        """Returns the kept reaction and whether an existing one was enriched.

        The merge rule is the conservative one (§21): same paper scope, same
        reaction key, and a key built purely from identity keys. A name-based
        key, or the same chemistry in a different paper, keeps two reactions.

        A lost race to a concurrent writer (Phase 4 §38) re-reads and
        merges instead of erroring or double-keeping.
        """
        from crea.repositories.errors import Duplicate

        for _attempt in range(5):
            # the same report registered twice is the same reaction: identical
            # ids mean identical record + scope + key
            by_id = self._repository.get(reaction.reaction_id)
            if by_id is not None:
                return self._merge(by_id, reaction), True
            if not key_is_identity_based(reaction.reaction_key):
                try:
                    self._repository.add(reaction)
                except Duplicate:
                    continue
                return reaction, False
            existing = self._repository.find_by_scope_key(
                reaction.paper_scope(), reaction.reaction_key)
            if existing is None:
                try:
                    self._repository.add(reaction)
                except Duplicate:
                    continue
                return reaction, False
            return self._merge(existing, reaction), True
        raise Duplicate(
            f"could not register reaction {reaction.reaction_id} after "
            f"concurrent updates")

    def _merge(self, keep: Reaction, incoming: Reaction) -> Reaction:
        for ref in incoming.evidence_refs:
            if ref not in keep.evidence_refs:
                keep.evidence_refs.append(ref)
        for ref in incoming.unresolved_evidence_refs:
            if ref not in keep.unresolved_evidence_refs:
                keep.unresolved_evidence_refs.append(ref)
        for note in incoming.notes:
            if note not in keep.notes:
                keep.notes.append(note)
        if (keep.status is ReactionStatus.CANDIDATE
                and incoming.status is not ReactionStatus.CANDIDATE):
            keep.status = incoming.status
        # Persisted repositories (Phase 3.11) hold a copy of a reaction's
        # state; `add` is the upsert that stores the merge above. The
        # in-memory one holds this very object and re-adding it is a no-op
        # by construction.
        self._repository.add(keep)
        return keep

    def get(self, reaction_id: str) -> Reaction:
        reaction = self._repository.get(reaction_id)
        if reaction is None:
            raise KeyError(f"no reaction {reaction_id!r} is registered")
        return reaction

    def find_by_paper(self, paper_id: str) -> list[Reaction]:
        return self._repository.find_by_paper(paper_id)

    def find_by_doi(self, doi: str) -> list[Reaction]:
        return self._repository.find_by_doi(doi)

    def find_by_component_entity(self, entity_id: str) -> list[Reaction]:
        return self._repository.find_by_component_entity(entity_id)

    def find_by_product(self, entity_id: str) -> list[Reaction]:
        """Reactions that make this entity (Stage D). Deterministic order."""
        return self._repository.find_by_product(entity_id)

    def find_by_reactant(self, entity_id: str) -> list[Reaction]:
        """Reactions that consume this entity (Stage D)."""
        return self._repository.find_by_reactant(entity_id)

    def find_between(self, reactant_ids: list[str],
                     product_ids: list[str]) -> list[Reaction]:
        """Reactions that turn these reactants into these products.

        Subset semantics: every named reactant is consumed and every named
        product is made. `A + B -> C` answers `find_between([A, B], [C])`
        and also `find_between([A], [C])`; the co-occurrence -- A and B in
        ONE reaction -- is never decomposed into two edges (§4).
        """
        return self._repository.find_between(
            [r for r in reactant_ids if r], [p for p in product_ids if p])

    def search_results(self, *, entity_id: str | None = None,
                       product_entity_id: str | None = None,
                       reactant_entity_id: str | None = None,
                       reactant_entity_ids: list[str] | None = None,
                       paper_id: str | None = None, doi: str | None = None,
                       reaction_key: str | None = None,
                       structure: str | None = None,
                       structure_mode: str = "exact",
                       evidence_lookup=None) -> list:
        """Search with reasons attached (Stage D §13).

        Every result says which entities the query matched and in which
        role, carries the reaction's evidence and provenance, and lists
        diagnostics for what is missing (no atom mapping, unresolved
        components). `evidence_lookup(evidence_id) -> dict | None` lets a
        caller with a knowledge base resolve the citations; without one
        the ids still travel.
        """
        from crea.core.models.reaction_knowledge import ReactionSearchResult

        found = self.search(
            entity_id=entity_id, product_entity_id=product_entity_id,
            reactant_entity_ids=([reactant_entity_id]
                                 if reactant_entity_id else None)
            or reactant_entity_ids,
            paper_id=paper_id, doi=doi, reaction_key=reaction_key,
            structure=structure, structure_mode=structure_mode)
        results = []
        for reaction in found:
            product_ids = {c.entity_id for c in reaction.products
                           if c.entity_id}
            reactant_ids = {c.entity_id for c in reaction.reactants
                            if c.entity_id}
            if product_entity_id and product_entity_id in product_ids:
                match_role = "product"
                matched = [product_entity_id]
            elif reactant_entity_id and reactant_entity_id in reactant_ids:
                match_role = "reactant"
                matched = [reactant_entity_id]
            elif entity_id:
                match_role = "component"
                matched = [entity_id]
            else:
                match_role = "filter"
                matched = sorted(i for i in product_ids | reactant_ids if i)
            evidence = []
            for ref in reaction.evidence_refs:
                resolved = evidence_lookup(ref) if evidence_lookup else None
                evidence.append(resolved or {"evidence_id": ref})
            diagnostics = []
            if reaction.atom_mapping is None:
                diagnostics.append("atom mapping not performed")
            if any(c.entity_id is None for c in reaction.components()):
                diagnostics.append("some components are unresolved")
            results.append(ReactionSearchResult(
                reaction=reaction,
                matched_entities=matched,
                match_role=match_role,
                evidence=evidence,
                provenance=(reaction.provenance.model_dump(mode="json")
                            if reaction.provenance else None),
                diagnostics=diagnostics))
        return results

    def search(self, *, entity_id: str | None = None,
               product_entity_id: str | None = None,
               reactant_entity_ids: list[str] | None = None,
               paper_id: str | None = None, doi: str | None = None,
               reaction_key: str | None = None,
               structure: str | None = None,
               structure_mode: str = "exact") -> list[Reaction]:
        """Deterministic filtered search (§34/§60). At least one filter.

        `structure` is matched against component structures: `exact` means
        an established structure equal to the query; `substructure` asks the
        chemistry layer whether the query sits inside a component (§33).
        No similarity, no ranking, no default-everything: an unfiltered
        query is `INVALID_QUERY` rather than a full table dump.
        """
        filters = (entity_id, product_entity_id, reactant_entity_ids,
                   paper_id, doi, reaction_key, structure)
        if not any(value for value in filters):
            raise ValueError(
                "INVALID_QUERY: at least one filter is required "
                "(entity_id, product_entity_id, reactant_entity_ids, "
                "paper_id, doi, reaction_key, structure)")
        if structure_mode not in ("exact", "substructure"):
            raise ValueError(
                f"INVALID_QUERY: unknown structure_mode {structure_mode!r}")

        wanted_entities = set(reactant_entity_ids or [])
        results = []
        for reaction in self._repository.all():
            if entity_id is not None and entity_id not in {
                    c.entity_id for c in reaction.components()}:
                continue
            if product_entity_id is not None and product_entity_id not in {
                    c.entity_id for c in reaction.products}:
                continue
            if wanted_entities and not wanted_entities <= {
                    c.entity_id for c in reaction.reactants}:
                continue
            if paper_id is not None and reaction.paper_id != paper_id:
                continue
            if doi is not None and reaction.doi != doi:
                continue
            if reaction_key is not None and reaction.reaction_key != reaction_key:
                continue
            if structure is not None and not self._matches_structure(
                    reaction, structure, structure_mode):
                continue
            results.append(reaction)
        return results

    @staticmethod
    def _matches_structure(reaction: Reaction, structure: str,
                           mode: str) -> bool:
        """Structure filter, through the chemistry abstraction (§33)."""
        from crea.chem.align import substructure_contains
        from crea.chem.normalize import normalize

        try:
            query = normalize(structure)
        except Exception:  # noqa: BLE001 - an unparseable query matches nothing
            return False
        for component in reaction.components():
            if not component.canonical_smiles:
                continue
            if mode == "exact":
                if component.identity_key and query.inchi and (
                        component.identity_key
                        == f"{REACTION_KEY_VERSION}:{query.inchi}"):
                    return True
                if component.canonical_smiles == query.canonical_smiles:
                    return True
            else:
                try:
                    if substructure_contains(normalize(component.canonical_smiles),
                                             query):
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def all(self) -> list[Reaction]:
        return self._repository.all()

    def __len__(self) -> int:
        return len(self._repository.all())


__all__ = [
    "InMemoryReactionRepository",
    "ReactionBuilder",
    "ReactionRegistry",
    "ReactionRepository",
    "evaluate_status",
    "evidence_level_for",
]

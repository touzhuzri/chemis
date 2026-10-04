"""Chemical entity registry: one stable id per established identity.

Phase 3.8.1. The registry sits on top of the Phase 3.6 identity layer and
does exactly one thing it cannot do itself: remember. An identity resolution
decides *what* a structure is; this records that a given identity was
established, gives it a durable internal id (`CHEM-00000001`), and returns
the same id when the same identity comes back.

The rules, in order of how often they matter:

* **Only `RESOLVED` registers.** AMBIGUOUS keeps its candidates, UNAVAILABLE
  keeps its silence, NOT_FOUND keeps its absence. None of them become an
  entity -- a name never turns into a structure here.
* **One identity key, one entity.** Re-registering an established identity
  returns the existing entity (and merges any new names or external
  identifiers into it) instead of minting `CHEM-00000003` for a compound that
  already has an id.
* **Names are labels.** They attach to an entity and never create one.
* **External ids point outward.** A PubChem CID is recorded as an
  `ExternalIdentifier` of the entity; it is never the entity id.

Persistence is a `ChemicalEntityRepository` behind the registry, with an
in-memory implementation now. Swapping in SQLite/PostgreSQL later means
implementing that protocol, not touching this class.

No RDKit, no network, no provider import: the registry consumes
`ChemicalIdentityResolution` objects the identity service already produced.
"""

from __future__ import annotations

from typing import Protocol

from crea.core.models.entity import (
    IDENTITY_KEY_VERSION,
    ChemicalEntity,
    EntityNotFound,
    EntityRegistrationError,
    ExternalIdentifier,
    format_entity_id,
    identity_key,
    identity_key_of,
)
from crea.core.models.identity import (
    ChemicalIdentityResolution,
    IdentityBasis,
    StructureInputKind,
    StructureResolutionStatus,
)
from crea.core.provenance import Provenance

#: Strongest first. A merge may raise an entity's basis to a stronger
#: established claim and never lower it -- the same rule the identity layer's
#: facet `basis()` follows: the strongest claim the evidence supports.
_BASIS_RANK = {
    IdentityBasis.UNKNOWN: 0,
    IdentityBasis.PROVIDER_IDENTITY: 1,
    IdentityBasis.SAME_CONNECTIVITY: 2,
    IdentityBasis.SAME_STEREO: 3,
    IdentityBasis.EXACT_STRUCTURE: 4,
}

_PUBCHEM_NAMESPACE = "pubchem"


class ChemicalEntityRepository(Protocol):
    """What the registry needs from storage. The future swap point:

    `InMemoryChemicalEntityRepository` -> `SQLite...` -> `PostgreSQL...`
    """

    def get(self, entity_id: str) -> ChemicalEntity | None: ...

    def add(self, entity: ChemicalEntity) -> None: ...

    def find_by_identity_key(self, key: str) -> ChemicalEntity | None: ...

    def find_by_external_id(self, namespace: str,
                            identifier: str) -> ChemicalEntity | None: ...

    def all(self) -> list[ChemicalEntity]: ...


class InMemoryChemicalEntityRepository:
    """Everything in dicts. For tests and offline runs; gone with the process."""

    def __init__(self) -> None:
        self._by_id: dict[str, ChemicalEntity] = {}
        self._by_key: dict[str, ChemicalEntity] = {}
        self._by_external: dict[tuple[str, str], ChemicalEntity] = {}

    def get(self, entity_id: str) -> ChemicalEntity | None:
        return self._by_id.get(entity_id)

    def add(self, entity: ChemicalEntity) -> None:
        self._by_id[entity.entity_id] = entity
        self._by_key.setdefault(entity.identity_key, entity)
        for identifier in entity.external_identifiers:
            self._by_external.setdefault(identifier.key(), entity)

    def find_by_identity_key(self, key: str) -> ChemicalEntity | None:
        return self._by_key.get(key)

    def find_by_external_id(self, namespace: str,
                            identifier: str) -> ChemicalEntity | None:
        return self._by_external.get((namespace, identifier))

    def all(self) -> list[ChemicalEntity]:
        return list(self._by_id.values())

    def index_external(self, entity: ChemicalEntity) -> None:
        for identifier in entity.external_identifiers:
            self._by_external.setdefault(identifier.key(), entity)


class ChemicalEntityRegistry:
    """Allocates and looks up chemical entities. See the module docstring."""

    def __init__(self, repository: ChemicalEntityRepository | None = None) -> None:
        self._repository = repository or InMemoryChemicalEntityRepository()
        self._allocated = 0

    # -- registration -------------------------------------------------------

    def register_resolution(self,
                            resolution: ChemicalIdentityResolution) -> ChemicalEntity:
        """Turn an established identity into an entity, or refuse loudly.

        The refusal is the point of half this method: anything short of
        `RESOLVED` raises rather than guessing, and a resolved candidate with
        no standard InChI raises rather than keying the entity on a name.
        """
        selected = self._established(resolution)
        key = self._key_of(selected, resolution)

        # Concurrent writers (Phase 4 §38) may win the race for one identity
        # or one id; a lost race is re-read and merged, never an error and
        # never a second entity for one identity.
        from crea.repositories.errors import Duplicate

        for _attempt in range(5):
            existing = self._repository.find_by_identity_key(key)
            if existing is not None:
                self._merge(existing, selected, resolution)
                # A repository that persists (Phase 3.11) holds its own copy of
                # an entity's state; the in-memory one holds this very object.
                # Telling it about the merged state is what makes names and
                # external identifiers survive a restart -- `add` is an upsert
                # of that state, for both implementations.
                self._repository.add(existing)
                return existing

            entity = ChemicalEntity(
                entity_id=self._allocate_entity_id(),
                identity_key=key,
                identity_basis=selected.identity_basis,
                facets=selected.facets,
                canonical_smiles=selected.canonical_smiles,
                inchi=selected.inchi,
                inchikey=selected.inchikey,
                molecular_formula=selected.molecular_formula,
                molecular_weight=selected.molecular_weight,
                identity_source=selected.source,
                source_id=selected.source_id,
                source_url=selected.source_url,
                retrieved_at=selected.retrieved_at,
                # The existing provenance vocabulary, not a second one: which
                # source answered, which record of theirs, and how this came to
                # be recorded at all. When the structure came from an image
                # reading, the image travels too (§36): a reader must be able
                # to answer "这个结构是从哪张图识别出来的？".
                provenance=Provenance(
                    tool="chemical-entity-registry",
                    tool_version=IDENTITY_KEY_VERSION,
                    source=selected.source or None,
                    source_id=selected.source_id or None,
                    params=self._image_params(resolution),
                ),
            )
            for name in (selected.input_name, selected.candidate_name,
                         *selected.synonyms):
                entity.add_name(name)
            for identifier in self._external_identifiers(resolution, selected):
                entity.add_external_identifier(identifier)
            try:
                self._repository.add(entity)
            except Duplicate:
                continue
            return entity
        raise EntityRegistrationError(
            "could not register this identity after concurrent updates",
            status=resolution.status, code="CONCURRENT_UPDATE")

    def _allocate_entity_id(self) -> str:
        """The next free `CHEM-` id, counting what storage already holds.

        A persistent repository outlives this counter: reopening a database
        that already holds `CHEM-00000001` must mint `CHEM-00000002` for a
        new identity, never collide with -- or quietly reuse -- the id of an
        entity that already exists (§12: `entity_id` stays the business id;
        the database is not the allocator). Registration order remains the
        story of the number.
        """
        taken = {e.entity_id for e in self._repository.all()}
        index = self._allocated
        while True:
            index += 1
            candidate = format_entity_id(index)
            if candidate not in taken:
                self._allocated = index
                return candidate

    def _established(self, resolution: ChemicalIdentityResolution):
        """The one candidate an established resolution stands on -- or a refusal.

        Never `candidates[0]`: the list is not a ranking, and a resolution
        that reached RESOLVED says which candidate it selected.
        """
        if resolution.status is not StructureResolutionStatus.RESOLVED:
            raise EntityRegistrationError(
                f"identity is {resolution.status.value}, not resolved; "
                f"{resolution.reason or 'nothing was established'}",
                status=resolution.status, code="NOT_RESOLVED")
        if resolution.selected is None:
            raise EntityRegistrationError(
                "identity is resolved but selected no candidate; nothing to register",
                status=resolution.status, code="NOT_RESOLVED")
        return resolution.selected

    def _key_of(self, candidate, resolution: ChemicalIdentityResolution) -> str:
        try:
            return identity_key_of(candidate)
        except ValueError as exc:
            raise EntityRegistrationError(
                f"cannot key this identity: {exc}",
                status=resolution.status, code="NO_IDENTITY_KEY") from exc

    def _image_params(self, resolution) -> dict:
        """Where a recognized structure came from, when it was an image (§36).

        `ImageProvenance` already records the recognizer and the file
        reference; this copies that answer onto the entity's provenance so
        the question survives registration. An empty dict when the
        structure did not come from an image -- no placeholder.
        """
        image = getattr(resolution.input, "image", None)
        if image is None:
            return {}
        return {"image": {
            "recognizer": image.recognizer,
            "recognizer_version": image.recognizer_version,
            "model_version": image.model_version,
            "file_reference": image.file_reference,
            "raw_smiles": image.raw_smiles,
            "normalized_smiles": image.normalized_smiles,
            "source": image.source,
        }}

    def _external_identifiers(self, resolution, selected) -> list[ExternalIdentifier]:
        """Outward pointers only. A CID is recorded as a CID, never as an id."""
        found: list[ExternalIdentifier] = []
        if resolution.input.kind is StructureInputKind.PUBCHEM_CID:
            found.append(ExternalIdentifier(
                namespace=_PUBCHEM_NAMESPACE, identifier=resolution.input.value,
                source="user_input"))
        if selected.source.upper() == "PUBCHEM" and selected.source_id:
            found.append(ExternalIdentifier(
                namespace=_PUBCHEM_NAMESPACE, identifier=selected.source_id,
                source=selected.source))
        return found

    def _merge(self, entity: ChemicalEntity, selected,
               resolution: ChemicalIdentityResolution) -> None:
        """A second look at an established identity enriches, never duplicates."""
        for name in (selected.input_name, selected.candidate_name, *selected.synonyms):
            entity.add_name(name)
        for identifier in self._external_identifiers(resolution, selected):
            if entity.add_external_identifier(identifier):
                self._repository.index_external(entity)
        if _BASIS_RANK[selected.identity_basis] > _BASIS_RANK[entity.identity_basis]:
            entity.notes.append(
                f"identity basis raised from {entity.identity_basis.value} to "
                f"{selected.identity_basis.value} by a later registration")
            entity.identity_basis = selected.identity_basis
        # provenance accumulates (§91): an image reading the entity did not
        # record yet answers "从哪张图识别出来的" (§36) and is kept
        image_params = self._image_params(resolution)
        if image_params and entity.provenance is not None and (
                "image" not in (entity.provenance.params or {})):
            entity.provenance.params = {
                **(entity.provenance.params or {}), **image_params}

    # -- lookup -------------------------------------------------------------

    def get(self, entity_id: str) -> ChemicalEntity:
        entity = self._repository.get(entity_id)
        if entity is None:
            raise EntityNotFound(f"no entity {entity_id!r} is registered")
        return entity

    def find_by_identity(self, inchi: str) -> ChemicalEntity | None:
        """The entity whose identity key this established structure earns."""
        return self._repository.find_by_identity_key(identity_key(inchi))

    def find_by_external_id(self, namespace: str,
                            identifier: str) -> ChemicalEntity | None:
        return self._repository.find_by_external_id(namespace, identifier)

    def add_name(self, entity_id: str, name: str) -> ChemicalEntity:
        """Attach a label to a known entity. Names never create entities."""
        entity = self.get(entity_id)
        entity.add_name(name)
        return entity

    def add_external_identifier(self, entity_id: str,
                                identifier: ExternalIdentifier) -> ChemicalEntity:
        entity = self.get(entity_id)
        if entity.add_external_identifier(identifier):
            self._repository.index_external(entity)
        return entity

    def __len__(self) -> int:
        return len(self._repository.all())

    @property
    def entity_ids(self) -> list[str]:
        return [e.entity_id for e in self._repository.all()]


__all__ = [
    "ChemicalEntityRegistry",
    "ChemicalEntityRepository",
    "InMemoryChemicalEntityRepository",
]

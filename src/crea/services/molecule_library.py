"""The local molecule library (CREA Stage 2.2).

A library of molecule records, keyed by standard chemical identity. The
record is the existing `ChemicalEntity` -- `entity_id` is the internal
record id, `identity_key` is the versioned Standard InChI, `inchikey` the
Standard InChIKey, and external ids live in `external_identifiers` (§2-§4).
No second molecule identity model is created.

Duplicate prevention is by identity: adding the same structure twice returns
the existing record (§9). Search (similarity / substructure) reuses the
existing chemistry layer and runs over a `MoleculeProvider` source, so a
future external provider plugs in behind the same seam (§12/§13).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from crea.core.errors import MoleculeLibraryError
from crea.core.models.entity import ChemicalEntity
from crea.core.models.molecule_library import LibraryHit
from crea.services.molecule_identity import MoleculeIdentityService


@runtime_checkable
class MoleculeProvider(Protocol):
    """A source of molecule records (§13). Provides records; the core
    normalizes, identities and computes chemistry -- never the provider."""

    name: str

    def records(self) -> list[ChemicalEntity]: ...


class LocalMoleculeProvider:
    """The local library as a molecule source (§14)."""

    name = "local"

    def __init__(self, library: "MoleculeLibrary") -> None:
        self._library = library

    def records(self) -> list[ChemicalEntity]:
        return self._library.list()


class MoleculeLibrary:
    """Add, look up, deduplicate, search and remove molecule records."""

    def __init__(self, kb) -> None:
        self._kb = kb
        self._identity = MoleculeIdentityService()

    # -- write --------------------------------------------------------------

    def add(self, smiles: str, *, source_type: str = "USER_PROVIDED",
            source_id: str = "") -> tuple[ChemicalEntity, bool]:
        """Add a molecule by SMILES. Returns (record, is_new).

        The same standard identity (InChI) never creates a second record --
        the existing one is returned with `is_new=False` (§9).
        """
        from crea.core.models.identity import StructureInput
        from crea.services.identity import IdentityResolutionService

        identity = self._identity.identity(smiles)  # INVALID_MOLECULE on bad input
        existing = self._kb.find_entity_by_identity_key(identity.identity_key)
        if existing is not None:
            return existing, False

        resolution = IdentityResolutionService().resolve(
            StructureInput.of(kind="SMILES", value=smiles))
        entity = self._kb.entity_registry().register_resolution(resolution)
        entity.identity_source = source_type
        entity.source_id = source_id
        return self._kb.save_entity(entity), True

    # -- read ---------------------------------------------------------------

    def get(self, record_id: str) -> ChemicalEntity:
        from crea.repositories.errors import NotFound

        try:
            return self._kb.get_entity(record_id)
        except NotFound as exc:
            raise MoleculeLibraryError(
                "MOLECULE_NOT_FOUND", f"no molecule record {record_id!r}") from exc

    def find_by_inchikey(self, inchikey: str) -> ChemicalEntity | None:
        return self._kb.find_entity_by_inchikey(inchikey)

    def list(self) -> list[ChemicalEntity]:
        return self._kb.list_entities()

    def count(self) -> int:
        return self._kb.count_entities()

    def delete(self, record_id: str) -> bool:
        """Remove a record. False when no such record; the store refuses a
        delete that would orphan established reaction/mention knowledge."""
        return self._kb.delete_entity(record_id)

    def provider(self) -> MoleculeProvider:
        return LocalMoleculeProvider(self)

    # -- search -------------------------------------------------------------

    def search_similar(self, smiles: str, *, limit: int = 10,
                       source: MoleculeProvider | None = None) -> list[LibraryHit]:
        """Rank the library by fingerprint similarity to `smiles` (§11/§12).

        Reuses `screen_similarity` -- similarity is a screening number, never
        identity and never a match verdict.
        """
        from crea.chem.fingerprint import screen_similarity
        from crea.chem.normalize import NormalizationError, normalize

        query = normalize(smiles)  # NormalizationError -> typed by the caller
        hits = []
        for entity in (source or self.provider()).records():
            if not entity.canonical_smiles:
                continue
            try:
                molecule = normalize(entity.canonical_smiles)
            except NormalizationError:
                continue
            score = screen_similarity(query, molecule).tanimoto_ecfp4
            hits.append(LibraryHit(entity=entity, score=round(score, 4)))
        hits.sort(key=lambda h: -(h.score or 0.0))
        return hits[:limit]

    def search_substructure(self, smiles: str,
                            source: MoleculeProvider | None = None) -> list[LibraryHit]:
        """Records whose structure contains `smiles` as a substructure."""
        from crea.chem.align import substructure_contains
        from crea.chem.normalize import NormalizationError, normalize

        query = normalize(smiles)
        hits = []
        for entity in (source or self.provider()).records():
            if not entity.canonical_smiles:
                continue
            try:
                molecule = normalize(entity.canonical_smiles)
            except NormalizationError:
                continue
            if substructure_contains(molecule, query):
                hits.append(LibraryHit(entity=entity, note="substructure match"))
        return hits


__all__ = ["LocalMoleculeProvider", "MoleculeLibrary", "MoleculeProvider"]

"""PubChem exact-identity lookup and explicit import (Stage 2.3).

Lookup and import are deliberately separate (§9): a lookup returns a verified
result, and only an explicit import writes it into the local library. Import
re-checks `identity_match`, never overwrites CREA's identity (§10), dedupes by
Standard InChIKey, and records PubChem as the provenance / external identifier.
"""

from __future__ import annotations

from crea.core.errors import MoleculeLibraryError
from crea.core.models.entity import ExternalIdentifier
from crea.core.models.pubchem import PubChemMoleculeResult


class PubChemService:
    """Look up a molecule in PubChem; optionally import the verified result."""

    def __init__(self, provider=None, kb=None) -> None:
        self._provider = provider
        self._kb = kb

    def lookup(self, smiles: str) -> PubChemMoleculeResult:
        from crea.services.molecule_identity import MoleculeIdentityService

        identity = MoleculeIdentityService().identity(smiles)
        if self._provider is None:
            raise MoleculeLibraryError(
                "PROVIDER_UNAVAILABLE", "PubChem is not available (offline)")
        return self._provider.lookup_exact(identity.standard_inchikey)

    def import_result(self, result: PubChemMoleculeResult):
        """Import a verified PubChem result into the local library.

        Returns `(record, is_new)`. A mismatch is refused, never silently
        corrected; the same Standard InChIKey dedupes to one record (§10).
        """
        from crea.services.molecule_library import MoleculeLibrary

        if not result.identity_match:
            raise MoleculeLibraryError(
                "PROVIDER_IDENTITY_MISMATCH",
                result.diagnostics[0] if result.diagnostics
                else "PubChem identity does not match the requested identity")
        if self._kb is None:
            raise MoleculeLibraryError(
                "PROVIDER_UNAVAILABLE", "no local library is configured for import")
        if not result.canonical_smiles:
            raise MoleculeLibraryError(
                "PROVIDER_INVALID_RESPONSE",
                "the PubChem record carried no structure to import")

        library = MoleculeLibrary(self._kb)
        entity, is_new = library.add(
            result.canonical_smiles, source_type="PUBCHEM",
            source_id=result.provider_record_id)
        entity.add_external_identifier(ExternalIdentifier(
            namespace="pubchem", identifier=result.provider_record_id,
            source="pubchem"))
        self._kb.save_entity(entity)
        return entity, is_new


__all__ = ["PubChemService"]

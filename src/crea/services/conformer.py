"""Conformer service (CREA Stage 2.1): SMILES -> one generated 3D conformer.

The service validates the input through the existing normalization, then
delegates the embedding to the chemistry layer. It maps failures to the
structured `ConformerError` taxonomy (`INVALID_MOLECULE` vs
`CONFORMER_GENERATION_FAILED`) -- the API never sees an RDKit exception.
"""

from __future__ import annotations

from crea.core.errors import ConformerError
from crea.core.models.conformer import MoleculeConformer


class ConformerService:
    """Generate one representative conformer for a SMILES."""

    def generate(self, smiles: str, *, optimize: bool = False) -> MoleculeConformer:
        from crea.chem.conformer import generate_conformer
        from crea.chem.normalize import NormalizationError, normalize

        try:
            molecule = normalize(smiles)
        except NormalizationError as exc:
            raise ConformerError(
                "INVALID_MOLECULE",
                f"the SMILES is not a parseable structure: {exc}") from exc

        return generate_conformer(molecule, optimize=optimize)


__all__ = ["ConformerService"]

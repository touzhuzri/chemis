"""Similarity between two molecules (CREA Phase 1).

A thin service over the existing chemistry layer: it normalizes two SMILES,
screens them with the existing fingerprints, compares their scaffolds, and
finds the atom-resolved MCS. Nothing here re-implements a similarity engine
(§八); it only composes the three existing capabilities and returns them as
one `SimilarityResult` with provenance.
"""

from __future__ import annotations

from crea.core.models.similarity import SimilarityResult
from crea.core.provenance import utc_now_iso


class SimilarityService:
    """Compare two structures on the existing screening/scaffold/MCS axes."""

    def compare(self, smiles_a: str, smiles_b: str) -> SimilarityResult:
        from crea.chem.align import find_core_match
        from crea.chem.fingerprint import screen_similarity
        from crea.chem.normalize import NormalizationError, normalize
        from crea.chem.scaffold import compare_scaffolds

        try:
            mol_a = normalize(smiles_a)
        except NormalizationError as exc:
            return SimilarityResult(smiles_a=smiles_a, smiles_b=smiles_b,
                                    diagnostics=[f"A: {exc}"])
        try:
            mol_b = normalize(smiles_b)
        except NormalizationError as exc:
            return SimilarityResult(smiles_a=smiles_a, smiles_b=smiles_b,
                                    diagnostics=[f"B: {exc}"])

        screen = screen_similarity(mol_a, mol_b)
        scaffold = compare_scaffolds(mol_a, mol_b)
        core = find_core_match(mol_a, mol_b)

        return SimilarityResult(
            smiles_a=smiles_a, smiles_b=smiles_b,
            identity_a={"canonical_smiles": mol_a.canonical_smiles,
                        "inchikey": mol_a.inchikey},
            identity_b={"canonical_smiles": mol_b.canonical_smiles,
                        "inchikey": mol_b.inchikey},
            tanimoto=round(screen.tanimoto_ecfp4, 4),
            dice=round(screen.dice_ecfp4, 4),
            scaffold_relation=scaffold.relation,
            mcs={
                "found": core.found,
                "common_atoms": core.num_atoms,
                "common_bonds": core.num_bonds,
                "common_substructure_smiles": core.common_substructure_smiles,
                "coverage_a": core.coverage_a,
                "coverage_b": core.coverage_b,
            },
            provenance={
                "tool": "similarity-service",
                "rdkit_version": mol_a.rdkit_version,
                "computed_at": utc_now_iso(),
            },
            diagnostics=[screen.caveat],
        )


__all__ = ["SimilarityService"]

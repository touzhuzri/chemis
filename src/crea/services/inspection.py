"""The Molecule Explorer's deterministic inspection service (CREA Phase 1).

One SMILES in, one structured `MoleculeInspection` out -- computed entirely by
the existing chemistry layer plus the new functional-group and reaction-library
capabilities. Nothing here is an LLM; a molecule is parsed, normalised,
described, and matched against curated reaction templates, and every number is
traceable to the module that computed it.

The service composes; it re-implements no chemistry. Normalization, descriptors,
fingerprints, scaffold, fragmentation and matching all stay where they are.
"""

from __future__ import annotations

from crea.core.models.inspection import MoleculeInspection
from crea.core.provenance import utc_now_iso
from crea.services.reaction_library import ReactionMatcher


def _degree_of_unsaturation(molecule) -> float:
    """DBE = (2C + 2 + N - H - X) / 2, for neutral organic molecules.

    Computed from the normalized atom vector (symbol + implicit H count), not
    from RDKit directly -- the molecule already carries what is needed.
    """
    c = n = h = x = 0
    for atom in molecule.atoms:
        symbol = atom.symbol
        if symbol == "C":
            c += 1
        elif symbol == "N":
            n += 1
        elif symbol in ("F", "Cl", "Br", "I"):
            x += 1
        h += atom.num_hs
    return (2 * c + 2 + n - h - x) / 2


class MoleculeInspectionService:
    """Inspect one molecule, deterministically, end to end."""

    def __init__(self, reactions=None) -> None:
        self._matcher = ReactionMatcher(reactions)

    def inspect(self, smiles: str) -> MoleculeInspection:
        from crea.chem.functional_groups import detect_functional_groups
        from crea.chem.fragment import decompose_molecule
        from crea.chem.normalize import NormalizationError, normalize
        from crea.chem.scaffold import compare_scaffolds, murcko_scaffold_smiles

        try:
            molecule = normalize(smiles)
        except NormalizationError as exc:
            return MoleculeInspection(smiles=smiles, valid=False,
                                      error=str(exc))

        d = molecule.descriptors
        diagnostics: list[str] = []
        if d.net_charge:
            diagnostics.append(
                "DBE assumes a neutral molecule; a charged structure shifts "
                "the degree of unsaturation")

        scaffold_match = compare_scaffolds(molecule, molecule)
        return MoleculeInspection(
            smiles=smiles,
            identity={
                "canonical_smiles": molecule.canonical_smiles,
                "inchi": molecule.inchi,
                "inchikey": molecule.inchikey,
                "mol_id": molecule.mol_id,
            },
            structure={
                "input_smiles": smiles,
                "canonical_smiles": molecule.canonical_smiles,
                "inchi": molecule.inchi,
                "inchikey": molecule.inchikey,
            },
            properties={
                "molecular_formula": d.molecular_formula,
                "molecular_weight": d.mol_wt,
                "exact_molecular_weight": d.exact_mol_wt,
                "formal_charge": d.net_charge,
                "atom_count": len(molecule.atoms),
                "heavy_atom_count": d.heavy_atom_count,
                "ring_count": d.num_rings,
                "aromatic_ring_count": d.num_aromatic_rings,
                "hbd": d.hbd,
                "hba": d.hba,
                "tpsa": d.tpsa,
                "logp": d.logp,
                "rotatable_bonds": d.rotatable_bonds,
                "fraction_csp3": d.fraction_csp3,
                "dbe": _degree_of_unsaturation(molecule),
            },
            functional_groups=detect_functional_groups(molecule),
            scaffold={
                "murcko_smiles": murcko_scaffold_smiles(molecule),
                "ring_systems": list(scaffold_match.ring_systems_a),
                "note": "Murcko scaffold keeps ring systems and the linkers "
                        "between them; acyclic substituents are discarded",
            },
            fragments=[
                {
                    "fragment_id": f.fragment_id,
                    "fragment_smiles": f.fragment_smiles,
                    "formula": f.formula,
                    "n_attachment_points": f.n_attachment_points,
                    "heavy_atom_count": f.heavy_atom_count,
                    "fg_labels": list(f.fg_labels),
                }
                for f in decompose_molecule(molecule, "ring_linker")
            ],
            reactions=self._matcher.match(molecule),
            provenance={
                "tool": "molecule-inspection",
                "rdkit_version": molecule.rdkit_version,
                "normalization_profile": molecule.normalization_profile,
                "computed_at": utc_now_iso(),
            },
            diagnostics=diagnostics,
        )


__all__ = ["MoleculeInspectionService"]

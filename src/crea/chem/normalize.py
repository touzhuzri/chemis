"""Layer 0 -- normalization: the single entry point for creating a Molecule.

Nothing else in the system is allowed to call `Chem.MolFromSmiles`. Centralizing
this keeps one canonical form, one standardization profile and one provenance
record per molecule, which is what makes downstream claims traceable.

Every action taken during normalization is recorded in
`Molecule.normalization_actions`, because "similarity 0.92" is meaningless
without knowing whether counter-ions were stripped and whether tautomers were
canonicalized.
"""

from __future__ import annotations

from rdkit import Chem
from rdkit.Chem import Descriptors, rdMolDescriptors
from rdkit.Chem.MolStandardize import rdMolStandardize

from crea.chem.fingerprint import compute_fingerprints
from crea.core.errors import NormalizationError
from crea.core.models.molecule import (
    AtomRef,
    BondRef,
    Descriptors as DescriptorsModel,
    Molecule,
    NormalizationProfile,
    SourceKind,
    SourceRef,
)
from crea.chem.version import rdkit_version
from crea.core.provenance import timer


def _chiral_tag(tag) -> str:
    return str(tag).split(".")[-1]


def _bond_order(bond) -> str:
    return str(bond.GetBondType()).split(".")[-1]


def _hybridization(atom) -> str:
    return str(atom.GetHybridization()).split(".")[-1]


def _ring_sizes(mol, atom_idx: int) -> tuple[int, ...]:
    return tuple(sorted({len(r) for r in mol.GetRingInfo().AtomRings() if atom_idx in r}))


def _atom_refs(mol) -> list[AtomRef]:
    ranks = list(Chem.CanonicalRankAtoms(mol))
    out: list[AtomRef] = []
    for atom in mol.GetAtoms():
        idx = atom.GetIdx()
        out.append(
            AtomRef(
                mol_id="",  # filled by normalize once the InChIKey is known
                atom_idx=idx,
                canonical_rank=ranks[idx],
                symbol=atom.GetSymbol(),
                atomic_num=atom.GetAtomicNum(),
                aromatic=atom.GetIsAromatic(),
                formal_charge=atom.GetFormalCharge(),
                degree=atom.GetDegree(),
                num_hs=atom.GetTotalNumHs(),
                hybridization=_hybridization(atom),
                is_in_ring=atom.IsInRing(),
                ring_sizes=_ring_sizes(mol, idx),
                chirality_tag=_chiral_tag(atom.GetChiralTag()),
                is_dummy=atom.GetAtomicNum() == 0,
            )
        )
    return out


def _bond_refs(mol) -> list[BondRef]:
    return [
        BondRef(
            mol_id="",
            bond_idx=b.GetIdx(),
            a_idx=b.GetBeginAtomIdx(),
            b_idx=b.GetEndAtomIdx(),
            order=_bond_order(b),
            is_in_ring=b.IsInRing(),
            stereo=str(b.GetStereo()).split(".")[-1],
        )
        for b in mol.GetBonds()
    ]


def _descriptors(mol) -> DescriptorsModel:
    return DescriptorsModel(
        molecular_formula=rdMolDescriptors.CalcMolFormula(mol),
        exact_mol_wt=Descriptors.ExactMolWt(mol),
        mol_wt=Descriptors.MolWt(mol),
        heavy_atom_count=mol.GetNumHeavyAtoms(),
        num_rings=rdMolDescriptors.CalcNumRings(mol),
        num_aromatic_rings=rdMolDescriptors.CalcNumAromaticRings(mol),
        tpsa=Descriptors.TPSA(mol),
        logp=Descriptors.MolLogP(mol),
        hbd=rdMolDescriptors.CalcNumHBD(mol),
        hba=rdMolDescriptors.CalcNumHBA(mol),
        rotatable_bonds=rdMolDescriptors.CalcNumRotatableBonds(mol),
        fraction_csp3=rdMolDescriptors.CalcFractionCSP3(mol),
        net_charge=Chem.GetFormalCharge(mol),
    )


def standardize(mol, profile: NormalizationProfile):
    """Apply a normalization profile. Returns (mol, actions, warnings)."""
    actions: list[str] = []
    warnings: list[str] = []

    if profile != "identity":
        mol = rdMolStandardize.Cleanup(mol)
        actions.append("rdMolStandardize.Cleanup (normalize + reionize)")

    if profile in ("parent", "canonical_tautomer"):
        n_before = mol.GetNumHeavyAtoms()
        mol = rdMolStandardize.FragmentParent(mol)
        if mol.GetNumHeavyAtoms() != n_before:
            removed = n_before - mol.GetNumHeavyAtoms()
            warnings.append(f"removed {removed} heavy atom(s) as counter-ion/solvent")
            actions.append("FragmentParent: kept largest covalent unit")

    if profile == "canonical_tautomer":
        canon = rdMolStandardize.TautomerEnumerator().Canonicalize(mol)
        if Chem.MolToSmiles(canon) != Chem.MolToSmiles(mol):
            actions.append("TautomerEnumerator: canonical tautomer")
        mol = canon

    return mol, actions, warnings


def normalize_mol(
    mol,
    input_repr: str,
    profile: NormalizationProfile = "parent",
    source: SourceKind = "user_input",
) -> Molecule:
    """Normalize an already-parsed RDKit Mol into a `Molecule`."""
    mol, actions, warnings = standardize(mol, profile)

    with timer("crea.chem.normalize.normalize_mol", rdkit_version(), profile=profile) as t:
        canonical_smiles = Chem.MolToSmiles(mol)
        # Every index recorded from here on refers to the canonical parse.
        mol = _as_canonical_graph(mol, canonical_smiles)
        canonical_smiles = Chem.MolToSmiles(mol)
        inchi = Chem.MolToInchi(mol)
        inchikey = Chem.InchiToInchiKey(inchi)
        mol_id = inchikey or f"nopkey-{abs(hash(canonical_smiles)) & 0xFFFFFFFF:08x}"

        atoms = _atom_refs(mol)
        bonds = _bond_refs(mol)
        for a in atoms:
            a.mol_id = mol_id
        for b in bonds:
            b.mol_id = mol_id

        descriptors = _descriptors(mol)
        fps = compute_fingerprints(mol)

    skeleton = "-".join(inchikey.split("-")[:2]) if inchikey else ""

    return Molecule(
        mol_id=mol_id,
        source=SourceRef(kind=source),
        input_repr=input_repr,
        canonical_smiles=canonical_smiles,
        inchi=inchi,
        inchikey=inchikey,
        inchikey_skeleton_block=skeleton,
        rdkit_version=rdkit_version(),
        normalization_profile=profile,
        normalization_actions=actions,
        atoms=atoms,
        bonds=bonds,
        descriptors=descriptors,
        fingerprints=fps,
        warnings=warnings,
        provenance=t.finish(),
    )


def _as_canonical_graph(mol, canonical_smiles: str):
    """Return the molecule obtained by parsing `canonical_smiles`.

    The `Molecule` graph is *defined* as that parse, and the recorded atom and
    bond vectors are built from it. That definition is what makes the indices
    trustworthy: `rebuild()` re-parses the same string and therefore reproduces
    the same atom and bond ordering, so an index carried on a `Molecule` means
    the same thing everywhere it is used.

    This is not cosmetic. Building the vectors from the pre-canonical molecule
    instead meant attachment points and unmatched components pointed at bonds of
    a different molecule -- the 1,4-diphenyl-1,3-diyne attachment bonds
    resolved to ring bonds. `Chem.RenumberAtoms` does not fix it either, since
    it does not preserve bond ordering.
    """
    reparsed = Chem.MolFromSmiles(canonical_smiles)
    return reparsed if reparsed is not None else mol


def rebuild(molecule: Molecule):
    """Reconstruct the RDKit Mol for an already-normalized `Molecule`.

    This is the only sanctioned way for the other chem modules to get an RDKit
    Mol back from a `Molecule`. It bypasses no standardization: the string it
    parses is already the canonical, normalized form, and it is the very parse
    the recorded atom and bond vectors were built from.
    """
    return Chem.MolFromSmiles(molecule.canonical_smiles)


def normalize(
    smiles: str,
    profile: NormalizationProfile = "parent",
    source: SourceKind = "user_input",
    max_heavy_atoms: int | None = None,
) -> Molecule:
    """Parse a SMILES string into a normalized `Molecule`.

    Raises `NormalizationError` for input RDKit cannot parse, so that callers
    never have to know RDKit's exception surface.
    """
    if not isinstance(smiles, str) or not smiles.strip():
        raise NormalizationError("empty SMILES input", input_repr=str(smiles))

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise NormalizationError("RDKit could not parse SMILES", input_repr=smiles)

    if max_heavy_atoms is not None and mol.GetNumHeavyAtoms() > max_heavy_atoms:
        raise NormalizationError(
            f"molecule has {mol.GetNumHeavyAtoms()} heavy atoms, "
            f"limit is {max_heavy_atoms}",
            input_repr=smiles,
        )

    return normalize_mol(mol, input_repr=smiles, profile=profile, source=source)


def normalize_inchi(
    inchi: str,
    profile: NormalizationProfile = "parent",
    source: SourceKind = "user_input",
) -> Molecule:
    """Parse an InChI string into a normalized `Molecule`.

    The second of the two deterministic structure notations this layer accepts,
    and the same kind of call as `normalize`: notation in, `Molecule` out, no
    interpretation. It lives here rather than in the identity service because
    "nothing else calls `Chem.MolFromInchi`" is the same rule as the one
    `normalize` already enforces for `MolFromSmiles`, and one rule stated in
    two places is a rule that will drift.

    Note what this is *not*: it is not `name -> SMILES`, and it is not
    `image -> SMILES`. Both of those are lookups or recognitions rather than
    parsing, and both live outside the Chemical Core by design.
    """
    if not isinstance(inchi, str) or not inchi.strip():
        raise NormalizationError("empty InChI input", input_repr=str(inchi))

    mol = Chem.MolFromInchi(inchi.strip())
    if mol is None:
        raise NormalizationError("RDKit could not parse InChI", input_repr=inchi)

    return normalize_mol(mol, input_repr=inchi.strip(), profile=profile, source=source)

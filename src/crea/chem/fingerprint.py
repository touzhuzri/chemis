"""Layer 1 -- molecular fingerprints and fast similarity.

Fingerprints here are a *screening* device. They decide what deserves a closer
look; they are not evidence of similarity. A Tanimoto score of 0.93 between
ortho- and para-xylene means "worth aligning", not "the same molecule" -- the
Morgan encoder cannot see substitution position, and neither can its bits.
"""

from __future__ import annotations

from rdkit import Chem, DataStructs
from rdkit.Chem import rdFingerprintGenerator, rdMolDescriptors

from crea.core.models.matching import SimilarityScreen
from crea.core.models.molecule import FingerprintSet, Molecule

RADIUS = 2
FP_SIZE = 2048

_ECFP_GEN = rdFingerprintGenerator.GetMorganGenerator(radius=RADIUS, fpSize=FP_SIZE)
_FCFP_GEN = rdFingerprintGenerator.GetMorganGenerator(
    radius=RADIUS,
    fpSize=FP_SIZE,
    atomInvariantsGenerator=rdFingerprintGenerator.GetMorganFeatureAtomInvGen(),
)


def _bitvect_to_hex(bv) -> str:
    """Pack an ExplicitBitVect into hex, bit i having weight 2**i."""
    bits = bv.ToBitString()
    return format(int(bits[::-1], 2), "x").zfill((bv.GetNumBits() + 3) // 4)


def _hex_to_bitvect(value: str, nbits: int):
    bv = DataStructs.ExplicitBitVect(nbits)
    num = int(value, 16)
    for i in range(nbits):
        if num & (1 << i):
            bv.SetBit(i)
    return bv


def compute_fingerprints(mol) -> FingerprintSet:
    return FingerprintSet(
        ecfp4_fp2048=_bitvect_to_hex(_ECFP_GEN.GetFingerprint(mol)),
        fcfp4_fp2048=_bitvect_to_hex(_FCFP_GEN.GetFingerprint(mol)),
        maccs167=_bitvect_to_hex(rdMolDescriptors.GetMACCSKeysFingerprint(mol)),
        generator=f"rdkit.Chem.rdFingerprintGenerator.GetMorganGenerator(radius={RADIUS})",
        radius=RADIUS,
        fp_size=FP_SIZE,
    )


def fragment_tanimoto(smiles_a: str, smiles_b: str) -> float:
    """Tanimoto between two fragment SMILES. Screening use only."""
    ma, mb = Chem.MolFromSmiles(smiles_a), Chem.MolFromSmiles(smiles_b)
    if ma is None or mb is None:
        return 0.0
    return float(
        DataStructs.TanimotoSimilarity(_ECFP_GEN.GetFingerprint(ma),
                                       _ECFP_GEN.GetFingerprint(mb))
    )


def _sim(mol_a: Molecule, mol_b: Molecule, key: str, nbits: int) -> float:
    bv_a = _hex_to_bitvect(getattr(mol_a.fingerprints, key), nbits)
    bv_b = _hex_to_bitvect(getattr(mol_b.fingerprints, key), nbits)
    return float(DataStructs.TanimotoSimilarity(bv_a, bv_b))


def _dice(mol_a: Molecule, mol_b: Molecule, key: str, nbits: int) -> float:
    bv_a = _hex_to_bitvect(getattr(mol_a.fingerprints, key), nbits)
    bv_b = _hex_to_bitvect(getattr(mol_b.fingerprints, key), nbits)
    return float(DataStructs.DiceSimilarity(bv_a, bv_b))


def screen_similarity(mol_a: Molecule, mol_b: Molecule) -> SimilarityScreen:
    """Cheap screen. Never a conclusion on its own."""
    tanimoto = _sim(mol_a, mol_b, "ecfp4_fp2048", FP_SIZE)
    formula_equal = (
        mol_a.descriptors.molecular_formula == mol_b.descriptors.molecular_formula
    )
    heavy_delta = abs(
        mol_a.descriptors.heavy_atom_count - mol_b.descriptors.heavy_atom_count
    )
    skeleton_equal = (
        bool(mol_a.inchikey_skeleton_block)
        and mol_a.inchikey_skeleton_block == mol_b.inchikey_skeleton_block
    )

    # Same formula and same heavy-atom count but a different InChIKey means the
    # molecules are isomers. Which kind of isomer is decided by the skeleton
    # block: equal block => same connectivity, so the difference is
    # stereochemistry only; different block => connectivity/position differs.
    distinct = bool(mol_a.inchikey) and mol_a.inchikey != mol_b.inchikey
    isomeric = formula_equal and heavy_delta == 0 and distinct
    stereo_hint = isomeric and skeleton_equal
    constitutional_hint = isomeric and not skeleton_equal

    caveat = (
        "Tanimoto is a screening number from hashed structural features, not a "
        "chemical verdict. A high value does not imply the same substitution "
        "pattern or the same stereochemistry."
    )
    if isomeric:
        caveat += (
            " These two share a molecular formula and heavy-atom count but not "
            "an InChIKey, so they are isomers; the layers below resolve which "
            "kind."
        )

    return SimilarityScreen(
        tanimoto_ecfp4=tanimoto,
        dice_ecfp4=_dice(mol_a, mol_b, "ecfp4_fp2048", FP_SIZE),
        tanimoto_fcfp4=_sim(mol_a, mol_b, "fcfp4_fp2048", FP_SIZE),
        tanimoto_maccs=_sim(mol_a, mol_b, "maccs167", 167),
        formula_identical=formula_equal,
        heavy_atom_delta=heavy_delta,
        skeleton_block_equal=skeleton_equal,
        constitutional_isomer_hint=constitutional_hint,
        stereo_isomer_hint=stereo_hint,
        caveat=caveat,
    )

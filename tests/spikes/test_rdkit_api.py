"""Regression guards for the RDKit API facts the architecture depends on.

Every assertion here was established by probing rdkit 2026.03.6 directly. They
are cheap and they are worth keeping: three of these APIs changed shape between
RDKit releases, and the code above is written against the exact behaviour
pinned below. If an RDKit upgrade breaks one of them, this file says which
assumption failed instead of producing a quietly wrong number.
"""

import pytest
from rdkit import Chem, DataStructs
from rdkit.Chem import BRICS, ChemicalFeatures, Recap as RECAP, rdFMCS
from rdkit.Chem import FunctionalGroups
from rdkit.Chem import rdMolDescriptors

from crea.chem.fingerprint import _ECFP_GEN


def test_001_substructure_matching_argument_order():
    # The molecule is the receiver; the query is the argument. Getting this
    # backwards makes every "smaller pattern in larger target" search return
    # nothing, which looks exactly like a broken build.
    toluene = Chem.MolFromSmiles("Cc1ccccc1")
    benzene = Chem.MolFromSmarts("c1ccccc1")
    match = toluene.GetSubstructMatch(benzene)
    assert len(match) == 6
    assert 0 not in match, "the methyl must not be part of the benzene match"
    assert toluene.HasSubstructMatch(benzene)


def test_002_mcsresult_has_no_smarts_atom_map_number():
    res = rdFMCS.FindMCS(
        [Chem.MolFromSmiles("Cc1ccccc1"), Chem.MolFromSmiles("Cc1cccc(C)c1")],
        timeout=10,
        atomCompare=rdFMCS.AtomCompare.CompareElements,
        bondCompare=rdFMCS.BondCompare.CompareOrderExact,
    )
    assert not hasattr(res, "smartsAtomMapNumber"), (
        "this build no longer exposes the query-side atom map; if a future one "
        "does, align.py may simplify but must keep the enumeration consistent"
    )
    assert res.queryMol is not None


def test_003_compare_order_treats_aromatic_as_single():
    # The reason the default 'relaxed' profile uses CompareOrderExact.
    common = dict(
        atomCompare=rdFMCS.AtomCompare.CompareElements,
        ringMatchesRingOnly=True,
        completeRingsOnly=True,
        timeout=10,
    )
    loose = rdFMCS.FindMCS(
        [Chem.MolFromSmiles("c1ccccc1"), Chem.MolFromSmiles("C1CCCCC1")],
        bondCompare=rdFMCS.BondCompare.CompareOrder, **common,
    )
    strict = rdFMCS.FindMCS(
        [Chem.MolFromSmiles("c1ccccc1"), Chem.MolFromSmiles("C1CCCCC1")],
        bondCompare=rdFMCS.BondCompare.CompareOrderExact, **common,
    )
    assert loose.numAtoms == 6, "CompareOrder matches benzene to cyclohexane"
    assert strict.numAtoms == 0, "CompareOrderExact must not"


def test_004_mol_fragment_to_smiles_does_not_add_boundary_dummies():
    m = Chem.MolFromSmiles("c1ccccc1C#Cc2ccccc2")
    out = Chem.MolFragmentToSmiles(m, atomsToUse=list(range(6, 12)), canonical=True)
    assert "*" not in out, "this build truncates the SMILES instead of capping it"


def test_005_mol_fragment_to_smiles_with_atom_labels_is_absent():
    assert not hasattr(Chem, "MolFragmentToSmilesWithAtomLabels")


def test_006_rooted_canonical_smiles_is_stable():
    m = Chem.MolFromSmiles("Cc1ccccc1C")
    cut = [b.GetIdx() for b in m.GetBonds()
           if b.GetBeginAtom().IsInRing() != b.GetEndAtom().IsInRing()]
    em = Chem.RWMol()
    idx_map = {i: em.AddAtom(Chem.Atom(m.GetAtomWithIdx(i))) for i in range(1, 7)}
    for b in m.GetBonds():
        if b.GetIdx() in cut:
            continue
        a, e = b.GetBeginAtomIdx(), b.GetEndAtomIdx()
        if a in idx_map and e in idx_map:
            em.AddBond(idx_map[a], idx_map[e], Chem.BondType(b.GetBondType()))
    dummies = []
    for b in m.GetBonds():
        if b.GetIdx() not in cut:
            continue
        a, e = b.GetBeginAtomIdx(), b.GetEndAtomIdx()
        inside = a if a in idx_map else e
        d = em.AddAtom(Chem.Atom(0))
        em.AddBond(idx_map[inside], d, Chem.BondType.SINGLE)
        dummies.append(d)
    piece = em.GetMol()
    Chem.SanitizeMol(piece)
    rcads = {Chem.MolToSmiles(piece, rootedAtAtom=d, canonical=True) for d in dummies}
    assert rcads == {"*c1ccccc1*"}, f"ortho-xylene ring attachment descriptor was {rcads}"


def test_007_brics_api_shapes():
    m = Chem.MolFromSmiles("CC(=O)Oc1ccccc1C(=O)O")
    assert isinstance(BRICS.FindBRICSBonds(m), type((x for x in ())))
    pairs = list(BRICS.FindBRICSBonds(m))
    assert pairs and all(len(p) == 2 for p in pairs)
    assert isinstance(BRICS.BreakBRICSBonds(m), Chem.Mol)


def test_008_recap_decompose_returns_a_hierarchy_node():
    node = RECAP.RecapDecompose(Chem.MolFromSmiles("CC(=O)Oc1ccccc1C(=O)O"))
    assert hasattr(node, "GetLeaves")
    assert RECAP.RecapDecompose(Chem.MolFromSmiles("Cc1ccccc1C")).GetLeaves() == {}


def test_009_crippen_lives_under_an_underscore_name():
    m = Chem.MolFromSmiles("C#Cc1ccccc1")
    contribs = rdMolDescriptors._CalcCrippenContribs(m)
    assert len(contribs) == m.GetNumAtoms()
    assert not hasattr(rdMolDescriptors, "CrippenContribs")


def test_010_base_features_fdef_is_not_a_functional_group_set():
    # BaseFeatures yields pharmacophore families only; the chemical functional
    # group names used by the role rules come from FunctionalGroups.
    import os

    from rdkit import RDConfig

    fac = ChemicalFeatures.BuildFeatureFactory(
        os.path.join(RDConfig.RDDataDir, "BaseFeatures.fdef")
    )
    families = {f.GetFamily() for f in fac.GetFeaturesForMol(Chem.MolFromSmiles("Brc1ccccc1"))}
    assert "Aromatic" in families
    assert "Halogen" not in families

    names = set()

    def walk(node):
        if getattr(node, "smarts", None) and getattr(node, "name", None):
            names.add(str(node.name))
        for child in (getattr(node, "children", None) or []):
            walk(child)

    for root in FunctionalGroups.BuildFuncGroupHierarchy():
        walk(root)
    assert "Halogen" in names
    assert "Terminal Alkyne" in names
    assert "Boronic Acid" in names


def test_011_explicit_bitvect_lives_in_datastructs():
    bv = _ECFP_GEN.GetFingerprint(Chem.MolFromSmiles("c1ccccc1"))
    assert isinstance(bv, DataStructs.ExplicitBitVect)
    assert bv.GetNumOnBits() > 0


def test_012_degenerate_mcs_returns_zero_atoms():
    res = rdFMCS.FindMCS(
        [Chem.MolFromSmiles("c1ccccc1"), Chem.MolFromSmiles("N#N")],
        timeout=5,
        atomCompare=rdFMCS.AtomCompare.CompareElements,
        bondCompare=rdFMCS.BondCompare.CompareOrderExact,
    )
    assert res.numAtoms == 0
    assert res.smartsString == ""


def test_013_inchikey_skeleton_block_differs_for_positional_isomers():
    keys = []
    for smi in ("Cc1ccccc1C", "Cc1cccc(C)c1", "Cc1ccc(C)cc1"):
        m = Chem.MolFromSmiles(smi)
        keys.append(Chem.InchiToInchiKey(Chem.MolToInchi(m)).split("-")[0])
    assert len(set(keys)) == 3, (
        "the skeleton block encodes connectivity, so it must differ for "
        "positional isomers; code depending on it for regioisomer detection "
        "would be wrong"
    )

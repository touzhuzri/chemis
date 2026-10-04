"""Functional-group detection and SMARTS matching.

The one sanctioned place a molecule is asked "which functional groups are
present". Detection uses a curated, named list of structural features --
alkene, alkyne, alcohol, phenol, ether, carbonyl, acid, ester, amide, amine,
nitrile, halides -- matched by SMARTS substructure search. Names here are
deterministic structural labels, never a model's guess, and the same
`matches_smarts` primitive powers the reaction library.

This module adds capability; it re-implements nothing -- normalization,
fragmentation and scaffold stay where they are.
"""

from __future__ import annotations

from rdkit import Chem

from crea.chem.normalize import rebuild
from crea.core.models.functional_group import FunctionalGroup
from crea.core.models.molecule import Molecule

#: Curated functional-group labels with their SMARTS. Deliberately small and
#: readable -- a label is a structural feature, not a reaction claim. The
#: reaction library uses its own substrate SMARTS for matching.
_CURATED_GROUPS: tuple[tuple[str, str], ...] = (
    ("Alkene", "[C]=[C]"),
    ("Alkyne", "[C]#[C]"),
    ("Terminal Alkyne", "[CH]#[C]"),
    ("Alcohol", "[C;!$(C=O)][OX2H]"),
    ("Phenol", "[c][OX2H]"),
    ("Ether", "[OD2]([#6])[#6]"),
    ("Aldehyde", "[CX3H1](=O)[#6]"),
    ("Ketone", "[CX3H0](=O)([#6])[#6]"),
    ("Carboxylic Acid", "[CX3](=O)[OX2H]"),
    ("Ester", "[CX3](=O)[OX2][#6]"),
    ("Amide", "[CX3](=O)[NX3]"),
    ("Amine", "[NX3;!$(N=O);!$(N[#6]=O)]"),
    ("Nitrile", "[C]#[N]"),
    ("Alkyl Halide", "[C;!$(C=O)]-[F,Cl,Br,I]"),
    ("Aryl Halide", "[c][F,Cl,Br,I]"),
)


def matches_smarts(molecule: Molecule, smarts: str) -> list[tuple[int, ...]]:
    """Every atom-index tuple matching `smarts` in the molecule.

    A SMARTS that does not parse, or a molecule that does not rebuild, matches
    nothing -- an unparseable query contains nothing.
    """
    mol = rebuild(molecule)
    if mol is None:
        return []
    pattern = Chem.MolFromSmarts(smarts)
    if pattern is None:
        return []
    return list(mol.GetSubstructMatches(pattern, uniquify=True))


def detect_functional_groups(molecule: Molecule) -> list[FunctionalGroup]:
    """The functional groups present in a molecule, deterministic and named."""
    groups: list[FunctionalGroup] = []
    for name, smarts in _CURATED_GROUPS:
        matches = matches_smarts(molecule, smarts)
        if matches:
            groups.append(FunctionalGroup(
                name=name, smarts=smarts, count=len(matches),
                atom_indices=[list(m) for m in matches]))
    return groups


__all__ = ["detect_functional_groups", "matches_smarts"]

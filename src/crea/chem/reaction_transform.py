"""Deterministic product generation from a reaction SMARTS (CREA Stage 5.4).

The one place a substrate becomes a product, and only when the template
carries a real transform (`reactant>>product` SMARTS). RDKit applies the
transform; nothing is guessed. An empty transform, an unparseable one, or a
substrate the transform does not match all return `None` -- the caller stops
rather than inventing a structure.
"""

from __future__ import annotations

from crea.chem.normalize import rebuild


def apply_reaction_smarts(reaction_smarts: str, molecule) -> str | None:
    """The product SMILES of `reaction_smarts` on `molecule`, or None.

    Deterministic (RDKit's own reactant/atom-mapped ordering) and
    sanitized. `None` means "no deterministic product here", never a guess.
    """
    if not (reaction_smarts or "").strip():
        return None
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem

        transform = AllChem.ReactionFromSmarts(reaction_smarts)
    except Exception:  # noqa: BLE001 - an unparseable transform makes nothing
        return None
    if transform is None:
        return None

    substrate = rebuild(molecule)
    if substrate is None:
        return None
    try:
        product_sets = transform.RunReactants((substrate,))
    except Exception:  # noqa: BLE001 - a transform that cannot run makes nothing
        return None
    if not product_sets or not product_sets[0]:
        return None
    try:
        product = product_sets[0][0]
        Chem.SanitizeMol(product)
        return Chem.MolToSmiles(product)
    except Exception:  # noqa: BLE001 - an invalid product is no product
        return None


__all__ = ["apply_reaction_smarts"]

"""The standard chemical identity of a molecule (CREA Stage 2.2).

Identity is Standard InChI + Standard InChIKey -- the cross-software,
cross-database identity (§2). `canonical_smiles` is a structure notation,
never the identity (§3). `identity_key` is CREA's internal InChI-wrapped
key (versioned), a record aid, not a replacement for the standard pair.
"""

from __future__ import annotations

from pydantic import BaseModel


class MoleculeIdentity(BaseModel):
    standard_inchi: str
    standard_inchikey: str
    canonical_smiles: str
    molecular_formula: str
    molecular_weight: float
    formal_charge: int = 0
    #: CREA's internal, versioned key (`crea-identity-key-v1:<InChI>`); the
    #: standard InChI is the identity, this is its stored form.
    identity_key: str = ""


__all__ = ["MoleculeIdentity"]

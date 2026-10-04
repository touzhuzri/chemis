"""Representations: typed, sourced notations of one chemical entity.

CREA 4.0 Stage B-1 (§3). A chemical entity *has* representations; it is
not them. `C#Cc1ccccc1`, its standard InChI, its InChIKey and its
molblock are four notations of ONE identity -- never four entities --
and every notation keeps the source it arrived from.

Rules:

* the type is explicit (`kind`), never inferred from the string's shape;
* provenance travels with the notation (who produced it, when);
* a notation that cannot be parsed fails here, explicitly -- it is never
  stored as a structure on the strength of looking like one;
* canonicalization is RDKit's (via `crea.chem`), never this module's.
"""

from __future__ import annotations

from crea.core.models.entity import (
    REPRESENTATION_KINDS,
    ChemicalRepresentation,
)
from crea.core.models.molecule import Molecule
from crea.core.provenance import utc_now_iso


class RepresentationError(ValueError):
    """A notation that is not a notation. Said out loud, never absorbed."""


def build_representations(molecule: Molecule, *, source: str = "rdkit",
                          source_id: str = "") -> list[ChemicalRepresentation]:
    """The full notation set of an already-normalized molecule.

    Input to this function is a `Molecule` the chem layer produced -- the
    structures were already parsed and standardized. This assembles the
    typed records; it performs no chemistry of its own.
    """
    from crea.chem import representations as chem_reps

    if not molecule.canonical_smiles:
        raise RepresentationError("molecule carries no canonical SMILES")
    now = utc_now_iso()
    values = {
        "canonical_smiles": molecule.canonical_smiles,
        "isomeric_smiles": chem_reps.isomeric_smiles(molecule),
        "standard_inchi": molecule.inchi,
        "inchikey": molecule.inchikey,
        "molblock": chem_reps.molblock(molecule),
    }
    out: list[ChemicalRepresentation] = []
    for kind in REPRESENTATION_KINDS:
        value = values.get(kind) or ""
        if not value:
            continue  # absent notation: absent, never fabricated
        out.append(ChemicalRepresentation(
            kind=kind, value=value, source=source, source_id=source_id,
            retrieved_at=now))
    return out


def representations_from_notation(kind: str, value: str, *,
                                  source: str = "user_input",
                                  source_id: str = ""
                                  ) -> list[ChemicalRepresentation]:
    """Parse ONE notation into the entity's full representation set.

    The caller says the kind (a clipboard adapter, a user input form); this
    module never guesses it from the string. The notation is parsed by
    RDKit through the chem layer -- fail loudly on anything else.
    """
    from crea.chem.normalize import normalize, normalize_inchi

    kind = (kind or "").strip().lower()
    if kind not in REPRESENTATION_KINDS:
        raise RepresentationError(f"unknown representation kind: {kind!r}")
    text = (value or "").strip()
    if not text:
        raise RepresentationError("empty representation value")

    if kind in ("canonical_smiles", "isomeric_smiles", "molblock"):
        # A molblock is a SMILES source only through a parser; normalize()
        # takes SMILES, so molblock enters via the same parser path by
        # way of the molecule it denotes -- handled in the resolver, which
        # has the full input vocabulary. Here: SMILES path only.
        if kind == "molblock":
            raise RepresentationError(
                "molblock parsing enters through SemanticResolver "
                "(ChemicalInput), not through a raw string path")
        molecule = normalize(text, source="user_input")
    elif kind == "standard_inchi":
        molecule = normalize_inchi(text, source="user_input")
    else:  # inchikey is a lookup key, not a parseable structure (§5)
        raise RepresentationError(
            "an InChIKey is a lookup key, not a structure notation; "
            "resolve it through the identifier path")

    return build_representations(molecule, source=source, source_id=source_id)


__all__ = [
    "RepresentationError",
    "build_representations",
    "representations_from_notation",
]

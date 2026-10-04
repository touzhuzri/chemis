"""Identity, judged: equivalence and evidence level (§4/§5).

Two questions this module answers, both deterministically:

* **Are these the same identity?** -- via the existing `IdentityFacets`
  and `measure_facets` (Phase 3.6). There is no second identity system
  here: representations are parsed and compared by the chem layer, and
  the facets say how far the agreement goes. String equality is not an
  identity judgement and appears nowhere.
* **How strong is this identity claim?** -- `IdentityEvidenceLevel`
  derived from the *chain that produced it* (§4). A name resolution never
  graduates to EXACT because it happened to find one structure; an EXACT
  label is reserved for input that carried a structure notation into a
  deterministic parser.

When the existing facets cannot express a judgement, the answer is
extended here in the same vocabulary (`IdentityFacets`) -- never a
parallel `Identity` class.
"""

from __future__ import annotations

from crea.core.models.entity import identity_key
from crea.core.models.identity import (
    ChemicalIdentityResolution,
    IdentityBasis,
    IdentityEvidenceLevel,
    IdentityFacets,
    MatchedBy,
    StructureInputKind,
    StructureResolutionStatus,
)

#: Inputs that are structure notations in their own right: the string IS
#: the structure, and a parser is the only reader it needs (§4 EXACT).
_NOTATION_KINDS = {
    StructureInputKind.SMILES,
    StructureInputKind.INCHI,
}


def same_entity_identity(inchi_a: str, inchi_b: str) -> IdentityBasis:
    """How far two established InChIs agree, as a basis (Phase 3.6 rules)."""
    return facets_for(inchi_a, inchi_b).basis()


def facets_for(inchi_a: str, inchi_b: str) -> IdentityFacets:
    """Facet-by-facet comparison via the existing measure_facets (§5)."""
    from crea.services.identity import measure_facets

    return measure_facets(inchi_a, inchi_b)


def identity_key_of_inchi(inchi: str) -> str:
    """The registry identity key of an established InChI."""
    return identity_key(inchi)


def identity_evidence_level(resolution: ChemicalIdentityResolution) -> IdentityEvidenceLevel:
    """What the resolution's chain is worth (§4). Rules, not vibes:

    * a structure notation parsed locally -> EXACT
    * resolved through a primary name or a deterministic identifier key
      (CID / InChIKey / InChI / SMILES lookup) -> SUPPORTED
    * resolved through a synonym or an unrecorded route -> PROBABLE
      (the chain points one way; the link is weaker than a primary record)
    * several distinct structures survived -> AMBIGUOUS
    * nothing established (absent, unavailable, unparseable) -> UNRESOLVED

    A name resolution never earns EXACT because it found one structure:
    the label reports the chain, and a chain that started from a word is
    not a chain that started from a structure (§4's warning).
    """
    status = resolution.status
    if status is StructureResolutionStatus.AMBIGUOUS:
        return IdentityEvidenceLevel.AMBIGUOUS
    if status is not StructureResolutionStatus.RESOLVED:
        return IdentityEvidenceLevel.UNRESOLVED

    if resolution.input.kind in _NOTATION_KINDS:
        return IdentityEvidenceLevel.EXACT

    selected = resolution.selected
    if selected is None:  # pragma: no cover - RESOLVED always selects
        return IdentityEvidenceLevel.UNRESOLVED

    if selected.matched_by in (MatchedBy.SYNONYM, MatchedBy.UNKNOWN,
                               MatchedBy.IMAGE_RECOGNITION):
        return IdentityEvidenceLevel.PROBABLE
    return IdentityEvidenceLevel.SUPPORTED


def evidence_for_match(level: IdentityEvidenceLevel, resolved_inchi: str,
                       against_inchi: str) -> IdentityEvidenceLevel:
    """The level of "this resolved structure IS that entity" (§5).

    Downgrades to PROBABLE when the facet comparison leaves anything
    undetermined (undefined stereochemistry and friends): the chain may be
    strong and the *match* still incomplete. Never upgrades.
    """
    if level in (IdentityEvidenceLevel.AMBIGUOUS,
                 IdentityEvidenceLevel.UNRESOLVED):
        return level
    facets = facets_for(resolved_inchi, against_inchi)
    incomplete = (facets.same_stereo is not True
                  or facets.same_isotope is not True
                  or facets.same_tautomer is not True)
    if incomplete:
        return IdentityEvidenceLevel.PROBABLE
    return level


__all__ = [
    "evidence_for_match",
    "facets_for",
    "identity_evidence_level",
    "identity_key_of_inchi",
    "same_entity_identity",
]

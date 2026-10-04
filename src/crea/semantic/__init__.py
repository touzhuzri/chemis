"""Chemical Semantic Layer (CREA 4.0, Stage B-1).

The spine: any chemical input -- a structure notation, a name, an
identifier -- is mapped to ONE `ChemicalEntity`, with its representations,
its identity evidence level, and its provenance, by deterministic means.

Nothing here decides chemistry by string equality, nothing here guesses a
structure, and no LLM reaches this layer: structures come from RDKit,
names from resolvers, and uncertainty from the rules in
`core.models.identity.IdentityEvidenceLevel`.

Layering: `core -> chem -> services -> semantic -> agent/report/app/api`.
This module may import core/chem/services; services never import it
(semantic resolvers are injected at the composition roots). Provider
adapters that speak to the outside world live in `crea.providers` and are
injected the same way.
"""

from crea.semantic.identity import identity_evidence_level, same_entity_identity
from crea.semantic.naming import (
    NameLookupHit,
    NameLookupProvider,
    NameLookupResult,
    NameResolver,
)
from crea.semantic.representations import (
    build_representations,
    representations_from_notation,
)
from crea.semantic.resolver import (
    ChemicalInput,
    InputKind,
    ProviderIdentityResult,
    ResolutionResult,
    SemanticResolver,
)

__all__ = [
    "ChemicalInput",
    "InputKind",
    "NameLookupHit",
    "NameLookupProvider",
    "NameLookupResult",
    "NameResolver",
    "ProviderIdentityResult",
    "ResolutionResult",
    "SemanticResolver",
    "build_representations",
    "identity_evidence_level",
    "representations_from_notation",
    "same_entity_identity",
]

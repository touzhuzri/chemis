"""Provider / adapter layer -- interfaces only in Phase 1.

No OpenAlex, Crossref, Unpaywall, PubChem, Web of Science, PDF parsing or
retrosynthesis code is implemented here yet. Only the boundary is defined.
"""

from crea.providers.base import (
    Availability,
    Capability,
    CompoundProvider,
    CompoundQuery,
    LiteratureProvider,
    LiteratureQuery,
    ProviderRegistry,
    ProviderStatus,
    ReactionProvider,
    ReactionQuery,
)

__all__ = [
    "Availability",
    "Capability",
    "CompoundProvider",
    "CompoundQuery",
    "LiteratureProvider",
    "LiteratureQuery",
    "ProviderRegistry",
    "ProviderStatus",
    "ReactionProvider",
    "ReactionQuery",
]

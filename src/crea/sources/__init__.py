"""Provider adapters.

`sources.base` holds the transport seam and shared machinery; `sources.http`
is the only module permitted to touch the network; `sources.openalex`,
`sources.crossref` and `sources.unpaywall` are the three implemented
providers. Crossref's and Unpaywall's *policy* -- rate limits, terms, data
licences -- remains UNVERIFIED in the contract sheet, and no code assumes it.
"""

from crea.sources.crossref import CrossrefProvider
from crea.sources.openalex import OpenAlexProvider
from crea.sources.unpaywall import UnpaywallProvider
from crea.sources.base import (
    BaseProvider,
    PaperRef,
    ProviderResult,
    RateLimit,
    RateSnapshot,
    Response,
    Transport,
    redact_url,
)

__all__ = [
    "BaseProvider",
    "CrossrefProvider",
    "OpenAlexProvider",
    "UnpaywallProvider",
    "PaperRef",
    "ProviderResult",
    "RateLimit",
    "RateSnapshot",
    "Response",
    "Transport",
    "redact_url",
]

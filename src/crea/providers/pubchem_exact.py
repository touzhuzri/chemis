"""PubChem exact-structure lookup with identity verification (Stage 2.3).

One question: does PubChem's standard record for a given Standard InChIKey
agree with CREA's identity? The provider asks PubChem by InChIKey, reads back
the record's InChIKey, and returns a `PubChemMoleculeResult` whose
`identity_match` says whether the two agree (§5). A mismatch is reported with
both identities, never silently corrected, and never imported.

Errors are typed (`PROVIDER_NOT_FOUND` / `PROVIDER_RATE_LIMITED` /
`PROVIDER_UNAVAILABLE` / `PROVIDER_INVALID_RESPONSE`) -- never a raw HTTP
exception (§14). Rate limiting and bounded retry live in `PubChemSource`
(§13); this module only configures them.
"""

from __future__ import annotations

from crea.core.errors import (
    InvalidRequest,
    InvalidUpstreamResponse,
    MoleculeLibraryError,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
    ResourceNotFound,
    UpstreamError,
)
from crea.core.models.pubchem import PubChemMoleculeResult
from crea.providers.base import CompoundQuery
from crea.sources.pubchem import PubChemSource

#: 5 req/s floor, retry transient errors twice with exponential backoff (§13).
_REQUEST_INTERVAL = 0.2
_MAX_RETRIES = 2


class PubChemExactProvider:
    """Exact-identity lookup against PubChem, with verification."""

    name = "pubchem"

    def __init__(self, transport=None, source: PubChemSource | None = None) -> None:
        if source is not None:
            self._source = source
        elif transport is not None:
            self._source = PubChemSource(
                transport=transport, min_request_interval=_REQUEST_INTERVAL,
                max_retries=_MAX_RETRIES, retry_backoff=1.0)
        else:
            self._source = None

    def available(self) -> bool:
        return self._source is not None

    def lookup_exact(self, standard_inchikey: str) -> PubChemMoleculeResult:
        """PubChem's record for `standard_inchikey`, identity verified."""
        if self._source is None:
            raise MoleculeLibraryError(
                "PROVIDER_UNAVAILABLE",
                "PubChem is not reachable (no transport configured; offline)")

        try:
            candidates = self._source.lookup(
                CompoundQuery(inchikey=standard_inchikey))
        except ResourceNotFound as exc:
            raise MoleculeLibraryError("PROVIDER_NOT_FOUND", str(exc)) from exc
        except RateLimited as exc:
            raise MoleculeLibraryError("PROVIDER_RATE_LIMITED", str(exc)) from exc
        except (InvalidRequest, InvalidUpstreamResponse) as exc:
            raise MoleculeLibraryError("PROVIDER_INVALID_RESPONSE", str(exc)) from exc
        except (UpstreamError, ProviderUnavailable, ProviderError) as exc:
            raise MoleculeLibraryError("PROVIDER_UNAVAILABLE", str(exc)) from exc

        if not candidates:
            raise MoleculeLibraryError(
                "PROVIDER_NOT_FOUND",
                f"PubChem holds no entry for InChIKey {standard_inchikey!r}")

        candidate = candidates[0]
        returned = candidate.inchikey or ""
        match = bool(returned) and returned.upper() == (standard_inchikey or "").upper()
        return PubChemMoleculeResult(
            provider_record_id=candidate.source_id or "",
            standard_inchi=candidate.inchi or "",
            standard_inchikey=returned,
            canonical_smiles=candidate.canonical_smiles or "",
            molecular_formula=candidate.molecular_formula,
            molecular_weight=candidate.molecular_weight,
            iupac_name=candidate.candidate_name or "",
            external_identifiers={"pubchem": {"cid": candidate.source_id or ""}},
            source_url=candidate.source_url or "",
            identity_match=match,
            requested_inchikey=standard_inchikey,
            diagnostics=[] if match else [
                f"returned InChIKey {returned!r} does not match requested "
                f"{standard_inchikey!r}; not imported"])


__all__ = ["PubChemExactProvider"]

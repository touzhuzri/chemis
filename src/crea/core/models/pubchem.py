"""A verified PubChem exact-structure result (CREA Stage 2.3).

The provider's answer, converted into CREA's own model -- never the raw PUG
REST JSON. `identity_match` is the whole point of the stage (§5): the returned
InChIKey was compared against the requested one; when it differs the result
still carries both, never silently "fixed". PubChem's CID is an external
identifier, not CREA's chemical identity (§6).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from crea.core.provenance import utc_now_iso


class PubChemMoleculeResult(BaseModel):
    provider: str = "pubchem"
    provider_record_id: str = ""          # PubChem CID
    standard_inchi: str = ""
    standard_inchikey: str = ""
    canonical_smiles: str = ""
    molecular_formula: str | None = None
    molecular_weight: float | None = None
    #: PubChem's Title (its preferred name, usually the IUPAC name).
    iupac_name: str = ""
    external_identifiers: dict = Field(default_factory=dict)
    source_url: str = ""
    retrieved_at: str = Field(default_factory=utc_now_iso)
    identity_match: bool = True
    requested_inchikey: str = ""
    diagnostics: list[str] = Field(default_factory=list)


__all__ = ["PubChemMoleculeResult"]

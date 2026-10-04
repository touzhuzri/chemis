"""NCI Chemical Identifier Resolver (CIR). The widest name/identifier
resolver verified so far -- and gated (CREA 4.0 Stage B-1).

Stage A verified this endpoint live (name/SMILES/InChI/InChIKey/CAS
round-trips; `phenylacetylene -> C#Cc1ccccc1`, `536-74-3 -> C#Cc1ccccc1`),
without a key. Its **service terms could not be obtained** (the site blocks
automated access; docs/LICENSES.md row is DEFERRED), so nothing in the
default chain touches it. The adapter exists so the moment a human clears
the terms, wiring it in is one constructor argument.

Held to the same contract as every source here: it requests, parses,
attributes and maps errors. It does not decide identity (§10) -- what it
returns are records with provenance, and the semantic layer judges them.
"""

from __future__ import annotations

from crea.core.models.identity import ChemicalIdentityCandidate, MatchedBy
from crea.sources.base import Transport

#: Verified live in Stage A. /chemical/structure/{name}/{representation}
STRUCTURE_ENDPOINT = "https://cactus.nci.nih.gov/chemical/structure"

#: Representations this adapter asks for when turning a hit into a
#: candidate. SMILES and standard InChI give the chem layer everything it
#: needs to derive the rest deterministically.
_REPS = ("smiles", "stdinchi")


class CIRSource:
    """One resolver, behind the shared Transport (offline-testable)."""

    name = "nci_cir"

    def __init__(self, transport: Transport | None = None) -> None:
        self._transport = transport

    def lookup_name(self, name: str) -> list[ChemicalIdentityCandidate]:
        """What CIR holds for `name`, as candidate records.

        Absence (`404`/empty) is `[]`; failure to ask raises. CIR answers
        one preferred structure per name; a name it maps several ways is
        left for the chain's ambiguity rule, which this adapter does not
        pre-empt.
        """
        if self._transport is None:
            raise RuntimeError(
                "CIRSource needs a Transport; it is opted into, never "
                "constructed implicitly")
        smiles = self._get(name, "smiles")
        if not smiles:
            return []
        inchi = self._get(name, "stdinchi")
        candidate = ChemicalIdentityCandidate(
            candidate_id=f"cir-{name}",
            source="NCI_CIR", source_id=name,
            candidate_name=name,
            canonical_smiles=smiles,
            inchi=inchi or "",
            matched_by=MatchedBy.PRIMARY_NAME)
        return [candidate]

    def _get(self, name: str, representation: str) -> str | None:
        import asyncio

        url = f"{STRUCTURE_ENDPOINT}/{name}/{representation}"
        response = asyncio.run(self._transport.request("GET", url))
        status = getattr(response, "status_code", 0)
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(
                f"CIR answered HTTP {status} for {representation}")
        payload = getattr(response, "payload", None)
        text = payload if isinstance(payload, str) else ""
        return text.strip() or None


__all__ = ["CIRSource", "STRUCTURE_ENDPOINT"]

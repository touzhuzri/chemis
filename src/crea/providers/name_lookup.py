"""Name-lookup providers: thin adapters, "what can this source find?" (§8).

CREA 4.0 Stage B-1. Each adapter answers exactly one question -- give me
this name -- and hands candidates up with their provenance. No ranking,
no picking, no identity claims (§10): a source is an External Identity
Provider, and the semantic layer judges what its records are worth.

Three adapters, one seam (`NameLookupProvider`):

* `CompoundNameLookup` -- any `CompoundProvider` (the canonical PubChem
  integration lives in `crea.providers.pubchem`; tests use the captured
  fake). The compound provider protocol is the existing seam; this is the
  thinnest possible bridge to it.
* `OpsinNameLookup` -- the isolated OPSIN worker (systematic nomenclature,
  deterministic parser). Unavailable runtime is a *diagnostic*, not a
  veto: the chain continues (§9).
* `NCICIRNameLookup` -- the NCI Chemical Identifier Resolver, kept out of
  the default chain until its service terms are cleared (Stage A,
  docs/LICENSES.md). Opt-in only.
"""

from __future__ import annotations

from crea.core.models.identity import ChemicalIdentityCandidate, MatchedBy
from crea.providers.base import CompoundQuery


class CompoundNameLookup:
    """A `CompoundProvider` seen through the name-lookup seam."""

    def __init__(self, compound_provider, name: str | None = None) -> None:
        self._provider = compound_provider
        self.name = name or getattr(compound_provider, "name", "compound")

    def lookup_name(self, name: str):
        from crea.semantic.naming import NameLookupHit, NameLookupResult

        try:
            records = list(self._provider.lookup(CompoundQuery(name=name)))
        except Exception:
            # Absence is not failure (§8): a raise means "could not ask",
            # and it travels up to the chain as a diagnostic.
            raise
        hits = []
        for record in records:
            if not isinstance(record, ChemicalIdentityCandidate):
                continue
            hits.append(NameLookupHit(
                candidate=record,
                matched_by=record.matched_by or MatchedBy.PRIMARY_NAME,
                matched_synonym=record.matched_synonym))
        return NameLookupResult(provider=self.name, hits=hits)


class OpsinNameLookup:
    """OPSIN (systematic name -> structure) over the engine worker (§9)."""

    name = "opsin"

    def __init__(self, reader=None) -> None:
        self._reader = reader

    def lookup_name(self, name: str):
        from crea.semantic.naming import NameLookupHit, NameLookupResult

        reader = self._reader
        if reader is None:
            from crea.providers.worker_opsin import OpsinNameReader

            reader = OpsinNameReader()
        reading = reader.read(name)
        if not reading.get("parsed"):
            return NameLookupResult(provider=self.name, hits=[])
        smiles = reading.get("smiles") or ""
        candidate = ChemicalIdentityCandidate(
            candidate_id=f"opsin-{name}",
            source="OPSIN", source_id=name,
            candidate_name=name,
            canonical_smiles=smiles,
            matched_by=MatchedBy.PRIMARY_NAME)
        return NameLookupResult(
            provider=self.name,
            hits=[NameLookupHit(candidate=candidate,
                                matched_by=MatchedBy.PRIMARY_NAME)])


class NCICIRNameLookup:
    """NCI CIR name -> records. Opt-in: service terms pending (§10/LICENSES).

    Kept out of the default chain until a human has read the CIR terms of
    use. What it returns is the same shape as everyone else: candidates
    with source stamps, never decisions.
    """

    name = "nci_cir"

    def __init__(self, source=None, transport=None) -> None:
        self._source = source
        self._transport = transport

    def lookup_name(self, name: str):
        from crea.semantic.naming import NameLookupHit, NameLookupResult

        source = self._source
        if source is None:
            from crea.sources.cir import CIRSource

            source = CIRSource(transport=self._transport)
        records = source.lookup_name(name)
        hits = []
        for record in records:
            if isinstance(record, ChemicalIdentityCandidate):
                hits.append(NameLookupHit(
                    candidate=record,
                    matched_by=record.matched_by or MatchedBy.SYNONYM,
                    matched_synonym=record.matched_synonym))
        return NameLookupResult(provider=self.name, hits=hits)


__all__ = ["CompoundNameLookup", "NCICIRNameLookup", "OpsinNameLookup"]

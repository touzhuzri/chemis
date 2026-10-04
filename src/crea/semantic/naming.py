"""Names: normalization, the provider seam, and the resolver chain (§8/§12).

A chemical name is a word. This module turns words into *candidates* and
keeps honest books about where each candidate came from. It never decides
a name means a structure: that decision is the identity layer's, on
evidence, and when several distinct structures survive the answer is
AMBIGUOUS and the choice is a person's.

Responsibility split (§8):

* **NameResolver** -- dispatch, dedup (via the identity layer's one
  `_distinct_structures` rule), ambiguity, evidence level, provenance.
* **NameLookupProvider** -- "give me this name, what can you find?"  and
  nothing else. No ranking, no picking, no identity claims.

Providers are injected at the composition roots (§ layering): this module
never imports a network client. A provider that cannot be consulted is a
*diagnostic*, not a veto -- the chain continues (Test 10), and if nothing
survives at all the answer says which providers failed and why.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from crea.core.models.identity import (
    ChemicalIdentityCandidate,
    ChemicalIdentityResolution,
    IdentityEvidenceLevel,
    MatchedBy,
    StructureInput,
    StructureInputKind,
    StructureResolutionStatus,
)
from crea.core.naming import normalize_for_search


@runtime_checkable
class NameLookupProvider(Protocol):
    """A source that can turn a name into candidate structures."""

    name: str

    def lookup_name(self, name: str) -> "NameLookupResult":
        """Find what this source holds for `name`.

        Absence is `hits=[]` and must not be raised; failure to *ask*
        raises `ProviderUnavailable`/`ProviderError` (or returns them in
        `errors`), and is never disguised as absence.
        """
        ...


class NameLookupHit(BaseModel):
    """One record a source found for a name."""

    candidate: ChemicalIdentityCandidate
    #: Which string reached the record (primary name, a synonym, ...).
    matched_by: MatchedBy = MatchedBy.PRIMARY_NAME
    matched_synonym: str | None = None


class NameLookupResult(BaseModel):
    """What one source could find -- never more."""

    provider: str
    hits: list[NameLookupHit] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class NameResolutionAnswer(BaseModel):
    """The chain's answer for one name, with its books in order (§17)."""

    name: str
    normalized_name: str
    status: StructureResolutionStatus
    candidates: list[ChemicalIdentityCandidate] = Field(default_factory=list)
    selected: ChemicalIdentityCandidate | None = None
    identity_evidence: IdentityEvidenceLevel = IdentityEvidenceLevel.UNRESOLVED
    #: which provider produced which candidate, by candidate_id
    provider_results: list[dict] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    resolution: ChemicalIdentityResolution | None = None


class NameResolver:
    """The name half of the semantic spine."""

    def __init__(self, providers) -> None:
        self._providers = list(providers)

    @property
    def provider_names(self) -> list[str]:
        return [getattr(p, "name", type(p).__name__) for p in self._providers]

    def resolve_name(self, name: str, *, input_obj: StructureInput | None = None
                     ) -> NameResolutionAnswer:
        normalized = normalize_for_search(name)
        answer = NameResolutionAnswer(
            name=(name or "").strip(), normalized_name=normalized,
            status=StructureResolutionStatus.NOT_FOUND)
        if not normalized:
            answer.status = StructureResolutionStatus.INVALID
            answer.diagnostics.append("empty chemical name")
            return answer

        # -- dispatch (§8: the resolver schedules; providers only find)
        hits: list[NameLookupHit] = []
        for provider in self._providers:
            provider_name = getattr(provider, "name", type(provider).__name__)
            try:
                result = provider.lookup_name(normalized)
            except Exception as exc:  # noqa: BLE001 - a dead provider is data
                answer.diagnostics.append(
                    f"{provider_name} could not be consulted: "
                    f"{type(exc).__name__}: {exc}")
                answer.provider_results.append(
                    {"provider": provider_name, "status": "unavailable",
                     "error": f"{type(exc).__name__}: {exc}"})
                continue
            kept = list(result.hits)
            hits.extend(kept)
            answer.provider_results.append({
                "provider": provider_name,
                "status": "ok" if kept else "absent",
                "hits": [h.candidate.candidate_id for h in kept],
                "errors": list(result.errors),
            })
            answer.diagnostics.extend(
                f"{provider_name}: {e}" for e in result.errors)

        # -- materialize, then dedup: the identity layer's ONE rule (§5).
        # Every provider structure is parsed and NORMALIZED before any
        # comparison -- two providers spelling the same molecule
        # differently (`C1(=CC=CC=C1)C#C` vs `C#Cc1ccccc1`) are one
        # structure, and string equality is not an identity judgement.
        from crea.services.identity import _distinct_structures

        candidates: list[ChemicalIdentityCandidate] = []
        for hit in hits:
            candidate = hit.candidate.model_copy(deep=True)
            if hit.matched_by is not MatchedBy.UNKNOWN:
                candidate.matched_by = hit.matched_by
            if hit.matched_synonym:
                candidate.matched_synonym = hit.matched_synonym
            if candidate.input_name is None:
                candidate.input_name = answer.name
            materialized = self._materialize(candidate, answer)
            if materialized is not None:
                candidates.append(materialized)
        candidates = _distinct_structures(candidates)
        answer.candidates = candidates
        answer.provider_results.append(
            {"provider": "chain", "status": "merged",
             "distinct_structures": len(candidates)})

        # -- ambiguity, evidence, provenance (§8: the resolver decides)
        if not candidates:
            answer.diagnostics.append(
                "no consulted source holds a structure for this name")
            if answer.diagnostics and not any(
                    p.get("status") == "ok" for p in answer.provider_results
                    if p["provider"] != "chain"):
                answer.status = StructureResolutionStatus.UNAVAILABLE
            else:
                answer.status = StructureResolutionStatus.NOT_FOUND
            answer.identity_evidence = IdentityEvidenceLevel.UNRESOLVED
            answer.resolution = self._as_resolution(input_obj, answer)
            return answer

        if len(candidates) > 1:
            # Never candidates[0] (§4/§24): the list IS the answer.
            answer.status = StructureResolutionStatus.AMBIGUOUS
            answer.identity_evidence = IdentityEvidenceLevel.AMBIGUOUS
            answer.diagnostics.append(
                f"{len(candidates)} distinct structures matched; "
                "no rule applies a choice (never pick candidate zero)")
            answer.resolution = self._as_resolution(input_obj, answer)
            return answer

        answer.selected = candidates[0]
        answer.status = StructureResolutionStatus.RESOLVED
        answer.identity_evidence = self._evidence_for(candidates[0])
        answer.resolution = self._as_resolution(input_obj, answer)
        return answer

    @staticmethod
    def _materialize(candidate: ChemicalIdentityCandidate,
                     answer: NameResolutionAnswer) -> ChemicalIdentityCandidate | None:
        """Give a candidate its normalized identity, or drop it honestly.

        RDKit (through the chem layer) parses the provider's structure and
        re-derives canonical SMILES/InChI/InChIKey; the provider's spelling
        is kept in `raw`. A candidate whose structure does not parse is
        dropped with a diagnostic -- never carried along looking like a
        structure (§3).
        """
        smiles = (candidate.canonical_smiles or "").strip()
        inchi = (candidate.inchi or "").strip()
        try:
            from crea.chem.normalize import normalize, normalize_inchi

            if smiles:
                molecule = normalize(smiles, source="user_input")
            elif inchi:
                molecule = normalize_inchi(inchi, source="user_input")
            else:
                answer.diagnostics.append(
                    f"candidate from {candidate.source or 'unknown'} carries "
                    "no structure; dropped")
                return None
        except Exception:  # noqa: BLE001 - a non-structure is data, not a crash
            answer.diagnostics.append(
                f"candidate from {candidate.source or 'unknown'} did not "
                "parse as a structure; dropped")
            return None
        if smiles and smiles != molecule.canonical_smiles:
            candidate.raw.setdefault("provider_smiles", smiles)
        candidate.canonical_smiles = molecule.canonical_smiles
        candidate.inchi = molecule.inchi
        candidate.inchikey = molecule.inchikey
        return candidate

    @staticmethod
    def _evidence_for(candidate: ChemicalIdentityCandidate) -> IdentityEvidenceLevel:
        """Name chains earn SUPPORTED at best; a synonym is PROBABLE (§4)."""
        if candidate.matched_by in (MatchedBy.SYNONYM, MatchedBy.UNKNOWN,
                                    MatchedBy.IMAGE_RECOGNITION):
            return IdentityEvidenceLevel.PROBABLE
        return IdentityEvidenceLevel.SUPPORTED

    @staticmethod
    def _as_resolution(input_obj: StructureInput | None,
                       answer: NameResolutionAnswer) -> ChemicalIdentityResolution:
        """The answer in the existing resolution vocabulary (no second model)."""
        inp = input_obj or StructureInput(
            kind=StructureInputKind.NAME, value=answer.name)
        return ChemicalIdentityResolution(
            status=answer.status,
            input=inp,
            candidates=list(answer.candidates),
            selected=answer.selected,
            reason="; ".join(answer.diagnostics) or answer.status.value,
            codes=[answer.status.value],
        )


__all__ = [
    "NameLookupHit",
    "NameLookupProvider",
    "NameLookupResult",
    "NameResolutionAnswer",
    "NameResolver",
]

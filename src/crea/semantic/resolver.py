"""The unified resolver: every chemical input enters ChemicalEntity here.

CREA 4.0 Stage B-1 (§6/§7/§17). One door:

    ChemicalInput -> SemanticResolver -> ResolutionResult -> ChemicalEntity

The input is **classified explicitly** -- a structure notation, a name,
an identifier, an image -- and the path follows the classification. There
is no `resolve("xxx")` that guesses what the string is: guessing the
input type is the first step toward guessing the structure.

The result is a `ResolutionResult`: resolution status (how far it got)
and identity evidence (what the claim rests on) are two different
dimensions and both are always present, along with every provider's
answer, the deterministic resolution in the existing vocabulary, and
diagnostics. `ChemicalEntity | None` is never the whole answer.

Layers below do the chemistry: RDKit parses, the identity service and
its facet rules decide sameness, providers supply records. This module
classifies, delegates, collects, and registers -- and when the evidence
says AMBIGUOUS it hands the question back.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.models.entity import ChemicalEntity
from crea.core.models.identity import (
    ChemicalIdentityResolution,
    IdentityEvidenceLevel,
    StructureInput,
    StructureInputKind,
    StructureResolutionStatus,
)
from crea.semantic.naming import NameResolver


class InputKind(str, Enum):
    """What kind of thing the user gave us (§7). Explicit, always."""

    #: The string IS a structure notation (SMILES, InChI, molblock).
    STRUCTURE = "structure"
    #: The string is a chemical name -- a word, not a structure.
    NAME = "name"
    #: The string is an identifier in some scheme (InChIKey, PubChem CID).
    IDENTIFIER = "identifier"
    #: An image of a structure (later stages; classified, not guessed).
    IMAGE = "image"


class ResolutionStatus(str, Enum):
    """How far resolution got (§17). A different question from how strong
    the evidence is -- both are always reported."""

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    INVALID = "invalid"


_STATUS_FOR = {
    StructureResolutionStatus.RESOLVED: ResolutionStatus.RESOLVED,
    StructureResolutionStatus.AMBIGUOUS: ResolutionStatus.AMBIGUOUS,
    StructureResolutionStatus.INVALID: ResolutionStatus.INVALID,
}
#: Everything that failed to establish an identity collapses to UNRESOLVED
#: at the result level, while `resolution` keeps the fine-grained reason
#: (NOT_FOUND vs UNAVAILABLE are different facts and stay different there).
_FAILING_STATUSES = {
    StructureResolutionStatus.NOT_FOUND: ResolutionStatus.UNRESOLVED,
    StructureResolutionStatus.UNAVAILABLE: ResolutionStatus.UNRESOLVED,
    StructureResolutionStatus.INPUT: ResolutionStatus.UNRESOLVED,
}


_KIND_FOR_INPUT = {
    StructureInputKind.SMILES: InputKind.STRUCTURE,
    StructureInputKind.INCHI: InputKind.STRUCTURE,
    StructureInputKind.INCHIKEY: InputKind.IDENTIFIER,
    StructureInputKind.PUBCHEM_CID: InputKind.IDENTIFIER,
    StructureInputKind.NAME: InputKind.NAME,
    StructureInputKind.IMAGE: InputKind.IMAGE,
}


class ChemicalInput(BaseModel):
    """A chemical input, with its kind stated (§7).

    A thin, explicit wrapper over the input vocabulary the identity layer
    already has. `StructureInput` travels underneath unchanged -- this
    names the *category* so the resolver never has to infer it. A
    molblock (future clipboard representation) enters through
    `of_notation` and is parsed by the chem layer, never guessed at.
    """

    kind: InputKind
    value: str = ""
    notation: str = ""  # "" | "smiles" | "inchi" | "molblock"
    structure_input: StructureInput | None = None

    @classmethod
    def of(cls, kind: str, value: str) -> "ChemicalInput":
        """Build from a user/API kind string. The kind is data, not a guess."""
        structure = StructureInput.of(kind=kind, value=value)
        return cls(kind=_KIND_FOR_INPUT[structure.kind], value=value,
                   structure_input=structure)

    @classmethod
    def of_notation(cls, notation: str, value: str) -> "ChemicalInput":
        """A structural notation the identity vocabulary has no kind for
        (molblock today; CDXML tomorrow). Explicit, always."""
        notation = (notation or "").strip().lower()
        if notation == "molblock":
            return cls(kind=InputKind.STRUCTURE, value=value, notation="molblock")
        if notation == "smiles":
            return cls(kind=InputKind.STRUCTURE, value=value, notation="smiles",
                       structure_input=StructureInput.of(kind="SMILES", value=value))
        if notation == "inchi":
            return cls(kind=InputKind.STRUCTURE, value=value, notation="inchi",
                       structure_input=StructureInput.of(kind="INCHI", value=value))
        raise ValueError(f"unknown notation: {notation!r}")

    @classmethod
    def from_structure_input(cls, structure: StructureInput) -> "ChemicalInput":
        return cls(kind=_KIND_FOR_INPUT[structure.kind], value=structure.value,
                   structure_input=structure)


class ProviderIdentityResult(BaseModel):
    """What one external source said. A record, never a verdict (§10)."""

    provider: str
    status: str = ""
    hits: list[str] = Field(default_factory=list)
    error: str = ""


class ResolutionResult(BaseModel):
    """The resolver's whole answer (§17): status AND evidence AND books."""

    status: ResolutionStatus = ResolutionStatus.UNRESOLVED
    input_kind: InputKind = InputKind.NAME
    input_value: str = ""
    entity: ChemicalEntity | None = None
    candidates: list = Field(default_factory=list)
    identity_evidence: IdentityEvidenceLevel = IdentityEvidenceLevel.UNRESOLVED
    provider_results: list[ProviderIdentityResult] = Field(default_factory=list)
    resolution: ChemicalIdentityResolution | None = None
    diagnostics: list[str] = Field(default_factory=list)


class SemanticResolver:
    """Classify -> delegate -> collect -> register. Nothing else."""

    def __init__(self, *, identity_service=None, name_resolver=None,
                 entity_registry=None) -> None:
        self._identity = identity_service
        self._names = name_resolver
        self._registry = entity_registry

    def bind_entity_registry(self, registry) -> None:
        """Attach the entity registry the mint-or-reuse step writes to.

        Composition roots (and `build_registry`) call this so the spine
        and the tools share ONE registry -- without it a resolved identity
        would have no door to become a ChemicalEntity.
        """
        if self._registry is None:
            self._registry = registry

    # -- resolution ---------------------------------------------------------

    def resolve(self, chemical_input: ChemicalInput) -> ResolutionResult:
        """Resolve one classified input to identity + entity (or say why)."""
        result = ResolutionResult(input_kind=chemical_input.kind,
                                 input_value=chemical_input.value)
        structure = chemical_input.structure_input
        if structure is None and chemical_input.notation != "molblock":
            result.status = ResolutionStatus.INVALID
            result.diagnostics.append("input carries no structure notation")
            return result

        if chemical_input.kind is InputKind.NAME:
            self._resolve_name(chemical_input, result)
        else:
            self._resolve_structured(chemical_input, result)

        if result.status is ResolutionStatus.RESOLVED:
            self._establish_entity(result)
        return result

    def resolve_entity(self, chemical_input: ChemicalInput) -> ResolutionResult:
        """Resolve and make sure a ChemicalEntity exists for the answer.

        Reuse is the point: the same identity returns the same entity
        (Stage B-1 Test 2/6), and representations accumulate on it rather
        than forking it.
        """
        return self.resolve(chemical_input)

    # -- paths --------------------------------------------------------------

    def _resolve_structured(self, chemical_input: ChemicalInput,
                            result: ResolutionResult) -> None:
        """Structure notation / identifier: the deterministic path (§3)."""
        if self._identity is None:
            result.status = ResolutionStatus.UNRESOLVED
            result.diagnostics.append("no identity service is configured")
            return
        structure = chemical_input.structure_input
        if structure is None and chemical_input.notation == "molblock":
            # The molblock is parsed by the chem layer and re-enters the
            # deterministic path through its canonical SMILES: one identity
            # system, one parser, no side door (§5).
            try:
                from crea.chem.representations import molecule_from_molblock

                molecule = molecule_from_molblock(chemical_input.value)
            except Exception as exc:  # noqa: BLE001
                result.status = ResolutionStatus.INVALID
                result.diagnostics.append(f"molblock did not parse: {exc}")
                return
            structure = StructureInput.of(kind="SMILES",
                                          value=molecule.canonical_smiles)
        resolution = self._identity.resolve(structure)
        result.resolution = resolution
        result.status = _result_status(resolution.status)
        result.candidates = list(resolution.candidates)
        from crea.semantic.identity import identity_evidence_level

        result.identity_evidence = identity_evidence_level(resolution)
        result.provider_results.append(ProviderIdentityResult(
            provider="deterministic" if chemical_input.kind is InputKind.STRUCTURE
            else "identifier-lookup",
            status=resolution.status.value,
            hits=[c.candidate_id for c in resolution.candidates]))
        if resolution.reason:
            result.diagnostics.append(resolution.reason)

    def _resolve_name(self, chemical_input: ChemicalInput,
                      result: ResolutionResult) -> None:
        """Name: the provider chain, then the rules (§8)."""
        if self._names is None:
            result.status = ResolutionStatus.UNRESOLVED
            result.diagnostics.append("no name resolver is configured")
            return
        answer = self._names.resolve_name(
            chemical_input.value, input_obj=chemical_input.structure_input)
        result.resolution = answer.resolution
        result.status = _result_status(answer.status)
        result.candidates = list(answer.candidates)
        result.identity_evidence = answer.identity_evidence
        result.diagnostics.extend(answer.diagnostics)
        result.provider_results.extend(
            ProviderIdentityResult(**entry) if "provider" in entry else
            ProviderIdentityResult(provider="chain", status="merged")
            for entry in answer.provider_results)

    # -- entity -------------------------------------------------------------

    def _establish_entity(self, result: ResolutionResult) -> None:
        """Mint or reuse the ChemicalEntity; grow its representations."""
        resolution = result.resolution
        if resolution is None or self._registry is None:
            return
        from crea.core.models.entity import EntityRegistrationError

        try:
            entity = self._registry.register_resolution(resolution)
        except EntityRegistrationError as exc:
            result.diagnostics.append(f"entity not registered: {exc}")
            return
        self.enrich_entity(entity, result.resolution)
        result.entity = entity

    def enrich_entity(self, entity: ChemicalEntity,
                      resolution: ChemicalIdentityResolution | None) -> None:
        """Add typed, sourced representations and per-field provenance.

        Accumulation, never replacement: a representation the entity
        already carries stays; a new one is added with its source (§3).
        Called on every registration path (mine or the tool's), so the
        representations never depend on which door the entity came in.
        """
        from crea.core.provenance import Provenance, utc_now_iso
        from crea.semantic.representations import (
            RepresentationError,
            build_representations,
        )

        selected = resolution.selected if resolution else None
        smiles = (selected.canonical_smiles if selected else "") or ""
        if not smiles:
            return
        try:
            from crea.chem.normalize import normalize

            molecule = normalize(smiles, source="user_input")
            fresh = build_representations(
                molecule,
                source=(selected.source if selected else "user_input") or "user_input",
                source_id=(selected.source_id if selected else ""))
        except Exception:  # noqa: BLE001 - unparseable is not fatal here
            return
        existing = {(r.kind, r.value) for r in entity.representations}
        now = utc_now_iso()
        for representation in fresh:
            if (representation.kind, representation.value) in existing:
                continue
            entity.representations.append(representation)
            entity.field_provenance[representation.kind] = Provenance(
                tool="semantic-resolver", tool_version="crea4-b1",
                source=representation.source,
                source_id=representation.source_id or None,
                computed_at=representation.retrieved_at or now)


def _result_status(status: StructureResolutionStatus) -> ResolutionStatus:
    return (_STATUS_FOR.get(status)
            or _FAILING_STATUSES.get(status)
            or ResolutionStatus.UNRESOLVED)


__all__ = [
    "ChemicalInput",
    "InputKind",
    "ProviderIdentityResult",
    "ResolutionResult",
    "ResolutionStatus",
    "SemanticResolver",
]

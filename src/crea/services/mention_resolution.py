"""Chemical mention -> chemical entity, by identity and never by guess.

Phase 3.8.2. A paper writes `phenylacetylene`, `ethynylbenzene`, `C#Cc1ccccc1`,
`PubChem CID 733`, `compound 7`. This layer turns such a mention into an
entity the registry already holds or can establish -- or refuses, loudly:

```
ChemicalMention
      │
      ├── explicit structure (user-attached SMILES/InChI)
      │       → identity service → entity registry → CHEM-XXXXXXXX
      ├── external id (PubChem CID) — registry first, then the provider
      │       → identity service → entity registry → CHEM-XXXXXXXX
      ├── registry name match (the entity already carries that name)
      │       → CHEM-XXXXXXXX, with no network call at all
      ├── numbered reference (compound 7) → paper context
      │       → definition → identity service → CHEM-XXXXXXXX
      │       → no context → UNRESOLVED
      └── plain name → trusted provider (PubChem)
              → one structure → CHEM-XXXXXXXX
              → several      → AMBIGUOUS (candidates kept, none chosen)
              → none         → NOT_FOUND
              → unreachable  → UNRESOLVED
```

Two rules the ordering exists for. **Precision beats recall**: an
unresolved mention is a correct answer, a wrongly bound one poisons every
reaction built on it later (§45). And **the cheap path first**: a name that
already sits on a registered entity resolves from the registry, without
consulting a provider on every occurrence (§9).

What is never here: an LLM deciding a structure (this module does not import
one and cannot be handed one), `candidates[0]` for an ambiguous name, a
similarity threshold binding a mention, or `compound 7` becoming a permanent
entity name. Numbered references are paper-local (§17): "compound 7" in one
paper may be another compound in the next, and its context travels with it.

RDKit is reached only through the identity service; providers only through
the `CompoundProvider` the identity service already uses. This module adds
no HTTP client and no second identity hash -- the key is Phase 3.8.1's
`crea-identity-key-v1`.
"""

from __future__ import annotations

from crea.core.naming import normalize_for_search

import re
from typing import Iterable, Protocol

from crea.core.ids import content_hash
from crea.core.models.entity import (
    ChemicalMention,
    EntityRegistrationError,
    MentionResolutionStatus,
)
from crea.core.models.identity import (
    StructureInput,
    StructureInputKind,
    StructureResolutionStatus,
)
from crea.core.models.mention_resolution import (
    MENTION_RESOLUTION_VERSION,
    ChemicalMentionResolution,
    ContextReference,
    MentionCandidateInfo,
    MentionResolutionBasis,
    MentionResolutionEvidence,
)
from crea.core.provenance import Provenance

#: Numbered references are keywords plus a number: "compound 7", "compound
#: 7a", "intermediate 3b", "product 4", "substrate 8". Case insensitive.
NUMBERED_REFERENCE_RE = re.compile(
    r"\b(?:compound|intermediate|product|substrate)\s+(\d+[a-z]?)\b",
    re.IGNORECASE)

#: An external identifier written into the text: "PubChem CID 733".
PUBCHEM_CID_RE = re.compile(r"\bPubChem\s+CID\s+(\d+)\b", re.IGNORECASE)

#: Experiment/analysis words that are not chemicals, no matter how
#: convincing their capitals look (§22). Deliberately small: a name matcher
#: only ever matches names it was given, so this set exists to refuse the
#: obvious, not to be a dictionary.
ANALYTICS_EXCLUSIONS = frozenset({"NMR", "HPLC", "GC", "TLC", "MS", "IR", "UV"})

_PUBCHEM_NAMESPACE = "pubchem"


def is_numbered_reference(text: str) -> bool:
    """Is this mention text a paper-local numbered reference?

    Whole-text test: an extracted mention's `raw_text` is exactly the matched
    span (`compound 7`, `compound 7a`), so a longer sentence is not a
    reference to itself.
    """
    return bool(NUMBERED_REFERENCE_RE.fullmatch((text or "").strip()))


def extract_chemical_mentions(
    text: str,
    *,
    known_names: Iterable[str] = (),
    source: str = "",
    source_id: str = "",
) -> list[ChemicalMention]:
    """Lightweight deterministic mention extraction (§20).

    Three pattern families, nothing else: numbered references
    (`compound 7` / `intermediate 3b` / ...), external identifiers
    (`PubChem CID 733`), and names the caller already trusts (`known_names`
    -- entity names, provider-resolved names, explicitly supplied
    candidates). No chemical NLP, no dictionary guessing, no
    capitalisation heuristics.

    §21: bare numbers with units (`7 mL`, `7 h`, `80 °C`, `82%`) match
    nothing here -- a number is only a mention when a keyword says so.
    §22: `NMR`, `HPLC`, `GC`, `TLC`, `MS`, `IR`, `UV` are refused even if
    supplied as known names.
    """
    spans: list[tuple[int, int, str]] = []  # start, end, kind

    for match in NUMBERED_REFERENCE_RE.finditer(text):
        spans.append((match.start(), match.end(), "numbered"))
    for match in PUBCHEM_CID_RE.finditer(text):
        spans.append((match.start(), match.end(), "cid"))

    for name in sorted({(n or "").strip() for n in known_names if (n or "").strip()},
                       key=len, reverse=True):
        if name.upper() in ANALYTICS_EXCLUSIONS:
            continue
        for match in re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)",
                                 text, re.IGNORECASE):
            if match.group(0).upper() in ANALYTICS_EXCLUSIONS:
                continue
            spans.append((match.start(), match.end(), "name"))

    # keep the earliest, longest match at any position: "compound 7" wins
    # over a known name that happens to sit inside it
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    kept: list[tuple[int, int, str]] = []
    for span in spans:
        if any(span[0] < k[1] and k[0] < span[1] for k in kept):
            continue
        kept.append(span)

    mentions: list[ChemicalMention] = []
    from crea.core.models.entity import MAX_CONTEXT_CHARS

    for start, end, kind in kept:
        raw = text[start:end]
        mentions.append(ChemicalMention(
            mention_id=content_hash(f"{source_id}|{start}|{raw}", 16),
            raw_text=raw,
            normalized_text=normalize_for_search(raw),
            source=source,
            source_id=source_id,
            context_before=text[max(0, start - MAX_CONTEXT_CHARS):start],
            context_after=text[end:end + MAX_CONTEXT_CHARS],
        ))
    return mentions


class MentionContextSource(Protocol):
    """Where a paper-local reference gets its definition (§13/§14)."""

    def resolve_reference(self, mention: ChemicalMention) -> ContextReference | None: ...


def _parenthetical_after(text: str) -> str | None:
    """The first bracket pair after the mention, brackets balanced (§13).

    A definition like `Compound 8 (InChI=1S/...(10-8-13)...)` nests
    parentheses; a shallow pattern would stop at the first close and hand
    the resolver a fragment of an InChI.
    """
    index = text.find("(")
    if index < 0:
        return None
    depth = 0
    for position in range(index, min(len(text), index + 240)):
        if text[position] == "(":
            depth += 1
        elif text[position] == ")":
            depth -= 1
            if depth == 0:
                return text[index + 1:position]
    return None


def _parenthetical_before(text: str) -> str | None:
    """The last bracket pair before the mention, brackets balanced."""
    index = text.rstrip().rfind(")")
    if index < 0:
        return None
    depth = 0
    for position in range(index, max(-1, index - 240), -1):
        if text[position] == ")":
            depth += 1
        elif text[position] == "(":
            depth -= 1
            if depth == 0:
                return text[position + 1:index]
    return None


class ParentheticalContext:
    """`Compound 7 (phenylacetylene)` -- the parenthetical beside the reference.

    Deterministic text lookup, nothing more: the bracket pair immediately
    after (or before) the mention is read as its definition. `InChI=...`
    inside the brackets is a structure; anything else is a name. A bracket
    holding a guess would be a guess -- so what is found is recorded as a
    `ContextReference` with its quote, and resolution still has to earn the
    entity through the identity service.
    """

    def resolve_reference(self, mention: ChemicalMention) -> ContextReference | None:
        after = mention.context_after or ""
        inner = _parenthetical_after(after)
        quote_prefix = mention.raw_text
        quote_suffix = ""
        if inner is None:
            before = mention.context_before or ""
            inner = _parenthetical_before(before)
            quote_prefix = ""
            quote_suffix = mention.raw_text
        if inner is None:
            return None
        inner = inner.strip()
        if not inner:
            return None
        quote = f"{quote_prefix} ({inner}) {quote_suffix}".strip()
        if inner.startswith("InChI="):
            return ContextReference(inchi=inner, context_quote=quote,
                                    source="context", source_id=mention.source_id,
                                    locator=mention.locator)
        return ContextReference(name=inner, context_quote=quote,
                                source="context", source_id=mention.source_id,
                                locator=mention.locator)


class StaticMentionContext:
    """An explicit reference table, keyed by paper scope (§17).

    `(source_id, reference)` -> definition. The same words in another paper
    are another question: `("", reference)` is the only cross-paper fallback,
    and only for a caller that deliberately supplied one.
    """

    def __init__(self, table: dict[tuple[str, str], ContextReference]) -> None:
        self._table = dict(table)

    def resolve_reference(self, mention: ChemicalMention) -> ContextReference | None:
        reference = normalize_for_search(mention.normalized_text or mention.raw_text)
        return (self._table.get((mention.source_id, reference))
                or self._table.get(("", reference)))


class ChemicalMentionResolver:
    """The pipeline above, end to end. See the module docstring."""

    def __init__(self, registry, *, identity=None,
                 context_sources: Iterable[MentionContextSource] = (),
                 held_evidence: Iterable | None = None) -> None:
        self._registry = registry
        self._identity = identity
        self._context_sources = tuple(context_sources)
        self._held_evidence = (
            {getattr(e, "evidence_id", e) for e in held_evidence}
            if held_evidence is not None else None)

    # -- entry point --------------------------------------------------------

    def resolve(self, mention: ChemicalMention) -> ChemicalMentionResolution:
        evidence = self._mention_evidence(mention)

        # 1. explicit structure: the highest basis there is (§23)
        explicit = (mention.explicit_smiles or "").strip() or (mention.explicit_inchi or "").strip()
        if explicit:
            kind = "SMILES" if (mention.explicit_smiles or "").strip() else "INCHI"
            return self._via_identity(
                mention, StructureInput.of(kind, explicit),
                MentionResolutionBasis.EXPLICIT_STRUCTURE, evidence,
                notes=["explicit structure + RDKit validation + identity key"])

        # 2. an external id written into the text: registry first, provider second
        cid_match = PUBCHEM_CID_RE.search(mention.raw_text or "")
        if cid_match:
            cid = cid_match.group(1)
            existing = self._registry.find_by_external_id(_PUBCHEM_NAMESPACE, cid)
            if existing is not None:
                # the entity's identity key is what the link was established
                # under; without it the key would fall back to the name
                evidence.identity_key = existing.identity_key
                return self._resolved(
                    mention, existing.entity_id,
                    MentionResolutionBasis.REGISTRY_EXTERNAL_ID, evidence,
                    notes=[f"external identifier pubchem:{cid} already on an entity"])
            return self._via_identity(
                mention, StructureInput.of("PUBCHEM_CID", cid),
                MentionResolutionBasis.PROVIDER_EXTERNAL_ID, evidence,
                notes=[f"pubchem:{cid} + identity policy + identity key"])

        # 3. a name the registry already knows: no provider call (§9)
        named = self._registry_entities_named(mention)
        if len(named) == 1:
            evidence.identity_key = named[0].identity_key
            return self._resolved(
                mention, named[0].entity_id,
                MentionResolutionBasis.REGISTRY_NAME_MATCH, evidence,
                notes=["name matched a registered entity's names"])
        if len(named) > 1:
            return self._ambiguous(
                mention, MentionResolutionBasis.REGISTRY_NAME_MATCH, evidence,
                entity_ids=[entity.entity_id for entity in named],
                note="the name matches more than one registered entity")

        # 4. a paper-local numbered reference: context or nothing (§13)
        if is_numbered_reference(mention.raw_text or ""):
            reference = self._first_context(mention)
            if reference is None:
                return self._unresolved(
                    mention, MentionResolutionBasis.UNRESOLVED, evidence,
                    notes=["paper-local reference; its definition is in the "
                           "context (name, scheme, figure, table) and has "
                           "not been provided"])
            return self._via_context(mention, reference, evidence)

        # 5. a plain name: the trusted provider, through identity policy.
        # The name is looked up as the text wrote it; normalisation is for
        # matching, never for re-spelling what the user's paper said.
        return self._via_identity(
            mention,
            StructureInput.of("NAME", (mention.raw_text or "").strip()
                              or mention.normalized_text),
            MentionResolutionBasis.PROVIDER_NAME_MATCH, evidence,
            notes=["provider name match + identity policy + identity key"])

    # -- shared paths -------------------------------------------------------

    def _resolve_spelling(self, structure_input):
        """Look a name up as written, then as its case-folded self.

        "Ethynylbenzene" and "ethynylbenzene" are one name spelled two ways,
        and a source that keys exactly would miss the first. Retrying the
        case-folded spelling is the same name, not a guess: only a NOT_FOUND
        continues to the retry -- anything else (resolved, ambiguous,
        unavailable) is the answer the source gave.
        """
        resolution = self._identity.resolve(structure_input)
        if (structure_input.kind is StructureInputKind.NAME
                and resolution.status is StructureResolutionStatus.NOT_FOUND):
            folded = normalize_for_search(structure_input.value)
            if folded and folded != structure_input.value.strip():
                retry = self._identity.resolve(StructureInput.of("NAME", folded))
                if retry.status is not StructureResolutionStatus.NOT_FOUND:
                    return retry
        return resolution

    def _via_identity(self, mention, structure_input, basis, evidence, *, notes):
        """StructureInput -> identity service -> entity registry. Or refuse."""
        if self._identity is None:
            return self._unresolved(
                mention, MentionResolutionBasis.UNRESOLVED, evidence,
                notes=["no structure source is configured; a name alone "
                       "cannot establish an identity"])
        try:
            resolution = self._resolve_spelling(structure_input)
        except Exception as exc:  # noqa: BLE001 - a source that cannot answer is not an answer
            return self._unresolved(
                mention, MentionResolutionBasis.UNRESOLVED, evidence,
                notes=[f"identity resolution could not run: {type(exc).__name__}"])

        if resolution.is_resolved:
            try:
                entity = self._registry.register_resolution(resolution)
            except EntityRegistrationError as exc:
                return self._unresolved(
                    mention, MentionResolutionBasis.UNRESOLVED, evidence,
                    notes=[f"identity resolved but not registrable: {exc}"])
            evidence.identity_key = entity.identity_key
            evidence.source = evidence.source or entity.identity_source
            evidence.source_id = evidence.source_id or entity.source_id
            return self._resolved(mention, entity.entity_id, basis, evidence,
                                  notes=notes)

        if resolution.is_ambiguous:
            return self._ambiguous(
                mention, basis, evidence,
                candidates=self._candidates_of(resolution),
                note=resolution.reason or "several structures survive; none chosen")

        if resolution.status.value == "not_found":
            return self._not_found(
                mention, basis, evidence,
                notes=[resolution.reason or "the source holds no such entry"])

        return self._unresolved(
            mention, MentionResolutionBasis.UNRESOLVED, evidence,
            notes=[resolution.reason or f"identity status {resolution.status.value}"])

    def _via_context(self, mention, reference: ContextReference, evidence):
        """The context defined the reference; the identity layer still decides."""
        evidence.context_quote = reference.context_quote
        evidence.source = evidence.source or reference.source or "context"
        evidence.source_id = evidence.source_id or reference.source_id
        evidence.locator = evidence.locator or reference.locator
        notes = ["context definition + identity policy + identity key"]
        if (reference.smiles or "").strip() or (reference.inchi or "").strip():
            kind = "SMILES" if (reference.smiles or "").strip() else "INCHI"
            value = (reference.smiles or reference.inchi).strip()
            return self._via_identity(
                mention, StructureInput.of(kind, value),
                MentionResolutionBasis.CONTEXTUAL_REFERENCE, evidence,
                notes=notes + [f"context gave a structure ({kind})"])
        if (reference.name or "").strip():
            return self._via_identity(
                mention, StructureInput.of("NAME", reference.name.strip()),
                MentionResolutionBasis.CONTEXTUAL_REFERENCE, evidence,
                notes=notes + [f"context named it {reference.name.strip()!r}"])
        return self._unresolved(
            mention, MentionResolutionBasis.UNRESOLVED, evidence,
            notes=["context was found but defined nothing"])

    def _first_context(self, mention):
        for source in self._context_sources:
            reference = source.resolve_reference(mention)
            if reference is not None:
                return reference
        return None

    def _registry_entities_named(self, mention):
        wanted = normalize_for_search(mention.normalized_text or mention.raw_text)
        if not wanted:
            return []
        named = []
        for entity_id in self._registry.entity_ids:
            entity = self._registry.get(entity_id)
            if any(normalize_for_search(name) == wanted
                   for name in entity.names):
                named.append(entity)
        return named

    def _candidates_of(self, resolution) -> list[MentionCandidateInfo]:
        """Every candidate, looked up -- never registered, never ranked."""
        out = []
        for candidate in resolution.candidates:
            entity_id = None
            if (candidate.inchi or "").startswith("InChI="):
                found = self._registry.find_by_identity(candidate.inchi)
                entity_id = found.entity_id if found is not None else None
            out.append(MentionCandidateInfo.from_identity_candidate(
                candidate, entity_id=entity_id))
        return out

    def _mention_evidence(self, mention: ChemicalMention) -> MentionResolutionEvidence:
        refs = list(mention.evidence_refs or [])
        if self._held_evidence is None:
            unresolved: list[str] = []
            notes = ["evidence references were not validated against held evidence"]
        else:
            kept = [r for r in refs if r in self._held_evidence]
            unresolved = [r for r in refs if r not in self._held_evidence]
            refs = kept
            notes = (["unresolved evidence references are listed, not dropped"]
                     if unresolved else [])
        return MentionResolutionEvidence(
            source=mention.source,
            source_id=mention.source_id,
            locator=mention.locator,
            evidence_refs=refs,
            unresolved_evidence_refs=unresolved,
            provenance=Provenance(
                tool="chemical-mention-resolver",
                tool_version=MENTION_RESOLUTION_VERSION,
                source=mention.source or None,
                source_id=mention.source_id or None),
            notes=notes)

    # -- outcomes -----------------------------------------------------------

    def _resolved(self, mention, entity_id, basis, evidence, *, notes):
        evidence.basis = basis
        evidence.notes = list(evidence.notes) + list(notes)
        resolved = mention.model_copy(update={
            "entity_id": entity_id,
            "resolution_status": MentionResolutionStatus.RESOLVED,
        })
        return ChemicalMentionResolution(
            mention=resolved, status=MentionResolutionStatus.RESOLVED,
            entity_id=entity_id, basis=basis, evidence=evidence)

    def _ambiguous(self, mention, basis, evidence, *, candidates=(), entity_ids=(), note=""):
        evidence.basis = basis
        evidence.notes = list(evidence.notes) + ([note] if note else [])
        kept = list(candidates) or [
            MentionCandidateInfo(entity_id=eid) for eid in entity_ids]
        return ChemicalMentionResolution(
            mention=mention.model_copy(update={
                "entity_id": None,
                "resolution_status": MentionResolutionStatus.AMBIGUOUS,
            }),
            status=MentionResolutionStatus.AMBIGUOUS, basis=basis,
            evidence=evidence, candidates=kept)

    def _not_found(self, mention, basis, evidence, *, notes):
        evidence.basis = basis
        evidence.notes = list(evidence.notes) + list(notes)
        return ChemicalMentionResolution(
            mention=mention.model_copy(update={
                "entity_id": None,
                "resolution_status": MentionResolutionStatus.NOT_FOUND,
            }),
            status=MentionResolutionStatus.NOT_FOUND, basis=basis,
            evidence=evidence)

    def _unresolved(self, mention, basis, evidence, *, notes):
        evidence.basis = basis
        evidence.notes = list(evidence.notes) + list(notes)
        return ChemicalMentionResolution(
            mention=mention.model_copy(update={
                "entity_id": None,
                "resolution_status": MentionResolutionStatus.UNRESOLVED,
            }),
            status=MentionResolutionStatus.UNRESOLVED, basis=basis,
            evidence=evidence)


__all__ = [
    "ANALYTICS_EXCLUSIONS",
    "ChemicalMentionResolver",
    "MentionContextSource",
    "NUMBERED_REFERENCE_RE",
    "PUBCHEM_CID_RE",
    "ParentheticalContext",
    "StaticMentionContext",
    "extract_chemical_mentions",
    "is_numbered_reference",
]

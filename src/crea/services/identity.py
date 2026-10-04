"""Chemical identity resolution. The decision layer.

This is the module the whole phase is about: it takes what the user *said* and
answers, honestly, what has been *established*. Three rules shape every
function here.

**1. Deterministic parsing outranks everything.** A SMILES or an InChI is
interpreted by RDKit and nothing else. There is no model in this module, no
fallback that "helps" a malformed string along, and no path by which a
suggestion becomes a finding. If the string does not parse, the answer is
`INVALID` -- not a repaired version of the user's intent.

**2. A lookup returns candidates; a rule promotes one to resolved.** Providers
propose. This module disposes. When more than one distinct structure survives,
the answer is `AMBIGUOUS` and the candidates are all carried forward. Taking
`candidates[0]` is the failure mode this phase exists to prevent, so there is
no code here that does it.

**3. Failure to look is not absence.** `NOT_FOUND` and `UNAVAILABLE` are
different answers and are different enum values. A provider that returns `[]`
after a timeout would make an unreachable database look like an empty one, so
this layer never treats a raised `ProviderError` as "no results".

Where the pieces live: `crea.chem` parses structure notations and is the only
caller of `Chem.MolFromSmiles` / `Chem.MolFromInchi`. `crea.providers` reaches
a database. `crea.core.models.identity` only describes. The judgment is here.

The two lookups this module deliberately does *not* contain are the two the
spec forbids: `name -> SMILES` and `image -> SMILES` are never computed by
invention. A name reaches a database; an image reaches a recogniser in its own
process; and neither is ever completed by guessing.
"""

from __future__ import annotations

import re
from typing import Iterable

from crea.core.errors import NormalizationError, ProviderError, ProviderUnavailable
from crea.core.ids import content_hash
from crea.core.models.identity import (
    ChemicalIdentityCandidate,
    ChemicalIdentityResolution,
    IdentityBasis,
    IdentityFacets,
    ImageProvenance,
    MatchedBy,
    RecognitionStage,
    StructureInput,
    StructureInputKind,
    StructureResolutionStatus,
)
from crea.providers.base import CompoundProvider, CompoundQuery


# ---------------------------------------------------------------------------
# identity facets -- pure string comparison over standard InChI layers
# ---------------------------------------------------------------------------

#: The InChI layers, and which facet each one answers. Standard InChI splits a
#: structure into exactly these questions, which is why comparing layer by
#: layer is the honest way to say *which* sense of "same" was measured.
#:
#: `c` is the heavy-atom connection table: which atoms are bonded to which,
#: with no bond orders in it. `h` is hydrogen placement, which is where
#: saturation and mobile-H tautomerism live. `t`/`m`/`s`/`b` are the
#: stereochemistry layers. `i` is isotopes. `q`/`p` are charge and protons.
#: `f` appears only in non-standard (fixed-H) InChI.
_LAYER_FACET = {
    "c": "connectivity",
    "h": "hydrogen",
    "t": "stereo",
    "m": "stereo",
    "s": "stereo",
    "b": "stereo",
    "i": "isotope",
    "q": "charge",
    "p": "charge",
    "f": "fixed_h",
}

_HEAVY_H = re.compile(r"H\d*")


def inchi_layers(inchi: str) -> dict[str, str]:
    """Split a standard InChI into `formula` and one entry per layer letter.

    Returns `{}` for anything that is not an InChI, so a caller comparing two
    of them gets "not comparable" rather than an exception.
    """
    if not isinstance(inchi, str) or not inchi.startswith("InChI="):
        return {}
    segments = inchi.split("/")[1:]
    if not segments:
        return {}
    layers: dict[str, str] = {"formula": segments[0]}
    for segment in segments[1:]:
        if not segment:
            continue
        letter, value = segment[0], segment[1:]
        layers[letter] = layers.get(letter, "") + value
    return layers


def _heavy_skeleton(formula: str) -> str:
    """The formula with every hydrogen count removed: `C6H6O` -> `C6O`."""
    return _HEAVY_H.sub("", formula or "")


def measure_facets(inchi_a: str, inchi_b: str) -> IdentityFacets:
    """Compare two InChI strings facet by facet, leaving the unanswerable `None`.

    Nothing here decides; it records. `basis()` on the result is what turns the
    four booleans into the one claim the caller may make.

    The facets are compared as InChI layers rather than by re-deriving them
    from graphs, for two reasons. InChI already normalises mobile hydrogen,
    so `/h` equality is exactly "same tautomer class by InChI's definition"
    rather than a definition this module would have to invent. And comparing
    layers is auditable: a reader can point at the `/t` that disagreed.

    One limit, in the safe direction. When two structures differ in isotopes,
    standard InChI may also rewrite the stereo layers, because isotope labels
    can change a stereocentre's parity. That pair is then reported as
    `same_stereo=False` even though the stereochemistry is really the same --
    which *under*-claims. Under-claiming is the right error for a phase whose
    entire purpose is to stop over-claiming, so it is left as is rather than
    patched with a heuristic.

    Two structures that are not both InChI return all-`None` facets, which
    `basis()` reads as `UNKNOWN`. Silently reporting "same" for a pair we could
    not compare is the one outcome that must not happen.
    """
    a = inchi_layers(inchi_a)
    b = inchi_layers(inchi_b)
    if not a or not b:
        return IdentityFacets()

    def layer(letter: str) -> tuple[str, str]:
        return a.get(letter, ""), b.get(letter, "")

    same_c = (a.get("c", "") == b.get("c", "")) and (
        _heavy_skeleton(a.get("formula", "")) == _heavy_skeleton(b.get("formula", ""))
    )
    same_h = a.get("h", "") == b.get("h", "")
    same_stereo = all(layer(letter)[0] == layer(letter)[1] for letter in ("t", "m", "s", "b"))
    same_iso = a.get("i", "") == b.get("i", "")
    # Tautomerism is hydrogen placement on a shared skeleton: the mobile-H
    # layer is what InChI normalises, so comparing it is comparing tautomer
    # classes. Protonation and charge ride along in the same facet, because
    # `/p` and `/q` answer "where are the protons" too -- acetic acid and
    # acetate differ in exactly that and in nothing else, and reporting them as
    # the same species because only `/p` moved would be the error this whole
    # module exists to prevent.
    #
    # A different skeleton is not a tautomer difference, it is a different
    # molecule, and is reported through `same_connectivity` instead.
    same_tautomer = (
        same_c and same_h
        and a.get("q", "") == b.get("q", "")
        and a.get("p", "") == b.get("p", "")
    )

    return IdentityFacets(
        same_connectivity=same_c,
        same_stereo=same_stereo if same_c else None,
        same_isotope=same_iso if same_c else None,
        same_tautomer=same_tautomer if same_c else None,
    )


def compare_identities(
    a: ChemicalIdentityCandidate, b: ChemicalIdentityCandidate
) -> IdentityFacets:
    """The facets between two candidates, preferring InChI and falling back carefully.

    Two candidates that carry no structure at all yield all-`None` facets. That
    is the honest answer: they were not compared, and saying so is better than
    reporting agreement between two empty strings.
    """
    if a.inchi and b.inchi:
        return measure_facets(a.inchi, b.inchi)
    if a.canonical_smiles and b.canonical_smiles and a.inchikey and b.inchikey:
        # No InChI, but the parser did produce keys. The skeleton block is the
        # connectivity; the rest of the key is stereo, isotope and charge
        # rolled together, so it can only answer the coarse questions.
        a_skeleton, _, a_rest = a.inchikey.partition("-")
        b_skeleton, _, b_rest = b.inchikey.partition("-")
        same_c = a_skeleton == b_skeleton and a_skeleton != ""
        return IdentityFacets(
            same_connectivity=same_c if a_skeleton else None,
            same_stereo=(a_rest == b_rest) if same_c else None,
            same_isotope=None,
            same_tautomer=None,
        )
    return IdentityFacets()


def _distinct_structures(candidates: Iterable[ChemicalIdentityCandidate]) -> list[
    ChemicalIdentityCandidate
]:
    """Collapse candidates that are the same structure, keeping the first of each.

    PubChem returns one entry under a primary name and again under a synonym,
    and a name lookup that surfaces both is not an ambiguity -- it is the same
    compound reached twice. Collapsing on the structure rather than on the
    record is what makes "several candidates" mean "several possibilities"
    rather than "several spellings".

    The record kept is the first one encountered, so the provenance a reader
    sees is the one that arrived first rather than an arbitrary survivor.
    """
    seen: set[tuple[str, str]] = set()
    out: list[ChemicalIdentityCandidate] = []
    for candidate in candidates:
        key = (candidate.inchikey, candidate.canonical_smiles, candidate.inchi)
        if key == ("", "", ""):
            out.append(candidate)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------


class IdentityResolutionService:
    """Resolve a `StructureInput` into a `ChemicalIdentityResolution`.

    Constructed with whatever it may consult and nothing else. A service with
    no `compound_provider` can still resolve a SMILES perfectly well -- the
    deterministic path needs no source -- and cannot resolve a name at all,
    which it reports as `UNAVAILABLE` rather than inventing an answer.

    There is no LLM field on this class, and adding one would not help: the
    output of this service is used as *deterministic input* to chemistry tools,
    and an input that a model could reach into is not deterministic.
    """

    def __init__(
        self,
        compound_provider: CompoundProvider | None = None,
        image_reader: object | None = None,
    ) -> None:
        self._provider = compound_provider
        self._image_reader = image_reader

    # -- dispatch ---------------------------------------------------------

    def resolve(self, inp: StructureInput) -> ChemicalIdentityResolution:
        """Resolve by the input's declared kind. The kind is never inferred."""
        dispatch = {
            StructureInputKind.SMILES: self.resolve_by_smiles,
            StructureInputKind.INCHI: self.resolve_by_inchi,
            StructureInputKind.INCHIKEY: self.resolve_by_inchikey,
            StructureInputKind.PUBCHEM_CID: self.resolve_by_cid,
            StructureInputKind.NAME: self.resolve_by_name,
            StructureInputKind.IMAGE: self.resolve_by_image,
        }
        handler = dispatch.get(inp.kind)
        if handler is None:  # pragma: no cover - the enum is exhaustive
            return self._finish(
                inp, StructureResolutionStatus.UNAVAILABLE,
                reason=f"no resolution path for kind {inp.kind!r}",
                codes=["NO_RESOLUTION_PATH"],
            )
        return handler(inp)

    # -- deterministic ----------------------------------------------------

    def resolve_by_smiles(self, inp: StructureInput) -> ChemicalIdentityResolution:
        """SMILES -> RDKit -> canonical -> InChI/InChIKey -> resolved.

        The highest-priority path in the phase, and the reason the rest of the
        work is worth doing: a structure the user already wrote down needs no
        database, no model and no opinion. It parses or it does not.

        An unparseable SMILES is `INVALID`, full stop. This layer has no
        opinion about what the user meant to type, and a "corrected" SMILES
        offered as *the* structure would be an invention wearing the user's
        input as a disguise.
        """
        smiles = (inp.value or "").strip()
        if not smiles:
            return self._finish(
                inp, StructureResolutionStatus.INVALID,
                reason="empty SMILES input", codes=["EMPTY_INPUT"],
            )
        try:
            from crea.chem.normalize import normalize

            molecule = normalize(smiles, source="user_input")
        except NormalizationError as exc:
            return self._finish(
                inp, StructureResolutionStatus.INVALID,
                reason=f"RDKit could not parse this SMILES: {exc.reason}",
                codes=["PARSE_FAILED"],
            )
        return self._resolved_from_molecule(inp, molecule)

    def resolve_by_inchi(self, inp: StructureInput) -> ChemicalIdentityResolution:
        """InChI -> RDKit -> structure -> canonical -> resolved. Same rules as SMILES.

        InChI is a deterministic notation and gets the deterministic path. The
        one asymmetry worth naming: a malformed InChI is `INVALID` exactly as a
        malformed SMILES is, and is never quietly tried as a SMILES instead --
        guessing which notation the user meant is the first step toward
        resolving something they did not ask about.
        """
        inchi = (inp.value or "").strip()
        if not inchi:
            return self._finish(
                inp, StructureResolutionStatus.INVALID,
                reason="empty InChI input", codes=["EMPTY_INPUT"],
            )
        if not inchi.startswith("InChI="):
            return self._finish(
                inp, StructureResolutionStatus.INVALID,
                reason="an InChI must start with 'InChI='; refusing to guess "
                       "at another notation",
                codes=["NOT_AN_INCHI"],
            )
        try:
            from crea.chem.normalize import normalize_inchi

            molecule = normalize_inchi(inchi, source="user_input")
        except NormalizationError as exc:
            return self._finish(
                inp, StructureResolutionStatus.INVALID,
                reason=f"RDKit could not parse this InChI: {exc.reason}",
                codes=["PARSE_FAILED"],
            )
        return self._resolved_from_molecule(inp, molecule)

    def _resolved_from_molecule(
        self, inp: StructureInput, molecule
    ) -> ChemicalIdentityResolution:
        """Build the resolution for a structure the parser established."""
        candidate = ChemicalIdentityCandidate(
            candidate_id=f"ide-{content_hash(molecule.canonical_smiles)}",
            source="user_input",
            source_id=inp.value[:80],
            matched_by=(
                MatchedBy.SMILES if inp.kind is StructureInputKind.SMILES else MatchedBy.INCHI
            ),
            canonical_smiles=molecule.canonical_smiles,
            isomeric_smiles=molecule.canonical_smiles,
            inchi=molecule.inchi,
            inchikey=molecule.inchikey,
            molecular_formula=molecule.descriptors.molecular_formula,
            molecular_weight=molecule.descriptors.mol_wt,
            identity_basis=IdentityBasis.EXACT_STRUCTURE,
            facets=IdentityFacets(
                same_connectivity=True,
                same_stereo=True,
                same_isotope=True,
                same_tautomer=True,
            ),
            notes=["parsed deterministically by RDKit from the user's own input"],
        )
        return self._finish(
            inp,
            StructureResolutionStatus.RESOLVED,
            candidates=[candidate],
            selected=candidate,
            reason="the user supplied a structure notation and it parsed; "
                   "nothing was looked up and nothing was guessed",
            codes=["DETERMINISTIC_PARSE"],
            rules=["deterministic_parse_wins", "no_llm_correction"],
            basis=IdentityBasis.EXACT_STRUCTURE,
        )

    # -- lookups ----------------------------------------------------------

    def resolve_by_inchikey(self, inp: StructureInput) -> ChemicalIdentityResolution:
        """An InChIKey is a *key*, not a structure: it is looked up, never inverted.

        Deriving a structure from an InChIKey is not a parsing operation. The
        key is a hash, and a hash is not reversible -- any tool that claims to
        invert one is returning a lookup result it did not label as such. So
        this path goes to a compound database and reports what came back.

        Several distinct entries for one key is `AMBIGUOUS`, not a vote.
        """
        inchikey = (inp.value or "").strip()
        if not inchikey:
            return self._finish(
                inp, StructureResolutionStatus.INVALID,
                reason="empty InChIKey input", codes=["EMPTY_INPUT"],
            )
        return self._lookup(
            inp,
            CompoundQuery(inchikey=inchikey),
            matched_by=MatchedBy.INCHIKEY,
            input_name=None,
        )

    def resolve_by_cid(self, inp: StructureInput) -> ChemicalIdentityResolution:
        """A PubChem CID is a primary key. One entry, or a report that says otherwise."""
        cid = (inp.value or "").strip()
        if not cid or not cid.isdigit():
            return self._finish(
                inp, StructureResolutionStatus.INVALID,
                reason="a PubChem CID must be a positive integer; "
                       f"got {cid!r}",
                codes=["NOT_A_CID"],
            )
        return self._lookup(
            inp,
            CompoundQuery(identifier=cid),
            matched_by=MatchedBy.CID,
            input_name=None,
        )

    def resolve_by_name(self, inp: StructureInput) -> ChemicalIdentityResolution:
        """A chemical name is a *word*, and a word is not a structure.

        This is the path the spec is most concerned about, and the rule is
        absolute: the structure comes from a deterministic parser, a trusted
        chemical database, or the user. It never comes from a model, because a
        model that can write a plausible SMILES for "vitamin C" can write a
        plausible SMILES for "vitamin C-ish" and there is no line between
        those two behaviours.

        What makes a name hard is not rare words -- it is ordinary ones.
        `benzene` names one compound. `vitamin C` may name ascorbic acid, or
        ascorbate, or a salt, or a hydrate, and those are different structures
        with different formulas. This layer does not resolve that by
        preference. If more than one distinct structure survives, the answer is
        `AMBIGUOUS` and the user is asked.
        """
        name = (inp.value or "").strip()
        if not name:
            return self._finish(
                inp, StructureResolutionStatus.INVALID,
                reason="empty chemical name", codes=["EMPTY_INPUT"],
            )
        return self._lookup(
            inp,
            CompoundQuery(name=name),
            matched_by=MatchedBy.PRIMARY_NAME,
            input_name=name,
        )

    def resolve_by_image(self, inp: StructureInput) -> ChemicalIdentityResolution:
        """Image -> recogniser -> candidate. Never image -> resolved.

        See `RecognitionStage`: OCSR is recognition, not verification. The
        recogniser emits a string it believes is a structure; whether that
        string *is* a structure is RDKit's question, and whether it is the
        user's structure is nobody's here. So the image path produces a
        candidate at `IMAGE_RECOGNIZED_CANDIDATE`, promotes it to
        `STRUCTURE_CANDIDATE` only if it parses, and otherwise reports the
        failure. It never reaches `RESOLVED`, because nothing in the chain
        established that this is the compound the drawing depicts.

        The recogniser runs out of process, behind a worker, in an isolated
        runtime. It is not imported here, and this module would work -- with a
        clear `UNAVAILABLE` -- even if DECIMER were never installed.
        """
        provenance = inp.image or ImageProvenance()
        if self._image_reader is None:
            return self._finish(
                inp, StructureResolutionStatus.UNAVAILABLE,
                reason="no image recogniser is configured in this environment; "
                       "chemical structure recognition runs in an isolated "
                       "worker and is optional",
                codes=["NO_RECOGNIZER"],
            )
        try:
            raw_smiles = self._image_reader.read(provenance)
        except Exception as exc:  # noqa: BLE001 - a worker can fail in many ways
            return self._finish(
                inp, StructureResolutionStatus.UNAVAILABLE,
                reason=f"the image recogniser did not return a reading: {exc}",
                codes=["RECOGNIZER_FAILED"],
            )

        recognized = provenance.model_copy(update={"raw_smiles": raw_smiles})
        candidate = ChemicalIdentityCandidate(
            candidate_id=f"ide-img-{content_hash(raw_smiles or 'empty')}",
            source="DECIMER",
            source_id=recognized.image_sha256[:16],
            matched_by=MatchedBy.IMAGE_RECOGNITION,
            canonical_smiles="",
            isomeric_smiles=raw_smiles,
            recognition_stage=RecognitionStage.IMAGE_RECOGNIZED_CANDIDATE,
            notes=[
                "the recogniser emitted this string; it has not been "
                "established as a structure"
            ],
            raw={"raw_smiles": raw_smiles, "image_sha256": recognized.image_sha256},
        )

        if not raw_smiles:
            return self._finish(
                inp, StructureResolutionStatus.CANDIDATE,
                candidates=[candidate],
                reason="the recogniser returned no structure for this image",
                codes=["IMAGE_RECOGNIZED_CANDIDATE", "EMPTY_READING"],
                rules=["ocr_is_recognition_not_verification"],
            )

        try:
            from crea.chem.normalize import normalize

            molecule = normalize(raw_smiles, source="ocr")
        except NormalizationError:
            candidate.notes.append(
                "the recognised string did not parse as a SMILES, so it is "
                "not a structure candidate at all"
            )
            candidate.recognition_stage = RecognitionStage.UNPARSED
            return self._finish(
                inp, StructureResolutionStatus.INVALID,
                candidates=[candidate],
                reason="the recogniser emitted a string that is not a valid "
                       "SMILES; this is a failed reading, not a structure",
                codes=["IMAGE_RECOGNIZED_CANDIDATE", "UNPARSED"],
                rules=["ocr_is_recognition_not_verification"],
            )

        candidate.canonical_smiles = molecule.canonical_smiles
        candidate.inchi = molecule.inchi
        candidate.inchikey = molecule.inchikey
        candidate.molecular_formula = molecule.descriptors.molecular_formula
        candidate.molecular_weight = molecule.descriptors.mol_wt
        candidate.recognition_stage = RecognitionStage.STRUCTURE_CANDIDATE
        candidate.notes.append(
            "the recognised string parsed and sanitised; it is a structure "
            "candidate and still not a confirmed identity"
        )

        # An image reading is a proposal. It is held at CANDIDATE -- never
        # RESOLVED -- and the caller decides whether to ask the user to confirm.
        return self._finish(
            inp, StructureResolutionStatus.CANDIDATE,
            candidates=[candidate],
            reason="the recognised structure parsed, but recognition is not "
                   "verification; the identity is a candidate awaiting "
                   "confirmation",
            codes=["IMAGE_RECOGNIZED_CANDIDATE", "STRUCTURE_CANDIDATE"],
            rules=["ocr_is_recognition_not_verification", "image_is_never_resolved"],
        )

    # -- the lookup shared by name / key / cid -----------------------------

    def _lookup(
        self,
        inp: StructureInput,
        query: CompoundQuery,
        *,
        matched_by: MatchedBy,
        input_name: str | None,
    ) -> ChemicalIdentityResolution:
        if self._provider is None:
            return self._finish(
                inp, StructureResolutionStatus.UNAVAILABLE,
                reason="no compound database is configured, so this input "
                       "cannot be looked up; it has not been shown to be "
                       "absent",
                codes=["NO_PROVIDER"],
            )

        try:
            records = list(self._provider.lookup(query))
        except (ProviderUnavailable, ProviderError) as exc:
            # The central distinction of this phase, applied here: a source
            # that could not answer is UNAVAILABLE, and a source that answered
            # "nothing" is NOT_FOUND. Merging them would tell the agent that
            # PubChem has never heard of a compound because a request timed
            # out, and the agent would then act on a fact nobody established.
            return self._finish(
                inp, StructureResolutionStatus.UNAVAILABLE,
                reason=f"the compound database could not be consulted: {exc}",
                codes=["PROVIDER_FAILED", type(exc).__name__],
            )

        candidates = _annotate(
            records,
            input_name=input_name,
            matched_by=matched_by,
            query=query,
        )
        candidates = _distinct_structures(candidates)

        if not candidates:
            return self._finish(
                inp, StructureResolutionStatus.NOT_FOUND,
                reason=f"{getattr(self._provider, 'name', 'the compound database')}"
                       f" holds no entry matching this input",
                codes=["NOT_FOUND"],
            )

        if len(candidates) > 1:
            # Never `candidates[0]`. The list is the answer.
            return self._finish(
                inp,
                StructureResolutionStatus.AMBIGUOUS,
                candidates=candidates,
                reason=(
                    f"found {len(candidates)} distinct structures for this "
                    f"input; they cannot be distinguished by any rule this "
                    f"layer is willing to apply, so the choice is the user's"
                ),
                codes=["MULTIPLE_CANDIDATES"],
                rules=["never_pick_candidate_zero"],
            )

        candidate = candidates[0]
        return self._finish(
            inp,
            StructureResolutionStatus.RESOLVED,
            candidates=candidates,
            selected=candidate,
            reason="exactly one structure matched this input in the "
                   "compound database",
            codes=["SINGLE_MATCH"],
            rules=["single_surviving_candidate"],
            basis=candidate.identity_basis,
        )

    # -- construction -----------------------------------------------------

    def _finish(
        self,
        inp: StructureInput,
        status: StructureResolutionStatus,
        *,
        candidates: list[ChemicalIdentityCandidate] | None = None,
        selected: ChemicalIdentityCandidate | None = None,
        reason: str = "",
        codes: list[str] | None = None,
        rules: list[str] | None = None,
        basis: IdentityBasis = IdentityBasis.UNKNOWN,
    ) -> ChemicalIdentityResolution:
        return ChemicalIdentityResolution(
            status=status,
            input=inp,
            candidates=list(candidates or []),
            selected=selected,
            selected_basis=basis if selected is not None else IdentityBasis.UNKNOWN,
            reason=reason,
            reason_codes=list(codes or []),
            rules_applied=list(rules or []),
        )


def _annotate(
    records: Iterable[object],
    *,
    input_name: str | None,
    matched_by: MatchedBy,
    query: CompoundQuery,
) -> list[ChemicalIdentityCandidate]:
    """Give every provider record the provenance the spec requires, in one place.

    A provider may already have filled some of these in; whatever it left blank
    is completed here rather than left blank, so that no candidate ever reaches
    a report without saying which source produced it and which key matched.
    """
    out: list[ChemicalIdentityCandidate] = []
    for index, record in enumerate(records):
        if isinstance(record, ChemicalIdentityCandidate):
            candidate = record.model_copy(deep=True)
        else:  # a provider returning some other shape is a provider bug
            continue
        if not candidate.candidate_id:
            candidate.candidate_id = f"ide-{content_hash(candidate.canonical_smiles or str(index))}"
        if input_name is not None:
            candidate.input_name = candidate.input_name or input_name
        if candidate.matched_by is MatchedBy.SYNONYM and candidate.matched_synonym:
            # A synonym hit is the one thing more specific than the query's
            # key: the source says which *string* reached the record, and that
            # is a better answer than "you looked it up by name". Keep it.
            pass
        else:
            # Otherwise `matched_by` describes *this* lookup, which is the
            # query's key -- not whatever the source happened to label its own
            # record. A CID lookup that returns a candidate pre-tagged
            # `PRIMARY_NAME` would otherwise report the wrong question answered.
            candidate.matched_by = matched_by
            if not candidate.matched_synonym:
                candidate.matched_synonym = None
        if not candidate.identity_basis or candidate.identity_basis is IdentityBasis.UNKNOWN:
            # A database entry asserts its own identity; we have not compared
            # it to anything. Recording that is the difference between "PubChem
            # says this is compound 2244" and "we verified this is 2244".
            if candidate.describes_structure():
                candidate.identity_basis = IdentityBasis.PROVIDER_IDENTITY
                candidate.facets = IdentityFacets(
                    same_connectivity=None,
                    same_stereo=None,
                    same_isotope=None,
                    same_tautomer=None,
                )
        if not candidate.notes:
            candidate.notes.append(
                f"matched by {candidate.matched_by.value}"
                + (f" ({candidate.matched_synonym})" if candidate.matched_synonym else "")
            )
        out.append(candidate)
    return out

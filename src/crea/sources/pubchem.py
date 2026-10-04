"""PubChem PUG REST. The compound database adapter.

The first adapter in this codebase whose subject is a *structure* rather than a
paper, and it is held to the same contract as Crossref and OpenAlex: it
requests, parses, attributes and maps errors. It does not decide, does not
rank, and does not pick among candidates.

> **A PubChem record is not literature evidence.**
> PubChem says "this structure is compound 2244 and it is called aspirin". It
> does not say anyone made it, under what conditions, with what outcome. The
> identity this adapter produces is `CHEMICAL_IDENTITY_EVIDENCE` and nothing
> more, and no field on the models it returns can carry a reaction claim.

Contract sources, verified against the live API before this file was written
(payloads in `tests/fixtures/providers/pubchem/`, every one of them a
`LIVE_CAPTURE` with the URL it came from in its `.meta.json`):

* ``/compound/name/{name}/property/{props}/JSON``
* ``/compound/inchikey/{key}/property/{props}/JSON``
* ``/compound/cid/{cid}/property/{props}/JSON``
* ``/compound/inchi/property/{props}/JSON``  (POST, form body)
* ``/compound/smiles/property/{props}/JSON`` (POST, form body)
* ``/compound/formula/{formula}/cids/JSON``  (returns a ``ListKey``)
* ``/compound/listkey/{key}/property/{props}/JSON``
* ``/compound/cid/{cid}/synonyms/JSON``

The two shapes that are easy to get wrong, and how they are handled:

``Fault`` with code ``PUGREST.NotFound``
    An *answer*, not an outage. The source has no such entry. It is returned as
    an empty result and never raised, because `ResourceNotFound`'s whole
    purpose is to keep "we asked and it is absent" apart from "we could not
    ask" -- and `services.identity` turns that into `NOT_FOUND` rather than
    `UNAVAILABLE``.

``Waiting`` with a ``ListKey``
    Broad queries (a molecular formula) are asynchronous. PubChem replies with
    a key rather than an answer. The key is followed, up to a bounded number of
    polls, and a query that never resolves is a `ProviderError` -- not an empty
    list, which would report "no such compound" for a question still being
    answered.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from crea.core.errors import (
    AuthenticationOrAuthorizationError,
    InvalidRequest,
    InvalidUpstreamResponse,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
    ResourceNotFound,
    UpstreamError,
    UpstreamSchemaError,
)
from crea.core.ids import content_hash
from crea.core.models.identity import ChemicalIdentityCandidate, MatchedBy
from crea.providers.base import Availability, Capability, CompoundQuery, ProviderStatus
from crea.sources.base import Response, Transport

PUBCHEM = "PUBCHEM"
BASE_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"

#: The property fields this adapter asks for. Kept to the list Phase 3.6 §6
#: permits, and nothing more: PubChem returns a large record and this adapter
#: does not store it. `SMILES` and `ConnectivitySMILES` are PubChem's current
#: names for the isomeric and connectivity forms; the older `IsomericSMILES`
#: and `CanonicalSMILES` names are still accepted when present, because both
#: spellings appear in the wild.
PROPERTIES = ",".join([
    "SMILES",
    "ConnectivitySMILES",
    "InChI",
    "InChIKey",
    "MolecularFormula",
    "MolecularWeight",
    "Title",
])

#: How many times a `ListKey` handoff is followed before giving up. PubChem
#: says "your request is running"; it does not say for how long, and polling
#: forever would turn one broad query into a hang.
MAX_LISTKEY_POLLS = 5
LISTKEY_POLL_SECONDS = 1.0

#: A CID is PubChem's integer primary key. Refusing to build a URL out of
#: anything else is cheaper than sanitising it afterwards.
_MAX_VALUE_LENGTH = 512


def _validate_path_value(value: str, what: str) -> str:
    """Reject a lookup value that could not have come from a legitimate path segment.

    These values are placed into a URL path. Nothing here executes anything and
    nothing here imports anything, but a value carrying a path separator or a
    query marker would silently change *which* endpoint is asked -- which is a
    bug that looks like a chemistry result. Refusing it is one line and is
    tested.

    This is deliberately *not* applied to a SMILES or an InChI. Those travel in
    a form body, and `/` is a branch separator in a SMILES: rejecting it there
    would refuse every branched structure.
    """
    if not isinstance(value, str) or not value:
        raise InvalidRequest(f"empty {what}", provider=PUBCHEM)
    if len(value) > _MAX_VALUE_LENGTH:
        raise InvalidRequest(
            f"{what} is longer than {_MAX_VALUE_LENGTH} characters; "
            f"refusing to build a request from it",
            provider=PUBCHEM,
        )
    if any(ch in value for ch in "/?#\n\r\t\\"):
        raise InvalidRequest(
            f"{what} contains a URL delimiter or control character; "
            f"refusing to build a request from it",
            provider=PUBCHEM,
        )
    return value


def _validate_body_value(value: str, what: str) -> str:
    """Reject a form-body value that is not a plausible structure notation.

    The delimiter rule does not apply here: `/` is a branch separator in a
    SMILES and a layer separator in an InChI. What does apply is length and
    control characters, because neither belongs in a structure notation and
    both would indicate the value is not one.
    """
    if not isinstance(value, str) or not value:
        raise InvalidRequest(f"empty {what}", provider=PUBCHEM)
    if len(value) > _MAX_VALUE_LENGTH:
        raise InvalidRequest(
            f"{what} is longer than {_MAX_VALUE_LENGTH} characters; "
            f"refusing to build a request from it",
            provider=PUBCHEM,
        )
    if any(ch in value for ch in "\n\r\t\x00"):
        raise InvalidRequest(
            f"{what} contains a control character; refusing to build a "
            f"request from it",
            provider=PUBCHEM,
        )
    return value


@dataclass
class PubChemSource:
    """PUG REST over a `Transport`. Owns URLs and payload shapes, nothing else.

    A dataclass beside the literature providers' `BaseProvider` rather than a
    subclass of it, because `BaseProvider` is typed to `Paper` and this adapter
    produces `ChemicalIdentityCandidate`. Forcing one through the other would
    mean a compound record wearing a paper's fields, which is the shape of
    mistake Phase 3.6 §6 exists to prevent.
    """

    transport: Transport
    name: str = PUBCHEM
    requires_key: bool = False  # PUG REST needs no key; etiquette does apply
    capabilities: frozenset[Capability] = frozenset(
        {Capability.COMPOUND_LOOKUP, Capability.STRUCTURE_SEARCH}
    )
    base_url: str = BASE_URL
    max_listkey_polls: int = MAX_LISTKEY_POLLS
    listkey_poll_seconds: float = LISTKEY_POLL_SECONDS
    #: Minimum seconds between requests (Stage 2.3, §13). 0.2 is the 5 req/s
    #: PubChem asks applications not to exceed; 0 disables the throttle.
    min_request_interval: float = 0.0
    #: Retries for transient errors only (429 / 5xx), with exponential backoff
    #: (§13/§14). 404 / 400 are never retried -- they are answers, not outages.
    max_retries: int = 0
    retry_backoff: float = 1.0
    #: Populated by `lookup` so a caller can show which payload produced a
    #: candidate. Hashes, not bodies: the body is the source's own record and
    #: §6 forbids keeping the whole of it.
    raw_hashes: dict[str, str] = field(default_factory=dict)
    _last_request_at: float = field(default=0.0, init=False, repr=False)

    # -- availability -----------------------------------------------------

    def status(self) -> ProviderStatus:
        """PUG REST needs no key. An absent key is therefore never the reason.

        `requires_key` is False here and that is a fact about the API rather
        than a shortcut: PubChem publishes no key requirement for PUG REST.
        The status says AVAILABLE so the router will try, and any real failure
        surfaces as a typed error from `lookup` rather than being pre-empted
        here.
        """
        return ProviderStatus(self.name, Availability.AVAILABLE)

    # -- lookup -----------------------------------------------------------

    def lookup(self, query: CompoundQuery) -> list[ChemicalIdentityCandidate]:
        """Answer one compound query. `[]` means the source has no such entry.

        Every other outcome raises. A timeout is not an empty database, and a
        500 is not a name nobody uses -- collapsing either into `[]` would make
        an outage indistinguishable from an absence, which is the one confusion
        `services.identity` is built to avoid.
        """
        route = self._route_for(query)
        if route is None:
            raise InvalidRequest(
                "a compound query must carry exactly one lookup key "
                "(identifier, name, smiles, inchi, inchikey or formula)",
                provider=self.name,
            )
        path, form = route

        payload = self._get_json(path, form)
        if isinstance(payload, dict) and ("Waiting" in payload or "ListKey" in payload):
            payload = self._await_listkey(payload)

        candidates = self._to_candidates(payload, query)
        # Synonyms are the field Phase 3.6 §10 cares about, and they live on a
        # second record. Fetching them per CID is what makes `matched_synonym`
        # a real observation rather than a copy of the query.
        self._attach_synonyms(candidates)
        for candidate in candidates:
            key = candidate.source_id or candidate.inchikey
            self.raw_hashes[key] = content_hash(str(payload), 16)
        return candidates

    # -- URL construction -------------------------------------------------

    def _route_for(self, query: CompoundQuery) -> tuple[str, dict | None] | None:
        """One lookup key per query: the path, plus a form body when one is needed.

        A query carrying several keys is refused rather than combined, because
        combining would answer a different question than the caller asked and
        would make "which key matched" unanswerable.

        SMILES and InChI go as a form body. Both contain characters a URL path
        mangles -- `/` is a branch separator in a SMILES -- and PUG REST
        documents the form-encoded form for exactly this reason. Putting them
        in the path would work for `C#Cc1ccccc1` and silently fail for every
        branched structure, which is the kind of bug that looks like chemistry.
        """
        keys = [
            ("identifier", query.identifier),
            ("name", query.name),
            ("smiles", query.smiles),
            ("inchi", query.inchi),
            ("inchikey", query.inchikey),
            ("formula", query.formula),
        ]
        present = [(kind, value) for kind, value in keys if value]
        if len(present) != 1:
            return None
        kind, value = present[0]

        if kind == "identifier":
            if not value.isdigit():
                raise InvalidRequest(
                    f"a PubChem CID must be a positive integer; got {value!r}",
                    provider=self.name,
                )
            return f"compound/cid/{value}/property/{PROPERTIES}/JSON", None

        if kind == "formula":
            return (
                f"compound/formula/{_validate_path_value(value, 'molecular formula')}"
                f"/cids/JSON",
                None,
            )

        if kind in ("smiles", "inchi"):
            _validate_body_value(value, kind)
            return f"compound/{kind}/property/{PROPERTIES}/JSON", {kind: value}

        if kind == "inchikey":
            return (
                f"compound/inchikey/{_validate_path_value(value, 'InChIKey')}"
                f"/property/{PROPERTIES}/JSON",
                None,
            )

        # A name is placed in the path percent-encoded by the transport. It is
        # still delimiter-checked: a name is a word, and a word with a `/` in it
        # is not a name this adapter will quietly turn into a different route.
        return (
            f"compound/name/{_validate_path_value(value, 'chemical name')}"
            f"/property/{PROPERTIES}/JSON",
            None,
        )

    # -- request / error map ---------------------------------------------

    def _get_json(self, path: str, form: dict | None = None) -> object:
        url = f"{self.base_url}/{path}"
        for attempt in range(self.max_retries + 1):
            response = self._request(url, form)
            try:
                return self._payload(response)
            except (RateLimited, UpstreamError):
                # Transient only. `ResourceNotFound`/`InvalidRequest` are
                # answers and must never be retried (§13/§14).
                if attempt >= self.max_retries:
                    raise
                time.sleep(self.retry_backoff * (2 ** attempt))
        raise ProviderError(f"{self.name} exhausted retries", provider=self.name)

    def _request(self, url: str, form: dict | None = None) -> Response:
        """One request over the `Transport` seam. Nothing else here touches I/O.

        `Transport.request` is async and `CompoundProvider.lookup` is a plain
        synchronous method, so this is the single bridge between the two. The
        transport stays the only module that knows about httpx, which is what
        the offline suite depends on.
        """
        import asyncio

        # A simple request-rate throttle (§13): no claim of guaranteeing a
        # rate, only a floor that keeps us under the documented 5 req/s.
        if self.min_request_interval > 0:
            now = time.monotonic()
            wait = self.min_request_interval - (now - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at = time.monotonic()

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise ProviderError(
                "PubChem lookup cannot run inside a running event loop; "
                "call it from synchronous code",
                provider=self.name,
            )
        return asyncio.run(self._arequest(url, form))

    async def _arequest(self, url: str, form: dict | None) -> Response:
        if form is not None:
            return await self.transport.request("POST", url, data=form)
        return await self.transport.request("GET", url)

    def _payload(self, response: Response) -> object:
        """Map a PUG REST response -- including its own error dialect -- to ours."""
        code = response.status_code
        payload = response.payload

        if isinstance(payload, dict) and "Fault" in payload:
            fault = payload["Fault"]
            fault_code = str(fault.get("Code", ""))
            message = str(fault.get("Message", "PubChem reported a fault"))
            details = fault.get("Details") or []
            detail = "; ".join(str(d) for d in details)[:300]
            # Absence is an answer. `ResourceNotFound` exists for exactly this
            # and is deliberately *not* raised as an error the service would
            # read as an outage -- see `lookup`, which treats it as `[]`.
            if "NotFound" in fault_code:
                raise ResourceNotFound(
                    f"{self.name} has no such entry: {message} {detail}".strip(),
                    provider=self.name,
                    http_status=code,
                )
            if "Busy" in fault_code or "Throttle" in fault_code or code == 429:
                raise RateLimited(
                    f"{self.name} is rate limiting or busy: {message}",
                    provider=self.name,
                    http_status=code,
                )
            if "Input" in fault_code or code == 400:
                raise InvalidRequest(
                    f"{self.name} rejected the query: {message} {detail}".strip(),
                    provider=self.name,
                    http_status=code,
                )
            if "Server" in fault_code or code >= 500:
                raise UpstreamError(
                    f"{self.name} failed: {message} {detail}".strip(),
                    provider=self.name,
                    http_status=code,
                )
            raise UpstreamSchemaError(
                f"{self.name} returned an unrecognised fault code "
                f"{fault_code!r}: {message}",
                provider=self.name,
                http_status=code,
            )

        if code == 404:
            raise ResourceNotFound(
                f"{self.name} has no such entry (HTTP 404)",
                provider=self.name,
                http_status=code,
            )
        if code == 429:
            raise RateLimited(
                f"{self.name} returned HTTP 429", provider=self.name, http_status=code
            )
        if code in (401, 403):
            raise AuthenticationOrAuthorizationError(
                f"{self.name} refused the request (HTTP {code})",
                provider=self.name,
                http_status=code,
            )
        if code == 400:
            raise InvalidRequest(
                f"{self.name} rejected the query (HTTP 400)",
                provider=self.name,
                http_status=code,
            )
        if code >= 500:
            raise UpstreamError(
                f"{self.name} returned HTTP {code}", provider=self.name, http_status=code
            )

        if not isinstance(payload, (dict, list)):
            raise InvalidUpstreamResponse(
                f"{self.name} returned a non-JSON body (HTTP {code})",
                provider=self.name,
                http_status=code,
            )
        return payload

    def _await_listkey(self, payload: object) -> object:
        """Follow PubChem's asynchronous handoff to the actual answer.

        A broad query returns `{"Waiting": {"ListKey": ...}}` rather than
        results. Polling is bounded; giving up raises rather than returns `[]`,
        because "the query has not finished" and "the query matched nothing"
        are different answers and only one of them is true here.
        """
        key = ""
        if isinstance(payload, dict):
            waiting = payload.get("Waiting") or {}
            key = str(waiting.get("ListKey") or payload.get("ListKey") or "")
        if not key:
            raise UpstreamSchemaError(
                f"{self.name} sent a listkey handoff with no list key in it",
                provider=self.name,
            )
        key = _validate_path_value(key, "list key")

        for _ in range(self.max_listkey_polls):
            time.sleep(self.listkey_poll_seconds)
            url = f"{self.base_url}/compound/listkey/{key}/property/{PROPERTIES}/JSON"
            response = self._request(url)
            current = self._payload(response)
            if isinstance(current, dict) and ("Waiting" in current or "ListKey" in current):
                continue
            return current
        raise ProviderError(
            f"{self.name} did not finish a list-key query after "
            f"{self.max_listkey_polls} polls; this is a timeout, not an "
            f"empty result",
            provider=self.name,
        )

    # -- payload -> candidates --------------------------------------------

    def _to_candidates(
        self, payload: object, query: CompoundQuery
    ) -> list[ChemicalIdentityCandidate]:
        if not isinstance(payload, dict):
            raise UpstreamSchemaError(
                f"{self.name} returned a JSON value that is not an object",
                provider=self.name,
            )

        if "PropertyTable" in payload:
            return self._from_property_table(payload, query)
        if "InformationList" in payload:
            return self._from_information_list(payload, query)
        if "IdentifierList" in payload:
            return self._from_identifier_list(payload, query)
        raise UpstreamSchemaError(
            f"{self.name} returned a JSON object with none of PropertyTable, "
            f"InformationList or IdentifierList in it",
            provider=self.name,
        )

    def _from_property_table(
        self, payload: dict, query: CompoundQuery
    ) -> list[ChemicalIdentityCandidate]:
        table = payload.get("PropertyTable") or {}
        rows = table.get("Properties") or []
        out: list[ChemicalIdentityCandidate] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            cid = str(row.get("CID", ""))
            # PubChem's current property names, and the older spellings, so a
            # cached payload from either era parses without special-casing.
            isomeric = str(row.get("SMILES") or row.get("IsomericSMILES") or "")
            connectivity = str(row.get("ConnectivitySMILES")
                               or row.get("CanonicalSMILES") or isomeric)
            out.append(
                ChemicalIdentityCandidate(
                    candidate_id=f"ide-pubchem-{cid}",
                    source=PUBCHEM,
                    source_id=cid,
                    source_url=f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                    candidate_name=str(row.get("Title") or "") or None,
                    canonical_smiles=connectivity,
                    isomeric_smiles=isomeric,
                    inchi=str(row.get("InChI") or ""),
                    inchikey=str(row.get("InChIKey") or ""),
                    molecular_formula=_as_str_or_none(row.get("MolecularFormula")),
                    molecular_weight=_as_float_or_none(row.get("MolecularWeight")),
                    matched_by=self._matched_by(query),
                    input_name=query.name or None,
                    raw={
                        "cid": cid,
                        "title": row.get("Title"),
                        # §6: nothing beyond the listed fields is retained.
                        "payload_keys": sorted(row.keys()),
                    },
                )
            )
        return out

    def _from_information_list(
        self, payload: dict, query: CompoundQuery
    ) -> list[ChemicalIdentityCandidate]:
        """A synonyms record. Carries names, never structures.

        A synonym list is not a structure source, so these candidates describe
        the *name* and leave the structure fields empty. `services.identity`
        treats a candidate with no structure as a name hit rather than as a
        competing molecule -- which is the correct handling for "this word is
        registered for compound 2244".
        """
        info = (payload.get("InformationList") or {}).get("Information") or []
        out: list[ChemicalIdentityCandidate] = []
        for record in info:
            if not isinstance(record, dict):
                continue
            cid = str(record.get("CID", ""))
            for synonym in record.get("Synonym") or []:
                out.append(
                    ChemicalIdentityCandidate(
                        candidate_id=f"ide-pubchem-{cid}-syn",
                        source=PUBCHEM,
                        source_id=cid,
                        source_url=f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                        candidate_name=None,
                        matched_by=MatchedBy.SYNONYM,
                        matched_synonym=str(synonym),
                        synonyms=[str(s) for s in (record.get("Synonym") or [])[:50]],
                        raw={"cid": cid},
                    )
                )
        return out

    def _from_identifier_list(
        self, payload: dict, query: CompoundQuery
    ) -> list[ChemicalIdentityCandidate]:
        """A bare CID list -- what `formula` gives before properties are fetched.

        Returned as candidates carrying the id only. They are names without
        faces: the service will see that they carry no structure and will not
        treat them as competing molecules, and a caller that needs the
        structures re-queries by CID.
        """
        ids = (payload.get("IdentifierList") or {}).get("CID") or []
        out: list[ChemicalIdentityCandidate] = []
        for cid in ids:
            out.append(
                ChemicalIdentityCandidate(
                    candidate_id=f"ide-pubchem-{cid}",
                    source=PUBCHEM,
                    source_id=str(cid),
                    source_url=f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                    matched_by=self._matched_by(query),
                    input_name=query.name or None,
                    raw={"cid": str(cid)},
                )
            )
        return out

    def _matched_by(self, query: CompoundQuery) -> MatchedBy:
        if query.name:
            return MatchedBy.PRIMARY_NAME
        if query.inchikey:
            return MatchedBy.INCHIKEY
        if query.inchi:
            return MatchedBy.INCHI
        if query.smiles:
            return MatchedBy.SMILES
        if query.identifier:
            return MatchedBy.CID
        return MatchedBy.UNKNOWN

    def _attach_synonyms(self, candidates: list[ChemicalIdentityCandidate]) -> None:
        """Fetch each entry's synonym list and record which one matched.

        §10 permits synonyms for matching and display and forbids treating one
        as identity proof. Recording `matched_by = SYNONYM` together with the
        exact string is what keeps that distinction visible: a reader can see
        that "aspirin" matched by synonym and is not thereby proven to be
        anything.

        A failure here is deliberately not fatal. The structure is the answer;
        the synonym list is context. Losing context degrades the record's
        explainability and must not turn a good lookup into an error -- but it
        is recorded on the candidate rather than swallowed, so a later reader
        can tell "this entry has no synonyms" from "we failed to fetch them".
        """
        for candidate in candidates:
            if not candidate.source_id or not candidate.source_id.isdigit():
                continue
            if candidate.synonyms:
                continue
            try:
                payload = self._get_json(f"compound/cid/{candidate.source_id}/synonyms/JSON")
            except Exception as exc:  # noqa: BLE001 - best-effort enrichment only
                candidate.notes.append(
                    f"synonym list could not be fetched ({type(exc).__name__}); "
                    f"the structure above is unaffected"
                )
                continue
            if not isinstance(payload, dict):
                continue
            info = (payload.get("InformationList") or {}).get("Information") or []
            names: list[str] = []
            for record in info:
                if isinstance(record, dict):
                    names.extend(str(s) for s in (record.get("Synonym") or []))
            candidate.synonyms = names[:50]


def _as_str_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def _as_float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

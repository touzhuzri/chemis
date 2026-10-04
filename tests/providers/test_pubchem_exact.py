"""PubChem exact-structure lookup (Stage 2.3) -- offline unit tests.

The provider is exercised over `FakeTransport` with canned PUG REST payloads,
so nothing here needs the network. Identity verification, error typing, the
bounded retry, and explicit import (dedup + provenance) are all pinned.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from crea.core.errors import MoleculeLibraryError
from crea.core.models.pubchem import PubChemMoleculeResult
from crea.providers.base import CompoundQuery
from crea.providers.pubchem_exact import PubChemExactProvider
from crea.services.knowledge_base import KnowledgeBase
from crea.services.molecule_identity import MoleculeIdentityService
from crea.services.pubchem_service import PubChemService
from tests.fakes.transport import FakeTransport

ETHANOL = "CCO"
ETHANOL_INCHIKEY = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"


def _ethanol_table():
    return {"PropertyTable": {"Properties": [{
        "CID": 702,
        "SMILES": "CCO",
        "ConnectivitySMILES": "CCO",
        "InChI": "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3",
        "InChIKey": ETHANOL_INCHIKEY,
        "MolecularFormula": "C2H6O",
        "MolecularWeight": 46.0684,
        "Title": "ethanol",
    }]}}


def _transport(payload=None):
    transport = FakeTransport()
    transport.route("GET", "compound/inchikey/", 200, payload or _ethanol_table())
    # the synonym fetch is best-effort; return an empty list
    transport.route("GET", "compound/cid/", 200,
                    {"InformationList": {"Information": []}})
    return transport


def _provider(payload=None):
    return PubChemExactProvider(transport=_transport(payload))


def _identity(smiles):
    return MoleculeIdentityService().identity(smiles)


# -- §5 identity verification --------------------------------------------


def test_exact_lookup_returns_verified_match():
    result = _provider().lookup_exact(ETHANOL_INCHIKEY)
    assert result.provider == "pubchem"
    assert result.provider_record_id == "702"
    assert result.identity_match is True
    assert result.standard_inchikey == ETHANOL_INCHIKEY
    assert result.canonical_smiles == "CCO"
    assert result.external_identifiers == {"pubchem": {"cid": "702"}}


def test_identity_mismatch_is_reported_not_corrected():
    # PubChem returns a *different* InChIKey than the one requested
    table = _ethanol_table()
    table["PropertyTable"]["Properties"][0]["InChIKey"] = "DIFFERENT-KEY-X"
    result = _provider(table).lookup_exact(ETHANOL_INCHIKEY)
    assert result.identity_match is False
    assert result.standard_inchikey == "DIFFERENT-KEY-X"
    assert result.requested_inchikey == ETHANOL_INCHIKEY
    assert any("does not match" in d for d in result.diagnostics)


# -- §14 error typing ------------------------------------------------------


def test_not_found_is_provider_not_found():
    provider = _provider({"Fault": {"Code": "PUGREST.NotFound",
                                    "Message": "no CID"}})
    with pytest.raises(MoleculeLibraryError) as exc:
        provider.lookup_exact(ETHANOL_INCHIKEY)
    assert exc.value.code == "PROVIDER_NOT_FOUND"


def test_rate_limited_is_typed():
    provider = _provider({"Fault": {"Code": "PUGREST.ServerBusy",
                                    "Message": "busy"}})
    with pytest.raises(MoleculeLibraryError) as exc:
        provider.lookup_exact(ETHANOL_INCHIKEY)
    assert exc.value.code == "PROVIDER_RATE_LIMITED"


def test_unavailable_without_transport():
    provider = PubChemExactProvider()
    with pytest.raises(MoleculeLibraryError) as exc:
        provider.lookup_exact(ETHANOL_INCHIKEY)
    assert exc.value.code == "PROVIDER_UNAVAILABLE"


# -- §13 bounded retry recovers from a transient 503 -----------------------


def test_bounded_retry_recovers_from_transient_503():
    from crea.sources.pubchem import BASE_URL, PROPERTIES, PubChemSource

    url = f"{BASE_URL}/compound/inchikey/{ETHANOL_INCHIKEY}/property/{PROPERTIES}/JSON"
    transport = FakeTransport()
    transport.route_sequence("GET", url, [
        (503, {"Fault": {"Code": "PUGREST.ServerBusy", "Message": "busy"}}, {}),
        (200, _ethanol_table(), {}),
    ])
    transport.route("GET", "compound/cid/", 200,
                    {"InformationList": {"Information": []}})
    source = PubChemSource(transport=transport, max_retries=1, retry_backoff=0.0)
    candidates = source.lookup(CompoundQuery(inchikey=ETHANOL_INCHIKEY))
    assert candidates[0].source_id == "702"


# -- §9/§10 lookup and import are separate, import dedupes -----------------


def test_service_lookup_then_explicit_import():
    db = pathlib.Path(tempfile.mkdtemp()) / "lib.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    service = PubChemService(provider=_provider(), kb=kb)

    result = service.lookup(ETHANOL)
    assert result.identity_match is True

    entity, is_new = service.import_result(result)
    assert is_new is True
    assert entity.identity_source == "PUBCHEM"
    assert entity.source_id == "702"
    assert any(e.namespace == "pubchem" and e.identifier == "702"
               for e in entity.external_identifiers)
    assert kb.count_entities() == 1

    # a second import of the same identity does not create a duplicate
    entity2, is_new2 = service.import_result(result)
    assert is_new2 is False
    assert entity2.entity_id == entity.entity_id
    kb.close()


def test_import_refuses_a_mismatched_identity():
    table = _ethanol_table()
    table["PropertyTable"]["Properties"][0]["InChIKey"] = "DIFFERENT-KEY-X"
    db = pathlib.Path(tempfile.mkdtemp()) / "lib.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    service = PubChemService(provider=_provider(table), kb=kb)
    result = service.lookup(ETHANOL)
    assert result.identity_match is False
    with pytest.raises(MoleculeLibraryError) as exc:
        service.import_result(result)
    assert exc.value.code == "PROVIDER_IDENTITY_MISMATCH"
    assert kb.count_entities() == 0
    kb.close()


# -- §22 offline mode: the app stays up, the lookup reports unavailable ----


def test_api_lookup_is_unavailable_in_offline_mode(tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        response = c.get("/api/providers/pubchem/lookup",
                         params={"smiles": "CCO"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"

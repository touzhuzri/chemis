"""Molecule Library + Standard Chemical Identity (Stage 2.2).

Identity is Standard InChI + Standard InChIKey -- never a self-invented id,
never the input SMILES (§2/§24). The record is the existing ChemicalEntity
(entity_id = internal record id). Tests pin identity consistency across
notations, duplicate prevention, library CRUD, exact/substructure/similarity
search, provenance, the provider seam, and the structured-error API.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from crea.core.errors import MoleculeLibraryError
from crea.services.knowledge_base import KnowledgeBase
from crea.services.molecule_identity import MoleculeIdentityService
from crea.services.molecule_library import MoleculeLibrary


def _kb() -> KnowledgeBase:
    db = pathlib.Path(tempfile.mkdtemp()) / "lib.db"
    kb = KnowledgeBase(db)
    kb.initialize()
    return kb


def _library() -> MoleculeLibrary:
    return MoleculeLibrary(_kb())


# -- §23 Identity: same structure, same identity -------------------------


def test_identity_is_consistent_across_notations():
    service = MoleculeIdentityService()
    a = service.identity("CCO")
    b = service.identity("OCC")
    assert a.standard_inchi == b.standard_inchi
    assert a.standard_inchikey == b.standard_inchikey
    assert a.identity_key == b.identity_key


def test_smiles_is_not_the_identity():
    service = MoleculeIdentityService()
    a = service.identity("CCO")
    assert a.standard_inchikey != "CCO"
    assert a.standard_inchi.startswith("InChI=")


# -- §23 exact identity: duplicate prevention ----------------------------


def test_duplicate_identity_does_not_create_a_second_record():
    library = _library()
    first, is_new = library.add("CCO")
    second, is_new_again = library.add("OCC")
    assert is_new is True and is_new_again is False
    assert first.entity_id == second.entity_id
    assert library.count() == 1


# -- §23 library CRUD ----------------------------------------------------


def test_library_add_get_list_delete_count():
    library = _library()
    entity, _ = library.add("c1ccccc1")
    assert library.count() == 1
    assert library.get(entity.entity_id).entity_id == entity.entity_id
    assert [m.entity_id for m in library.list()] == [entity.entity_id]
    assert library.delete(entity.entity_id) is True
    assert library.count() == 0
    assert library.delete(entity.entity_id) is False


# -- §23 lookup by record_id and InChIKey --------------------------------


def test_lookup_by_record_id_and_inchikey():
    library = _library()
    entity, _ = library.add("CCO")
    assert library.get(entity.entity_id).inchikey == entity.inchikey
    assert library.find_by_inchikey(entity.inchikey).entity_id == entity.entity_id
    assert library.find_by_inchikey("NO-SUCH-KEY") is None


# -- §23 similarity search (reuses SimilarityService) --------------------


def test_similarity_search_ranks_the_library():
    library = _library()
    library.add("Cc1ccccc1")   # toluene
    library.add("CCO")         # ethanol
    hits = library.search_similar("c1ccccc1")  # benzene
    assert hits[0].entity.canonical_smiles == "Cc1ccccc1"
    assert all(h.score is not None for h in hits)


# -- §23 substructure search ---------------------------------------------


def test_substructure_search_finds_benzene_containing_molecules():
    library = _library()
    library.add("c1ccccc1")
    library.add("Cc1ccccc1")
    library.add("CCO")
    hits = library.search_substructure("c1ccccc1")
    smiles = {h.entity.canonical_smiles for h in hits}
    assert "c1ccccc1" in smiles and "Cc1ccccc1" in smiles
    assert "CCO" not in smiles


def test_unrelated_molecule_does_not_substructure_match():
    library = _library()
    library.add("CCO")
    assert library.search_substructure("c1ccccc1") == []


# -- §23 invalid input is a structured error -----------------------------


def test_invalid_smiles_raises_invalid_molecule():
    library = _library()
    with pytest.raises(MoleculeLibraryError) as exc:
        library.add("not-a-smiles")
    assert exc.value.code == "INVALID_MOLECULE"


def test_missing_record_raises_not_found():
    library = _library()
    with pytest.raises(MoleculeLibraryError) as exc:
        library.get("CHEM-00000999")
    assert exc.value.code == "MOLECULE_NOT_FOUND"


# -- §23 provenance ------------------------------------------------------


def test_source_type_is_recorded():
    library = _library()
    entity, _ = library.add("CCO", source_type="CURATED", source_id="seed/1")
    assert entity.identity_source == "CURATED"
    assert entity.source_id == "seed/1"


# -- §23 provider seam ---------------------------------------------------


def test_local_molecule_provider_exposes_records():
    library = _library()
    library.add("CCO")
    library.add("c1ccccc1")
    provider = library.provider()
    assert provider.name == "local"
    assert len(provider.records()) == 2


# -- API -----------------------------------------------------------------


def test_molecule_library_api(tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        body = c.post("/api/molecules", json={"smiles": "CCO"}).json()
        assert body["is_new"] is True
        record_id = body["molecule"]["entity_id"]
        inchikey = body["molecule"]["inchikey"]
        assert body["molecule"]["identity_key"].startswith("crea-identity-key-v1:")
        assert body["molecule"]["external_identifiers"] == []

        dup = c.post("/api/molecules", json={"smiles": "OCC"}).json()
        assert dup["is_new"] is False
        assert dup["molecule"]["entity_id"] == record_id

        assert c.get("/api/molecules").json()["count"] == 1
        assert c.get(f"/api/molecules/{record_id}").json()["entity_id"] == record_id
        assert c.get(f"/api/molecules/identity/{inchikey}").json()["entity_id"] == record_id

        c.post("/api/molecules", json={"smiles": "c1ccccc1"})
        sim = c.get("/api/molecules/search/similar",
                    params={"smiles": "Cc1ccccc1"}).json()
        assert sim["hits"]
        sub = c.get("/api/molecules/search/substructure",
                    params={"smiles": "c1ccccc1"}).json()
        assert sub["hits"]

        assert c.delete(f"/api/molecules/{record_id}").json()["deleted"] is True
        assert c.get(f"/api/molecules/{record_id}").status_code == 404
        assert c.get("/api/molecules/identity/NO-KEY").status_code == 404
        assert c.post("/api/molecules", json={"smiles": "nope"}).status_code == 400

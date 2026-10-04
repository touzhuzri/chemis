"""3D conformer generation (Stage 2.1): ETKDG, coordinates, stereo.

Tests that a SMILES becomes one representative conformer -- atoms with
coordinates, bonds with orders, and no silent breaking of explicit
stereochemistry. Invalid input and embed failures are typed errors, never a
raw RDKit exception (§6).
"""

from __future__ import annotations

import pytest

from crea.core.errors import ConformerError
from crea.services.conformer import ConformerService


def _generate(smiles, **kwargs):
    return ConformerService().generate(smiles, **kwargs)


# -- Test 1/2/3: representative molecules --------------------------------


def test_ethanol_conformer_has_atoms_bonds_and_coordinates():
    conformer = _generate("CCO")
    assert conformer.formula == "C2H6O"
    assert conformer.generation_method == "ETKDG"
    assert len(conformer.atoms) == 9, "2 C + 6 H + 1 O"
    assert len(conformer.bonds) == 8
    assert all(a.atomic_number > 0 for a in conformer.atoms)
    assert all(b.a < len(conformer.atoms) and b.b < len(conformer.atoms)
               for b in conformer.bonds)


def test_benzene_conformer_has_six_carbons_and_aromatic_bonds():
    conformer = _generate("c1ccccc1")
    carbons = [a for a in conformer.atoms if a.element == "C"]
    assert len(carbons) == 6
    aromatic = [b for b in conformer.bonds if b.order == "aromatic"]
    assert len(aromatic) == 6, "aromatic bonds are preserved"


def test_phenylacetylene_conformer_preserves_the_triple_bond():
    conformer = _generate("C#Cc1ccccc1")
    assert conformer.formula == "C8H6"
    assert any(b.order == "triple" for b in conformer.bonds)


# -- Test 4: stereochemistry is not silently broken -----------------------


def test_explicit_stereochemistry_is_preserved():
    conformer = _generate("C[C@H](O)F")
    assert "@" in conformer.canonical_smiles, "the stereo marker survives"

    from rdkit import Chem
    from rdkit.Chem import AllChem
    from crea.chem.normalize import normalize, rebuild

    mol = rebuild(normalize("C[C@H](O)F"))
    before = dict(Chem.FindMolChiralCenters(mol, includeUnassigned=True))
    with_h = Chem.AddHs(mol)
    assert AllChem.EmbedMolecule(with_h, AllChem.ETKDGv3()) == 0
    after = dict(Chem.FindMolChiralCenters(with_h, includeUnassigned=True))
    assert before == after, "the CIP label must not flip during embedding"


# -- Test 5: invalid SMILES is a typed error ------------------------------


def test_invalid_smiles_raises_invalid_molecule():
    with pytest.raises(ConformerError) as exc:
        _generate("not-a-smiles")
    assert exc.value.code == "INVALID_MOLECULE"


# -- Test 6: optional optimization ----------------------------------------


def test_optimized_conformer_reports_its_force_field():
    conformer = _generate("CCO", optimize=True)
    assert conformer.optimized is True
    assert conformer.optimization_method in ("MMFF94", "UFF")


# -- API endpoint: result + structured error ------------------------------


def test_3d_endpoint_returns_coordinates_and_structured_error(tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        payload = c.get("/api/molecule/3d", params={"smiles": "CCO"}).json()
        assert payload["formula"] == "C2H6O"
        assert payload["generation_method"] == "ETKDG"
        assert payload["atoms"], "coordinates returned"
        assert payload["bonds"]

        bad = c.get("/api/molecule/3d", params={"smiles": "not-a-smiles"})
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "INVALID_MOLECULE"

"""The Molecule Explorer (Phase 1): deterministic inspection + reactions.

Positive, negative and edge cases for the inspection service, the functional
group detection, the reaction matcher, and the similarity service -- all over
the existing chemistry layer, no LLM. Reaction matching is pinned for the
"no false positive" guarantee (§13): benzene must never match an alcohol
oxidation, and an alcohol must never match an alkene reaction.
"""

from __future__ import annotations

from crea.chem.normalize import normalize
from crea.chem.scaffold import murcko_scaffold_smiles
from crea.services.inspection import MoleculeInspectionService
from crea.services.reaction_library import ReactionMatcher
from crea.services.similarity import SimilarityService


def _fgs(result):
    return [g.name for g in result.functional_groups]


def _reactions(result):
    return [m.template.name for m in result.reactions]


def _inspect(smiles):
    return MoleculeInspectionService().inspect(smiles)


# -- §13 test 1/3/4/8: inspection over representative molecules ------------


def test_ethanol_inspection_finds_alcohol_oxidation():
    result = _inspect("CCO")
    assert result.valid
    assert "Alcohol" in _fgs(result)
    assert "Primary Alcohol Oxidation" in _reactions(result)
    assert result.properties["molecular_formula"] == "C2H6O"
    assert result.properties["dbe"] == 0
    assert result.identity["inchikey"]


def test_benzene_inspection_matches_only_aromatic_eas():
    result = _inspect("c1ccccc1")
    assert result.valid
    names = _reactions(result)
    # benzene matches electrophilic aromatic substitution, never alkene/alcohol
    assert "Aromatic Nitration" in names
    assert "Primary Alcohol Oxidation" not in names
    assert "Catalytic Hydrogenation" not in names
    assert result.properties["dbe"] == 4
    assert result.properties["aromatic_ring_count"] == 1
    assert result.scaffold["murcko_smiles"] == "c1ccccc1"


def test_acetylene_inspection_finds_alkyne_reactions():
    result = _inspect("C#C")
    names = _reactions(result)
    assert "Hydrogenation" in names
    assert "Partial Reduction (Lindlar)" in names
    assert "Hydration" in names
    assert "Alkyne" in _fgs(result)


def test_carbonyl_inspection_finds_carboxylic_acid():
    result = _inspect("CC(=O)O")
    assert "Carboxylic Acid" in _fgs(result)
    assert "Carboxylic Acid Reduction" in _reactions(result)
    assert result.properties["dbe"] == 1


def test_halide_inspection_finds_substitution_and_elimination():
    result = _inspect("CCBr")
    names = _reactions(result)
    assert "Nucleophilic Substitution" in names
    assert "Elimination" in names
    assert "Alkyl Halide" in _fgs(result)


# -- §13 test 8: no false positive -----------------------------------------


def test_benzene_never_matches_an_alcohol_oxidation():
    result = _inspect("c1ccccc1")
    assert "Primary Alcohol Oxidation" not in _reactions(result)
    assert "Alcohol" not in _fgs(result)


def test_an_alcohol_never_matches_an_alkene_reaction():
    result = _inspect("CCO")
    names = _reactions(result)
    assert "Catalytic Hydrogenation" not in names
    assert "Epoxidation" not in names


def test_aryl_halide_does_not_match_aliphatic_substitution():
    # bromobenzene: aryl halide, not an SN2/elimination substrate
    result = _inspect("c1ccccc1Br")
    assert "Nucleophilic Substitution" not in _reactions(result)
    assert "Aryl Halide" in _fgs(result)


# -- §13 test 6: scaffold extraction ---------------------------------------


def test_scaffold_extraction():
    assert murcko_scaffold_smiles(normalize("Cc1ccccc1")) == "c1ccccc1"
    assert murcko_scaffold_smiles(normalize("CCc1ccccc1")) == "c1ccccc1"


# -- §13 test 5: similarity -------------------------------------------------


def test_similarity_is_a_screening_number():
    result = SimilarityService().compare("c1ccccc1", "Cc1ccccc1")
    assert result.tanimoto > 0.2, "benzene and toluene share the benzene core"
    assert result.scaffold_relation == "identical"
    assert result.mcs["found"] is True

    unrelated = SimilarityService().compare("c1ccccc1", "CCO")
    assert unrelated.tanimoto < 0.1
    assert unrelated.scaffold_relation == "disjoint"


# -- §13 test 7: reaction matcher positive/negative ------------------------


def test_reaction_matcher_positive_and_negative():
    matcher = ReactionMatcher()
    alkyne = [m.template.name for m in matcher.match(normalize("C#Cc1ccccc1"))]
    assert "Hydrogenation" in alkyne
    assert "Partial Reduction (Lindlar)" in alkyne
    assert "Dissolving-Metal Reduction" in alkyne
    aromatic = [m.template.name for m in matcher.match(normalize("c1ccccc1"))]
    assert "Aromatic Nitration" in aromatic
    assert "Primary Alcohol Oxidation" not in aromatic
    assert matcher.match(normalize("CCO"))[0].matched_atoms, "a match carries atoms"


# -- §13 edge cases ---------------------------------------------------------


def test_invalid_molecule_is_honest():
    result = _inspect("not-a-smiles")
    assert result.valid is False
    assert result.error


def test_phenylacetylene_matches_multiple_alkyne_reactions():
    result = _inspect("C#Cc1ccccc1")
    names = _reactions(result)
    assert "Hydration" in names
    assert "Dissolving-Metal Reduction" in names
    assert "Aromatic Nitration" in names  # the aromatic ring also matches EAS
    assert len(names) >= 8, "alkyne reactions plus aromatic EAS"


# -- API endpoint smoke -----------------------------------------------------


def test_molecule_inspect_endpoint(tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        payload = c.get("/api/molecule/inspect", params={"smiles": "CCO"}).json()
        assert payload["valid"] is True
        assert payload["properties"]["molecular_formula"] == "C2H6O"
        assert any(g["name"] == "Alcohol" for g in payload["functional_groups"])
        assert any(m["template"]["name"] == "Primary Alcohol Oxidation"
                   for m in payload["reactions"])

"""Route explorer (Stage 5.4).

A bounded, forward-only, deterministic route search. Production templates carry
no reaction transform, so the complete-route path is exercised with test
templates that do -- never by adding transforms to the library. Target matching
is by standard identity, products are never guessed, and no probability field
exists.
"""

from __future__ import annotations

import pytest

from crea.chem.normalize import NormalizationError
from crea.core.errors import RouteExplorerError
from crea.core.models.reaction_template import (
    ReactionClassFamily,
    ReactionTemplate,
    Transformation,
)
from crea.core.models.route_explorer import RouteStatus
from crea.services.reaction_library import SEED_REACTION_TEMPLATES
from crea.services.route_explorer import MAX_STEPS, RouteExplorerService
from crea.services.transformation_search import TransformationSearchService


def _template(template_id, name, substrate, transform, outputs):
    return ReactionTemplate(
        template_id=template_id, name=name,
        class_family=ReactionClassFamily.OXIDATION,
        reaction_class="test_transform",
        transformation=Transformation(input_features=["x"], output_features=outputs,
                                      note=name),
        substrate_patterns=[substrate], reaction_smarts=transform)


# alcohol -> aldehyde, and aldehyde -> acid: a clean 2-step chain CCO -> CC=O -> CC(=O)O
_OXIDATION = _template("test-oxidation", "Test Alcohol to Aldehyde",
                       "[C;H2,H3][OX2H]", "[C:1][OX2H:2]>>[C:1]=[O:2]", ["aldehyde"])
_ACID = _template("test-acid", "Test Aldehyde to Acid",
                  "[CX3H1](=O)[#6]", "[CX3H1:1]=[O:2]>>[CX3:1](=[O:2])[OX2H]",
                  ["carboxylic_acid"])
_NO_TRANSFORM = _template("test-no-transform", "Test No Transform",
                          "[C;H2,H3][OX2H]", "", ["?"])


def _service(templates):
    return RouteExplorerService(
        search_service=TransformationSearchService(templates=templates))


# -- 1. one-step complete ----------------------------------------------------


def test_one_step_complete_route():
    result = _service([_OXIDATION]).explore("CCO", "CC=O")
    complete = result.by_status(RouteStatus.COMPLETE)
    assert len(complete) == 1
    route = complete[0]
    assert route.status is RouteStatus.COMPLETE
    assert len(route.steps) == 1
    step = route.steps[0]
    assert step.reaction_template_id == "test-oxidation"
    assert step.input_smiles == "CCO" and step.output_smiles == "CC=O"


# -- 2. two-step complete ----------------------------------------------------


def test_two_step_complete_route():
    result = _service([_OXIDATION, _ACID]).explore("CCO", "CC(=O)O")
    complete = result.by_status(RouteStatus.COMPLETE)
    assert complete
    route = complete[0]
    assert len(route.steps) == 2
    assert [s.reaction_template_id for s in route.steps] == [
        "test-oxidation", "test-acid"]
    assert route.steps[0].output_smiles == route.steps[1].input_smiles


# -- 3. no route -------------------------------------------------------------


def test_no_route_returns_empty():
    result = _service([_OXIDATION]).explore("CCO", "c1ccccc1")
    assert result.by_status(RouteStatus.COMPLETE) == []
    assert any("no complete route" in d for d in result.diagnostics)


# -- 4. product unavailable is honest ----------------------------------------


def test_product_unavailable_is_partial_not_complete():
    result = _service([_NO_TRANSFORM]).explore("CCO", "CC=O")
    assert result.by_status(RouteStatus.COMPLETE) == []
    partial = result.by_status(RouteStatus.PARTIAL)
    assert partial
    assert partial[0].blocked_reason == "PRODUCT_STRUCTURE_UNAVAILABLE"
    assert partial[0].blocked_reaction_id == "test-no-transform"
    assert partial[0].steps == []  # no fabricated product step


def test_production_library_yields_no_complete_route():
    # the 46 seed templates carry no transform: nothing can complete
    result = RouteExplorerService().explore("CCO", "CC=O")
    assert result.by_status(RouteStatus.COMPLETE) == []


# -- 5. target identity normalization ----------------------------------------


def test_target_identity_normalization():
    # OCC and CCO are the same molecule; a route to CCO is complete for OCC
    result = _service([_OXIDATION]).explore("OCC", "CC=O")
    assert result.by_status(RouteStatus.COMPLETE)


# -- 6. depth is capped and validated ----------------------------------------


def test_max_steps_out_of_range_is_a_structured_error():
    with pytest.raises(RouteExplorerError) as exc:
        _service([_OXIDATION]).explore("CCO", "CC=O", max_steps=99)
    assert exc.value.code == "INVALID_ROUTE_DEPTH"
    with pytest.raises(RouteExplorerError):
        _service([_OXIDATION]).explore("CCO", "CC=O", max_steps=0)


def test_invalid_smiles_raises_for_the_caller_to_map():
    with pytest.raises(NormalizationError):
        _service([_OXIDATION]).explore("not-a-smiles", "CC=O")


# -- 7. cycle avoidance ------------------------------------------------------


def test_cycles_do_not_loop_forever():
    reverse = _template("test-reverse", "Test Aldehyde to Alcohol",
                        "[CX3H1](=O)[#6]", "[C:1]=[O:2]>>[C:1][OX2H:2]", ["alcohol"])
    result = _service([_OXIDATION, reverse]).explore("CCO", "CCN", max_steps=MAX_STEPS)
    # terminates; nothing reaches the (unrelated) target
    assert result.by_status(RouteStatus.COMPLETE) == []
    assert len(result.paths) < 25


# -- 8. deterministic, deduplicated, in a stable order -----------------------


def test_output_is_deterministic():
    svc = _service([_OXIDATION, _ACID])
    assert svc.explore("CCO", "CC(=O)O").model_dump() == \
        svc.explore("CCO", "CC(=O)O").model_dump()


def test_complete_routes_are_ordered_before_partial():
    result = _service([_OXIDATION, _ACID, _NO_TRANSFORM]).explore("CCO", "CC(=O)O")
    ranks = [0 if p.status is RouteStatus.COMPLETE else 1 for p in result.paths]
    assert ranks == sorted(ranks)


# -- 9. no probability / yield / recommendation fields -----------------------


def test_no_confidence_or_yield_fields():
    result = _service([_OXIDATION]).explore("CCO", "CC=O")
    payload = result.model_dump(mode="json")
    banned = ("route_confidence", "route_probability", "success_probability",
              "predicted_yield", "yield", "best_route", "recommended_route")
    for path in payload["paths"]:
        for field in banned:
            assert field not in path
    for field in banned:
        assert field not in payload


# -- 10. API -----------------------------------------------------------------


def test_route_search_api(tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    with TestClient(app) as c:
        body = c.post("/api/routes/search", json={
            "start_molecule": "CCO", "target_molecule": "CC=O"}).json()
        # production templates have no transform -> no complete route, but the
        # matched-but-product-unknown path is reported honestly
        assert body["routes"] == []
        assert body["count"] >= 1
        assert any(p["blocked_reason"] == "PRODUCT_STRUCTURE_UNAVAILABLE"
                   for p in body["partial_routes"])

        assert c.post("/api/routes/search", json={
            "start_molecule": "CCO", "target_molecule": "CC=O",
            "max_steps": 99}).status_code == 400
        assert c.post("/api/routes/search", json={
            "start_molecule": "not-a-smiles",
            "target_molecule": "CC=O"}).status_code == 400
        assert c.post("/api/routes/search", json={
            "start_molecule": "CCO"}).status_code == 400

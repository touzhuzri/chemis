"""Reaction Explorer (Stage 3.2): list / filter / search / detail over HTTP.

The 46 curated ReactionTemplates are browsable through the API. Everything is
serialized from the Stage 3.1 structured model -- class family, transformation,
patterns, reagents, conditions, selectivity, evidence status -- and an unknown
template_id is a structured 404, never a stack trace.
"""

from __future__ import annotations

from crea.services.reaction_library import get_template, list_templates


def _client(tmp_path):
    from crea.api.app import create_app
    from crea.api.dependencies import create_context
    from fastapi.testclient import TestClient

    ctx = create_context(db_path=tmp_path / "crea.db",
                         documents_dir=tmp_path / "docs", offline=True)
    app = create_app(context=ctx, web_root=tmp_path / "no-web")
    return TestClient(app)


# -- direct service helpers -------------------------------------------------


def test_list_templates_returns_all_seed_reactions():
    templates = list_templates()
    assert len(templates) == 46


def test_get_template_by_id():
    template = get_template("primary-alcohol-oxidation")
    assert template is not None
    assert template.name == "Primary Alcohol Oxidation"
    assert get_template("nope") is None


# -- API: list / filter / search / detail ----------------------------------


def test_reaction_api_lists_all_templates(tmp_path):
    with _client(tmp_path) as c:
        payload = c.get("/api/reaction-templates").json()
        assert payload["count"] == 46
        assert len(payload["reactions"]) == 46
        template = payload["reactions"][0]
        assert template["template_id"] and template["name"]
        assert template["class_family"]
        assert template["transformation"]["input_features"]


def test_reaction_api_class_filter(tmp_path):
    with _client(tmp_path) as c:
        payload = c.get("/api/reaction-templates",
                        params={"class_family": "oxidation"}).json()
        assert payload["reactions"]
        assert all(t["class_family"] == "oxidation"
                   for t in payload["reactions"])


def test_reaction_api_name_search(tmp_path):
    with _client(tmp_path) as c:
        payload = c.get("/api/reaction-templates", params={"q": "oxidation"}).json()
        assert payload["reactions"]
        assert all("oxidation" in t["name"].lower()
                   for t in payload["reactions"])


def test_reaction_api_detail_returns_full_structured_model(tmp_path):
    with _client(tmp_path) as c:
        template = c.get("/api/reaction-templates/primary-alcohol-oxidation").json()
        assert template["template_id"] == "primary-alcohol-oxidation"
        assert template["class_family"] == "oxidation"
        assert template["transformation"]["input_features"] == ["primary_alcohol"]
        assert template["transformation"]["output_features"] == ["aldehyde"]
        assert template["substrate_patterns"]
        assert template["evidence_status"] == "curated"


def test_reaction_api_unknown_template_is_404(tmp_path):
    with _client(tmp_path) as c:
        assert c.get("/api/reaction-templates/no-such-template").status_code == 404


def test_every_template_is_findable_with_no_duplicate_ids(tmp_path):
    with _client(tmp_path) as c:
        templates = c.get("/api/reaction-templates").json()["reactions"]
        ids = [t["template_id"] for t in templates]
        assert len(ids) == len(set(ids)) == 46
        for template_id in ids:
            assert c.get(f"/api/reaction-templates/{template_id}").status_code == 200

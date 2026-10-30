"""M1.0 -- the tool-surface census.

The M0 audit found 44 registrations across three registries and 39 distinct
names. This test freezes that census so a later change is a deliberate edit,
not a quiet drift.

What is pinned, and why:

* the exact name list of each registry -- adding or removing a tool fails
  here until the baseline below is updated;
* the duplicate names and *where* they duplicate -- `search_literature` is
  three tools with three surfaces, and merging them is an M1.1 decision;
* a canonical-name / alias map -- old names stay callable, so no caller is
  broken while the surface is unified;
* the argument-schema differences between the same-named tools -- this is
  the evidence that they cannot simply be renamed into one another;
* that building the registries and calling `describe()` executes no tool
  body, opens no database and touches no network.

Nothing here runs a tool, reaches a provider or writes storage. The three
registries are built with backends that raise if any tool body is invoked.
"""

from __future__ import annotations

import pytest

from crea.agent.evidence_tools import build_evidence_tool_registry
from crea.agent.toolsets import build_registry
from crea.tools.registry import default_tool_registry


# ---------------------------------------------------------------------------
# The pinned baseline (M1.0, verified against the source on this tree)
# ---------------------------------------------------------------------------

RESEARCH_TOOLS = frozenset({
    "analyze_molecule",
    "compare_structures",
    "create_claim",
    "extract_reactions",
    "get_document_evidence",
    "get_entity",
    "get_evidence_for_reaction",
    "get_existing_knowledge",
    "get_knowledge_gaps",
    "get_papers_for_entity",
    "get_reaction",
    "get_reaction_details",
    "get_routes_for_entity",
    "lookup_paper",
    "resolve_chemical_identity",
    "retrosynthesize",
    "route_evidence_report",
    "search_entities",
    "search_knowledge",
    "search_literature",
    "search_local_document",
    "search_reaction_evidence",
    "search_reactions",
    "synthesize_evidence",
    "validate_reaction",
    "validate_route_step",
})

WORKSPACE_TOOLS = frozenset({
    "analyze_structure",
    "compare_structures",
    "explore_chemical_space",
    "find_chemical_transformations",
    "name_structure",
    "resolve_chemical",
    "retrosynthesize",
    "search_chemical",
    "search_literature",
    "search_reactions",
})

EVIDENCE_TOOLS = frozenset({
    "compare_reactions",
    "get_reaction_template",
    "inspect_molecule",
    "literature_for_paper",
    "reaction_evidence",
    "route_search",
    "search_literature",
    "search_transformations",
})

BASELINE = {
    "research": RESEARCH_TOOLS,
    "workspace": WORKSPACE_TOOLS,
    "evidence": EVIDENCE_TOOLS,
}

#: The same name registered more than once, and where.
DUPLICATE_NAMES = {
    "compare_structures": ("research", "workspace"),
    "search_literature": ("research", "workspace", "evidence"),
    "search_reactions": ("research", "workspace"),
    "retrosynthesize": ("research", "workspace"),
}

#: (registry, registered name) -> the one name the agent surface should use.
#: Same-named tools keep their name as canonical; the M1.1 unification is a
#: schema decision, not a naming decision, and is recorded in
#: SCHEMA_DIVERGENCE below.
CANONICAL_OF = {
    # -- research registry (the future agent surface) ----------------------
    ("research", "analyze_molecule"): "analyze_molecule",
    ("research", "compare_structures"): "compare_structures",
    ("research", "create_claim"): "create_claim",
    ("research", "extract_reactions"): "extract_reactions",
    ("research", "get_document_evidence"): "get_document_evidence",
    ("research", "get_entity"): "get_entity",
    ("research", "get_evidence_for_reaction"): "get_evidence_for_reaction",
    ("research", "get_existing_knowledge"): "get_existing_knowledge",
    ("research", "get_knowledge_gaps"): "get_knowledge_gaps",
    ("research", "get_papers_for_entity"): "get_papers_for_entity",
    ("research", "get_reaction"): "get_reaction",
    ("research", "get_reaction_details"): "get_reaction_details",
    ("research", "get_routes_for_entity"): "get_routes_for_entity",
    ("research", "lookup_paper"): "lookup_paper",
    ("research", "resolve_chemical_identity"): "resolve_chemical_identity",
    ("research", "retrosynthesize"): "retrosynthesize",
    ("research", "route_evidence_report"): "route_evidence_report",
    ("research", "search_entities"): "search_entities",
    ("research", "search_knowledge"): "search_knowledge",
    ("research", "search_literature"): "search_literature",
    ("research", "search_local_document"): "search_local_document",
    ("research", "search_reaction_evidence"): "search_reaction_evidence",
    ("research", "search_reactions"): "search_reactions",
    ("research", "synthesize_evidence"): "synthesize_evidence",
    ("research", "validate_reaction"): "validate_reaction",
    ("research", "validate_route_step"): "validate_route_step",
    # -- workspace registry (chat) -----------------------------------------
    ("workspace", "analyze_structure"): "analyze_molecule",
    ("workspace", "compare_structures"): "compare_structures",
    ("workspace", "explore_chemical_space"): "explore_chemical_space",
    ("workspace", "find_chemical_transformations"): "find_chemical_transformations",
    ("workspace", "name_structure"): "name_structure",
    ("workspace", "resolve_chemical"): "resolve_chemical_identity",
    ("workspace", "retrosynthesize"): "retrosynthesize",
    ("workspace", "search_chemical"): "search_chemical",
    ("workspace", "search_literature"): "search_literature",
    ("workspace", "search_reactions"): "search_reactions",
    # -- evidence registry (read-only) -------------------------------------
    ("evidence", "compare_reactions"): "compare_reaction_templates",
    ("evidence", "get_reaction_template"): "get_reaction_template",
    ("evidence", "inspect_molecule"): "analyze_molecule",
    ("evidence", "literature_for_paper"): "get_evidence_for_paper",
    ("evidence", "reaction_evidence"): "get_evidence_for_reaction",
    ("evidence", "route_search"): "explore_routes",
    ("evidence", "search_literature"): "search_literature",
    ("evidence", "search_transformations"): "search_reaction_templates",
}

#: Old names that remain callable as aliases of a canonical name.
ALIASES = {
    "analyze_molecule": {
        "analyze_structure": ("workspace",),
        "inspect_molecule": ("evidence",),
    },
    "resolve_chemical_identity": {
        "resolve_chemical": ("workspace",),
    },
    "get_evidence_for_reaction": {
        "reaction_evidence": ("evidence",),
    },
    "get_evidence_for_paper": {
        "literature_for_paper": ("evidence",),
    },
    "explore_routes": {
        "route_search": ("evidence",),
    },
    "search_reaction_templates": {
        "search_transformations": ("evidence",),
    },
    "compare_reaction_templates": {
        "compare_reactions": ("evidence",),
    },
}

#: The same-named tools whose *surfaces* differ. Inputs are checked below;
#: output differences are recorded in the M1.0 report.
SCHEMA_DIVERGENCE = {
    # research has a records mode; the other two do not.
    "search_literature": {"research": {"include_records"},
                          "workspace": set(), "evidence": set()},
    # research searches by reaction key / structure / plural reactant ids;
    # workspace uses a single reactant id and no key/structure-mode.
    "search_reactions": {"research": {"reaction_key", "structure_mode",
                                      "reactant_entity_ids"},
                         "workspace": {"reactant_entity_id"}},
    # research accepts max_transforms; workspace does not.
    "retrosynthesize": {"research": {"max_transforms"},
                        "workspace": set()},
}


class _Exploding:
    """A backend that fails if a tool body ever touches it."""

    def __getattr__(self, name):
        def boom(*_args, **_kwargs):
            raise AssertionError(f"a tool body executed and used {name!r}")

        return boom


def _build_all():
    research = build_registry(_Exploding(), _Exploding(), [],
                              routes=_Exploding())
    workspace = default_tool_registry(explorer=_Exploding(),
                                      transformation_service=_Exploding())
    evidence = build_evidence_tool_registry(
        transformation_search=_Exploding(), comparison=_Exploding(),
        evidence=_Exploding(), literature=_Exploding(), route=_Exploding())
    return {"research": research, "workspace": workspace, "evidence": evidence}


def _schema(registry, name: str) -> dict:
    spec = registry.get(name)
    if hasattr(spec, "arguments_model"):
        return spec.arguments_model.model_json_schema()
    return spec.describe()["arguments"]


# ---------------------------------------------------------------------------
# Census
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", sorted(BASELINE))
def test_registry_census_matches_the_pinned_baseline(label):
    registries = _build_all()
    assert set(registries[label].names()) == BASELINE[label]


def test_total_registrations_and_unique_names():
    registries = _build_all()
    total = sum(len(r.names()) for r in registries.values())
    unique = {name for r in registries.values() for name in r.names()}
    assert total == 44
    assert len(unique) == 39


def test_duplicate_names_are_exactly_the_documented_ones():
    registries = _build_all()
    found: dict[str, list[str]] = {}
    for label, registry in registries.items():
        for name in registry.names():
            found.setdefault(name, []).append(label)
    computed = {name: tuple(labels) for name, labels in found.items()
                if len(labels) > 1}
    assert computed == DUPLICATE_NAMES


def test_every_registration_has_one_canonical_name():
    registries = _build_all()
    registered = {(label, name)
                  for label, registry in registries.items()
                  for name in registry.names()}
    assert set(CANONICAL_OF) == registered
    assert len(CANONICAL_OF) == 44


def test_old_names_survive_as_aliases():
    """The alias map is complete: every renamed registration is covered, and
    every alias points at a canonical name that exists in the map."""
    renamed = {(label, name) for (label, name), canonical in CANONICAL_OF.items()
               if name != canonical}
    expected: dict[str, dict[str, tuple[str, ...]]] = {}
    for label, name in sorted(renamed):
        canonical = CANONICAL_OF[(label, name)]
        expected.setdefault(canonical, {}).setdefault(name, ())
        expected[canonical][name] += (label,)
    assert expected == ALIASES
    canonical_names = set(CANONICAL_OF.values())
    for canonical, aliases in ALIASES.items():
        assert canonical in canonical_names
        for alias in aliases:
            assert alias != canonical


def test_same_named_tools_really_have_different_surfaces():
    """A rename alone would be a lie: these tools do not take the same input."""
    registries = _build_all()
    for name, by_registry in SCHEMA_DIVERGENCE.items():
        for label, expected in by_registry.items():
            if label not in registries:
                continue
            schema = _schema(registries[label], name)
            props = set(schema.get("properties", {}))
            assert expected <= props, (
                f"{label}:{name} should expose {sorted(expected)}, "
                f"has {sorted(props)}")
        # and the divergence is real, not cosmetic
        research = set(_schema(registries["research"], name)["properties"])
        for label, expected in by_registry.items():
            if label == "research":
                continue
            other = set(_schema(registries[label], name)["properties"])
            assert research != other, (
                f"{name} / {label} is no longer divergent; M1.1 may have "
                f"unified it -- update this baseline deliberately")


def test_compare_structures_keeps_one_input_shape_but_two_output_shapes():
    registries = _build_all()
    research = _schema(registries["research"], "compare_structures")
    workspace = _schema(registries["workspace"], "compare_structures")
    assert research["properties"] == workspace["properties"]
    assert research["required"] == workspace["required"] == [
        "smiles_a", "smiles_b"]
    # the surfaces differ in what they return, which is why the duplicate is
    # recorded rather than merged by name
    r_desc = registries["research"].get("compare_structures").description
    w_desc = registries["workspace"].get("compare_structures").description
    assert r_desc != w_desc


def test_every_tool_declares_a_json_object_schema():
    for registry in _build_all().values():
        for entry in registry.describe():
            arguments = entry["arguments"]
            assert arguments.get("type") == "object", entry["name"]
            assert isinstance(arguments.get("properties"), dict), entry["name"]


def test_describe_is_deterministic():
    registries = _build_all()
    for registry in registries.values():
        assert registry.describe() == registry.describe()


# ---------------------------------------------------------------------------
# No execution, no storage, no network
# ---------------------------------------------------------------------------


def test_building_and_describing_executes_no_tool_body():
    """All three registries are built over backends that raise on any use.

    If a tool body ran during construction or `describe()`, this test fails
    with that backend's name -- which is also the proof that the census
    itself has no persistence side effect.
    """
    registries = _build_all()
    for registry in registries.values():
        assert registry.describe()  # only schemas; no run()


def test_no_tool_is_registered_without_a_description():
    for registry in _build_all().values():
        for entry in registry.describe():
            assert entry["description"].strip(), entry["name"]

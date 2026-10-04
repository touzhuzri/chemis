"""ToolRegistry regression: nested argument models must reach the tool intact.

A tool's arguments are validated against its model and then handed over via
`model_dump()`, which flattens nested models to plain dicts. `route_evidence_report`
takes `record_links: list[_RecordLink]` and used to read `spec.step_id` straight
off those dicts -- so any non-empty `record_links` crashed with `AttributeError`
before doing any work. The repair is local (`_RecordLink.model_validate` where
the specs are read); this test pins the path that broke: `ToolRegistry.execute`
with a non-empty `record_links`, end to end.
"""

from __future__ import annotations

from crea.agent.offline import build_offline_workflow_stack
from crea.core.models.research import ToolStatus

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"


def _registry_with_a_route():
    _workflow, _library, _sink, registry = build_offline_workflow_stack()
    result = registry.execute("retrosynthesize", {"target_smiles": DIYNE})
    assert result.status is ToolStatus.OK
    assert result.data["route_count"] >= 1
    return registry, result.data


def test_route_evidence_report_accepts_record_links_through_the_registry():
    """The exact call that used to die: execute -> route_evidence_report ->
    non-empty record_links -> normal execution."""
    registry, retro = _registry_with_a_route()
    step = retro["routes"][0]["steps"][0]

    result = registry.execute("route_evidence_report", {
        "record_links": [{
            "step_id": step["step_id"],
            "reaction_id": "rxn-not-held",
            "reactant_smiles": [m["canonical_smiles"]
                                for m in step["reactants"]],
            "product_smiles": [m["canonical_smiles"] for m in step["products"]],
        }],
    })

    assert result.status is ToolStatus.OK, result.error
    assert result.data["route_count"] >= 1
    reports = result.data["reports"]
    assert reports
    chain_step = next(s for r in reports for s in r["steps"]
                      if s["step_id"] == step["step_id"])

    # the repair changed no semantics: a link naming a record this task does
    # not hold supports nothing, and says so rather than being dropped
    assert chain_step["evidence_level"] == "retrosynthesis_candidate"
    assert chain_step["experimental_support"] == []
    assert any("not among the records this task holds" in w
               for w in chain_step["warnings"])


def test_record_links_for_an_unknown_step_still_refuse_cleanly():
    """The rebuilt spec keeps the refusal path: a step id nobody holds is a
    failed call, not a crash and not a silent pass."""
    registry, _retro = _registry_with_a_route()

    result = registry.execute("route_evidence_report", {
        "record_links": [{
            "step_id": "no-such-step",
            "reaction_id": "rxn-1",
        }],
    })

    assert result.status is ToolStatus.ERROR
    assert "no step" in (result.error or "")


def test_an_empty_record_links_list_still_runs():
    registry, _retro = _registry_with_a_route()

    result = registry.execute("route_evidence_report", {"record_links": []})

    assert result.status is ToolStatus.OK, result.error
    assert result.data["route_count"] >= 1

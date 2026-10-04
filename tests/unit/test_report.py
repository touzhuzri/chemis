"""End-to-end: a real molecule, through every layer, to a readable report."""

import json

from crea.chem.normalize import normalize
from crea.report.render import render_report
from crea.services.matching import MatchOptions, build_match_report
from tests.conftest import (
    ASPIRIN,
    DIPHENYLACETYLENE,
    DIPHENYL_BUTADIYNE,
    O_XYLENE,
    P_XYLENE,
    PHENYLACETYLENE,
)


def _report(a, b, **kwargs):
    return build_match_report(
        normalize(a), normalize(b), label_a="A", label_b="B",
        options=MatchOptions(**kwargs),
    )


def test_R01_identical_inputs_are_identical():
    r = _report(DIPHENYL_BUTADIYNE, DIPHENYL_BUTADIYNE)
    assert r.verdict == "IDENTICAL"
    assert r.screen.tanimoto_ecfp4 == 1.0
    assert r.core.coverage_a == 1.0


def test_R02_positional_isomers_are_separated_at_the_attachment_layer():
    r = _report(P_XYLENE, O_XYLENE)
    assert r.verdict == "REGIO_ISOMER"
    # the three cheaper layers cannot see it ...
    assert r.scaffold.relation == "identical"
    assert r.core.num_atoms == 7
    # ... the attachment layer can
    assert any(
        am.rcad_a == "*c1ccc(*)cc1" and am.rcad_b == "*c1ccccc1*"
        for am in r.attachment_matches
    )
    assert any(am.position_a == "para" and am.position_b == "ortho"
               for am in r.attachment_matches)


def test_R03_phenylacetylene_vs_diphenylacetylene():
    r = _report(PHENYLACETYLENE, DIPHENYLACETYLENE)
    assert r.verdict in ("PARTIAL_OVERLAP", "SCAFFOLD_SHARED")
    assert r.scaffold.relation in ("substructure", "ring_system_shared")
    alkyne = next(am for am in r.attachment_matches if am.rcad_a.startswith("*C#C"))
    assert alkyne.rcad_a == "*C#C"
    assert alkyne.rcad_b == "*C#C*"
    assert alkyne.role_match is False
    assert any("terminal_alkyne" in c for c in alkyne.role_conflicts)
    assert any("internal_alkyne" in c for c in alkyne.role_conflicts)


def test_R04_dissimilar_structures_are_reported_as_such():
    # Sulfuric acid and 1,2-dichlorobenzene share no ring system, no scaffold
    # and no common substructure above the atom level.
    r = _report("OS(=O)(=O)O", "Clc1ccccc1Cl")
    assert r.verdict == "DISSIMILAR"
    assert r.verdict_reasons
    assert r.scaffold.relation == "disjoint"
    assert r.core.found is False


def test_R04c_partial_overlap_is_not_downscaled_to_dissimilar():
    # Aspirin and a pure aliphatic acid share no ring system, but they do share
    # a C-C(=O)O fragment. "DISSIMILAR" is reserved for zero overlap, so this
    # must be PARTIAL_OVERLAP rather than an over-confident "dissimilar".
    r = _report(ASPIRIN, "CCCCCCCCCCCCCCCC(=O)O")
    assert r.verdict == "PARTIAL_OVERLAP"
    assert r.scaffold.relation == "disjoint"


def test_R04b_a_shared_benzene_is_not_called_dissimilar():
    # Aspirin and the diyne do share a benzene ring, so calling them
    # dissimilar would be wrong. This guards against over-claiming.
    r = _report(ASPIRIN, DIPHENYL_BUTADIYNE)
    assert r.verdict == "PARTIAL_OVERLAP"
    assert r.scaffold.relation in ("substructure", "ring_system_shared")


def test_R05_report_renders_every_required_section():
    text = render_report(_report(P_XYLENE, O_XYLENE))
    for heading in (
        "OVERALL FINGERPRINT SIMILARITY",
        "MATCHED SCAFFOLD",
        "COMMON CORE",
        "MATCHED FRAGMENTS",
        "ATTACHMENT DIFFERENCE",
        "DIFFERENT REGIONS",
        "LIMITATIONS",
    ):
        assert heading in text, f"missing section: {heading}"
    assert "*c1ccc(*)cc1" in text
    assert "*c1ccccc1*" in text


def test_R06_report_is_json_serialisable():
    payload = json.loads(
        json.dumps(_report(P_XYLENE, O_XYLENE).model_dump(mode="json"))
    )
    assert payload["verdict"] == "REGIO_ISOMER"
    assert payload["screen"]["screening_only"] is True


def test_R07_optional_decompositions_do_not_break_the_report():
    r = _report(ASPIRIN, PHENYLACETYLENE, run_brics=True, run_recap=True)
    assert r.verdict is not None
    assert any("brics" in lim for lim in r.limitations)
    assert any("recap" in lim for lim in r.limitations)


def test_R08_known_limitations_are_always_disclosed():
    r = _report(P_XYLENE, O_XYLENE)
    assert r.limitations
    assert any("screening number" in lim for lim in r.limitations)
    assert any("not an identity claim" in lim for lim in r.limitations)
    assert any("heuristic" in lim for lim in r.limitations)

"""Render a `StructureMatchReport` as readable text.

Every line is generated from a field that already exists on the report. The
renderer is not allowed to infer, round, or summarize a number that the model
does not carry -- if something is worth saying, it has to be in the model.
"""

from __future__ import annotations

from crea.core.models.report import StructureMatchReport

_VERDICT_BLURB = {
    "IDENTICAL": "the two inputs are the same molecule",
    "REGIO_ISOMER": "same formula, different connectivity or substitution position",
    "STEREOISOMER": "same connectivity, different stereochemistry",
    "SCAFFOLD_SHARED": "a shared ring skeleton, with real differences elsewhere",
    "PARTIAL_OVERLAP": "some common substructure, no shared scaffold",
    "DISSIMILAR": "no meaningful common structure",
}


def _pct(x: float) -> str:
    return f"{x:.0%}"


def render_report(report: StructureMatchReport, width: int = 78) -> str:
    a, b = report.a, report.b
    w = width
    out: list[str] = []
    bar = "=" * w

    out.append(bar)
    out.append(f"STRUCTURE MATCH REPORT".ljust(w))
    out.append(bar)
    for side in (a, b):
        m = side.molecule
        out.append(f"{side.label}: {m.canonical_smiles}")
        out.append(f"  InChIKey      {m.inchikey}")
        out.append(f"  formula       {m.descriptors.molecular_formula}   "
                   f"MW {m.descriptors.mol_wt:.3f}   heavy atoms {m.descriptors.heavy_atom_count}")
        out.append(f"  rings {m.descriptors.num_rings} (aromatic {m.descriptors.num_aromatic_rings})   "
                   f"TPSA {m.descriptors.tpsa:.2f}   logP {m.descriptors.logp:.2f}   "
                   f"charge {m.descriptors.net_charge}")
        if m.normalization_actions:
            out.append(f"  normalization {'; '.join(m.normalization_actions)}")
        if m.warnings:
            out.append(f"  warnings      {'; '.join(m.warnings)}")
    out.append("")

    out.append("VERDICT".ljust(w))
    out.append(f"  {report.verdict}: {_VERDICT_BLURB.get(report.verdict, '')}")
    for reason in report.verdict_reasons:
        out.append(f"    - {reason}")
    out.append("")

    out.append("1. OVERALL FINGERPRINT SIMILARITY  (screening only)".ljust(w))
    s = report.screen
    out.append(f"  ECFP4 Tanimoto   {s.tanimoto_ecfp4:.3f}")
    out.append(f"  ECFP4 Dice       {s.dice_ecfp4:.3f}")
    out.append(f"  FCFP4 Tanimoto   {s.tanimoto_fcfp4:.3f}")
    out.append(f"  MACCS Tanimoto   {s.tanimoto_maccs:.3f}")
    out.append(f"  formula identical: {s.formula_identical}   heavy-atom delta: {s.heavy_atom_delta}")
    out.append(f"  isomers: constitutional={s.constitutional_isomer_hint}  "
               f"stereo={s.stereo_isomer_hint}")
    out.append(f"  ! {s.caveat}")
    out.append("")

    out.append("2. MATCHED SCAFFOLD  (Murcko)".ljust(w))
    sc = report.scaffold
    out.append(f"  {a.label} scaffold: {sc.scaffold_a or '(none)'}")
    out.append(f"  {b.label} scaffold: {sc.scaffold_b or '(none)'}")
    out.append(f"  relation: {sc.relation}")
    if sc.generic_a or sc.generic_b:
        out.append(f"  generic: {sc.generic_a}  |  {sc.generic_b}")
    if sc.ring_systems_a:
        out.append(f"  {a.label} ring systems: {', '.join(sc.ring_systems_a)}")
    if sc.ring_systems_b:
        out.append(f"  {b.label} ring systems: {', '.join(sc.ring_systems_b)}")
    out.append(f"  ! {sc.caveat}")
    out.append("")

    out.append("3. COMMON CORE  (maximum common substructure)".ljust(w))
    c = report.core
    if not c.found:
        out.append("  no common substructure above the atom level")
    else:
        out.append(f"  {c.num_atoms} atoms / {c.num_bonds} bonds")
        out.append(f"  SMARTS        {c.mcs_smarts}")
        out.append(f"  as SMILES     {c.common_substructure_smiles}")
        out.append(f"  coverage      {a.label} {_pct(c.coverage_a)}   "
                   f"{b.label} {_pct(c.coverage_b)}   jaccard {_pct(c.coverage_jaccard)}")
        out.append(f"  mcs profile   {c.params.profile} "
                   f"({c.params.atom_compare} / {c.params.bond_compare})")
        if c.best_alignment:
            al = c.best_alignment
            pairs = ", ".join(f"{i}->{j}" for i, j in list(al.atom_map.items())[:12])
            more = " ..." if len(al.atom_map) > 12 else ""
            out.append(f"  atom map      {pairs}{more}")
            out.append(f"  alignment score {al.score}  ({al.score_breakdown})")
        if c.alternative_alignments:
            out.append(f"  alternative alignments: {len(c.alternative_alignments)} "
                       "(other legitimate correspondences exist)")
        out.append(f"  computed in {c.runtime_ms} ms")
    out.append("")

    out.append("4. MATCHED FRAGMENTS".ljust(w))
    if not report.fragment_matches:
        out.append("  no fragment-level correspondence")
    for m in report.fragment_matches:
        out.append(f"  {m.frag_a_id} <-> {m.frag_b_id}")
        out.append(f"    smiles identical={m.fragment_smiles_equal}  "
                   f"rcads identical={m.rcads_equal}  "
                   f"position={m.position_relation}  "
                   f"embedded={m.embedded_match}")
        if m.local_env_match:
            out.append(f"    local env   {m.local_env_match}")
        if m.role_match is not None:
            out.append(f"    role match  {m.role_match}")
    for side, frags in ((a.label, report.fragments_a), (b.label, report.fragments_b)):
        for f in frags:
            out.append(f"  {side} fragment {f.fragment_smiles:24s} "
                       f"attachments={f.n_attachment_points} rcads={list(f.rcads)}")
            for ap in f.attachments:
                out.append(f"      {ap.ap_id}: parent atom#{ap.parent_atom_idx} "
                           f"via bond#{ap.parent_bond_idx}({ap.bond_order}) "
                           f"-> {','.join(ap.parent_side_elements) or '-'}  rcad={ap.rcad}")
    out.append("")

    out.append("5. ATTACHMENT DIFFERENCE".ljust(w))
    if not report.attachment_matches:
        out.append("  no attachment points to compare")
    for am in report.attachment_matches:
        out.append(f"  {am.frag_a_id} <-> {am.frag_b_id}")
        if am.rcad_a or am.rcad_b:
            out.append(f"    rooted canonical attachment descriptor")
            out.append(f"      {a.label}: {am.rcad_a or '(none)'}")
            out.append(f"      {b.label}: {am.rcad_b or '(none)'}")
        out.append(f"    identical={am.rcad_equal}   position={am.position_relation} "
                   f"({a.label}={am.position_a}, {b.label}={am.position_b})")
        if am.local_env_match:
            out.append(f"    local env   {am.local_env_match}")
        if am.role_match is not None:
            out.append(f"    role match  {am.role_match}")
        for conflict in am.role_conflicts:
            out.append(f"    role conflict: {conflict}")
        for conflict in am.attachment_env_conflicts:
            out.append(f"    attachment conflict: {conflict}")
    out.append("")

    out.append("6. DIFFERENT REGIONS".ljust(w))
    for side, comps in ((a.label, report.core.unmatched_components_a),
                        (b.label, report.core.unmatched_components_b)):
        if not comps:
            out.append(f"  {side}: no unmatched region")
            continue
        for comp in comps:
            out.append(f"  {side} {comp.component_id}: {comp.heavy_atom_count} heavy atom(s), "
                       f"{comp.n_rings} ring(s), {comp.formula}")
            out.append(f"    as SMILES   {comp.canonical_smiles_with_star}")
            if comp.attachment_note:
                out.append(f"    attaches via {comp.attachment_note}")
    out.append("")

    out.append("7. LIMITATIONS".ljust(w))
    for lim in report.limitations:
        out.append(f"  - {lim}")
    out.append("")
    out.append(f"generated {report.generated_at} by "
               f"{report.provenance.tool_version if report.provenance else '?'}")
    return "\n".join(out)

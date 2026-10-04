"""Service layer -- orchestrates the Chemical Core into a match report.

This is the only place that knows the full layer order. It adds no chemistry of
its own and asks no LLM anything; every number in the report was produced by
RDKit upstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crea.chem import align, attachment, fingerprint, fragment, scaffold
from crea.core.models.fragments import FRAGMENT_FORMULA_CAVEAT, Fragment
from crea.core.models.molecule import Molecule, NormalizationProfile
from crea.core.models.report import MatchInput, StructureMatchReport
from crea.chem.version import rdkit_version
from crea.core.provenance import Provenance, utc_now_iso


@dataclass(frozen=True)
class MatchOptions:
    normalization_profile: NormalizationProfile = "parent"
    mcs_profile: str = "relaxed"
    mcs_timeout: int = 10
    fragment_methods: tuple[str, ...] = ("ring_linker",)
    run_brics: bool = False
    run_recap: bool = False


@dataclass
class _Prepared:
    molecule: Molecule
    fragments: dict[str, list[Fragment]] = field(default_factory=dict)


def _prepare(mol: Molecule, opts: MatchOptions) -> _Prepared:
    from crea.chem.fragment import decompose_molecule

    methods = list(opts.fragment_methods)
    if opts.run_brics and "brics" not in methods:
        methods.append("brics")
    if opts.run_recap and "recap" not in methods:
        methods.append("recap")

    frags: dict[str, list[Fragment]] = {}
    for method in methods:
        frags[method] = decompose_molecule(mol, method)
    return _Prepared(molecule=mol, fragments=frags)


def _verdict(report: dict) -> tuple[str, list[str]]:
    mol_a, mol_b = report["a"], report["b"]
    screen, core, scaf = report["screen"], report["core"], report["scaffold"]

    if mol_a.inchikey and mol_a.inchikey == mol_b.inchikey:
        return "IDENTICAL", ["identical canonical InChIKey"]

    if screen.constitutional_isomer_hint or screen.stereo_isomer_hint:
        if screen.stereo_isomer_hint:
            return "STEREOISOMER", [
                "identical molecular formula, heavy-atom count and InChIKey "
                "skeleton block, so connectivity is the same and only "
                "stereochemistry differs",
            ]
        return "REGIO_ISOMER", [
            "identical molecular formula and heavy-atom count but a different "
            "InChIKey skeleton block, so connectivity or substitution position "
            "differs while the formula does not",
            "fingerprint, scaffold and MCS layers cannot see this difference; "
            "it is resolved only at the attachment layer",
        ]

    if scaf.relation in ("identical", "generic_match", "substructure"):
        if core.coverage_jaccard >= 0.85:
            return "SCAFFOLD_SHARED", [
                f"shared Murcko scaffold ({scaf.relation})",
                f"MCS covers {core.num_atoms} atoms / {core.num_bonds} bonds",
            ]
        return "PARTIAL_OVERLAP", [
            f"shared Murcko scaffold ({scaf.relation}) but only "
            f"{core.coverage_jaccard:.0%} of the union is common",
        ]

    if core.found and core.coverage_jaccard > 0.0:
        return "PARTIAL_OVERLAP", [
            f"MCS of {core.num_atoms} atoms with no shared Murcko scaffold"
        ]

    return "DISSIMILAR", ["no shared scaffold and no common substructure above atom level"]


def build_match_report(
    mol_a: Molecule,
    mol_b: Molecule,
    label_a: str = "A",
    label_b: str = "B",
    options: MatchOptions | None = None,
) -> StructureMatchReport:
    opts = options or MatchOptions()

    prep_a, prep_b = _prepare(mol_a, opts), _prepare(mol_b, opts)

    screen = fingerprint.screen_similarity(mol_a, mol_b)
    scaf = scaffold.compare_scaffolds(mol_a, mol_b)
    core = align.find_core_match(
        mol_a, mol_b, profile=opts.mcs_profile, timeout=opts.mcs_timeout
    )

    primary = opts.fragment_methods[0]
    frags_a = prep_a.fragments.get(primary, [])
    frags_b = prep_b.fragments.get(primary, [])
    frag_matches = attachment.match_fragments(frags_a, frags_b)

    frag_by_id = {f.fragment_id: f for f in list(frags_a) + list(frags_b)}
    att_matches = [
        attachment.compare_attachments(
            frag_by_id[fm.frag_a_id], frag_by_id[fm.frag_b_id], core=core
        )
        for fm in frag_matches
        if fm.frag_a_id in frag_by_id and fm.frag_b_id in frag_by_id
    ]

    verdict, reasons = _verdict(
        {
            "a": mol_a, "b": mol_b, "screen": screen, "core": core, "scaffold": scaf,
        }
    )

    limitations = [
        "Fingerprint similarity is a screening number from hashed features; "
        "it is not a chemical conclusion.",
        scaf.caveat,
        "Fragment and attachment layers depend on the decomposition method; "
        f"this report used '{primary}'"
        + (" + brics" if opts.run_brics else "")
        + (" + recap" if opts.run_recap else "")
        + ".",
        FRAGMENT_FORMULA_CAVEAT,
        "Role classification is rule-derived and is a heuristic, not an "
        "experimental fact.",
    ]

    return StructureMatchReport(
        a=MatchInput(label=label_a, molecule=mol_a),
        b=MatchInput(label=label_b, molecule=mol_b),
        screen=screen,
        scaffold=scaf,
        core=core,
        fragments_a=frags_a,
        fragments_b=frags_b,
        fragment_matches=frag_matches,
        attachment_matches=att_matches,
        verdict=verdict,
        verdict_reasons=reasons,
        limitations=limitations,
        generated_at=utc_now_iso(),
        provenance=Provenance(
            tool="crea.services.matching.build_match_report",
            tool_version=rdkit_version(),
            params={
                "mcs_profile": opts.mcs_profile,
                "mcs_timeout": opts.mcs_timeout,
                "fragment_methods": list(opts.fragment_methods),
                "run_brics": opts.run_brics,
                "run_recap": opts.run_recap,
            },
        ),
    )

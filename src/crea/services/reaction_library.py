"""The curated reaction-template library and its matcher (CREA Stage 3.1).

A small, curated library of classical functional-group transformations. Each
`ReactionTemplate` is a structured knowledge object (§21): a class family and
specific class, a structured transformation (input -> output features), SMARTS
substrate patterns, reagents, typical conditions, mechanism, selectivity and
limitations -- all `source_type = CURATED` and `evidence_status = curated`
(§17/§18). These are textbook transformations, not literature citations.

Matching is deterministic SMARTS search; a match means "the structure fits the
substrate pattern" and never "the reaction will succeed" (§20).
"""

from __future__ import annotations

from crea.chem.functional_groups import matches_smarts
from crea.core.models.reaction_template import (
    ConditionSpec,
    EvidenceStatus,
    Limitation,
    ReactionClassFamily,
    ReactionMatch,
    ReactionTemplate,
    ReagentSpec,
    SelectivitySpec,
    Transformation,
)
from crea.core.provenance import Provenance

_SEED_PROVENANCE = Provenance(
    tool="crea-reaction-library", tool_version="seed-v2",
    source="curated_educational")

_ALKENE = "[C]=[C]"
_ALKYNE = "[C]#[C]"
_PRIMARY_ALCOHOL = "[C;H2,H3][OX2H]"
_SECONDARY_ALCOHOL = "[C;H1]([#6])[OX2H]"
_ALDEHYDE = "[CX3H1](=O)[#6]"
_KETONE = "[CX3H0](=O)([#6])[#6]"
_CARBOXYLIC_ACID = "[CX3](=O)[OX2H]"
_ALKYL_HALIDE = "[C;!$(C=O)]-[F,Cl,Br,I]"
#: Stage 3.3 additions.
_AROMATIC = "a"                                       # any aromatic atom (EAS)
_TERMINAL_ALKYNE = "[CH]#[C]"
_PRIMARY_SECONDARY_HALIDE = "[C;H1,H2][F,Cl,Br,I]"    # SN2/E2 substrates
_TERTIARY_HALIDE = "[C;H0]([#6])([#6])[F,Cl,Br,I]"    # SN1/E1 substrates
_AMINE = "[NX3;H2,H1;!$(N=O);!$(N[#6]=O)]"
_NITRILE = "[C]#[N]"
_EPOXIDE = "C1OC1"
_ESTER = "[CX3](=O)[OX2][#6]"
_ALCOHOL = "[C;!$(C=O)][OX2H]"                        # any alcohol, not acid

#: A product SMARTS that could not be reliably encoded is stated, never faked.
_PRODUCT_NOT_ENCODED = "product pattern not yet encoded"


def _split(text: str) -> tuple[list[str], list[str]]:
    left, _, right = text.partition("->")
    inputs = [t.strip() for t in left.split("+") if t.strip()]
    outputs = [t.strip() for t in right.split("+") if t.strip()]
    return inputs, outputs


def _t(template_id, name, family, reaction_class, transformation, substrate,
       reagent="", mechanism="", conditions=(), limitations="",
       selectivity=None, product_patterns=()) -> ReactionTemplate:
    inputs, outputs = _split(transformation)
    patterns = [substrate] if isinstance(substrate, str) else list(substrate)
    return ReactionTemplate(
        template_id=template_id, name=name, class_family=family,
        reaction_class=reaction_class,
        transformation=Transformation(input_features=inputs,
                                      output_features=outputs,
                                      note=transformation),
        substrate_patterns=patterns,
        product_patterns=list(product_patterns),
        product_pattern_note=("" if product_patterns else _PRODUCT_NOT_ENCODED),
        reagents=[ReagentSpec(name=reagent)] if reagent else [],
        conditions=[ConditionSpec(kind="typical", value=c) for c in conditions],
        mechanism_class=mechanism,
        selectivity=selectivity,
        limitations=[Limitation(text=limitations)] if limitations else [],
        source_id=f"seed/{template_id}",
        evidence_status=EvidenceStatus.CURATED,
        provenance=_SEED_PROVENANCE)


SEED_REACTION_TEMPLATES: list[ReactionTemplate] = [
    # -- alkenes ------------------------------------------------------------
    _t("alkene-hydrogenation", "Catalytic Hydrogenation",
       ReactionClassFamily.REDUCTION, "hydrogenation", "alkene -> alkane",
       _ALKENE, "H2 / Pt, Pd, or Ni",
       "heterogeneous catalytic reduction",
       ("H2 gas, metal catalyst, room temperature or heat",),
       "needs a C=C double bond; aromatic rings are not reduced under these conditions"),
    _t("alkene-halogenation", "Halogenation",
       ReactionClassFamily.ADDITION, "halogenation",
       "alkene -> vicinal_dihalide", _ALKENE, "Br2 or Cl2 (in CCl4)",
       "electrophilic addition via a halonium ion",
       ("Br2 or Cl2, inert solvent (CCl4), room temperature",),
       "anti addition; stereospecific",
       selectivity=SelectivitySpec(stereoselectivity="anti")),
    _t("alkene-hydrohalogenation", "Hydrohalogenation",
       ReactionClassFamily.ADDITION, "hydrohalogenation",
       "alkene -> alkyl_halide", _ALKENE, "HX (HCl, HBr, HI)",
       "electrophilic addition",
       ("HX, room temperature or mild heat",),
       "carbocation rearrangements possible",
       selectivity=SelectivitySpec(regioselectivity="Markovnikov")),
    _t("alkene-acid-hydration", "Acid-catalyzed Hydration",
       ReactionClassFamily.ADDITION, "hydration", "alkene -> alcohol",
       _ALKENE, "H2O / H2SO4 or H3PO4",
       "electrophilic addition",
       ("dilute strong acid, water, heat",),
       "rearrangements possible",
       selectivity=SelectivitySpec(regioselectivity="Markovnikov")),
    _t("alkene-hydroboration-oxidation", "Hydroboration-Oxidation",
       ReactionClassFamily.ADDITION, "hydroboration_oxidation",
       "alkene -> alcohol", _ALKENE, "1) BH3/THF 2) H2O2, NaOH",
       "syn addition, anti-Markovnikov",
       ("BH3 then basic H2O2, two steps",),
       "anti-Markovnikov and syn stereochemistry",
       selectivity=SelectivitySpec(regioselectivity="anti-Markovnikov",
                                   stereoselectivity="syn")),
    _t("alkene-epoxidation", "Epoxidation",
       ReactionClassFamily.OXIDATION, "epoxidation", "alkene -> epoxide",
       _ALKENE, "mCPBA (or a peracid)",
       "pericyclic oxidation, syn",
       ("peracid (mCPBA), chlorinated solvent, low temperature",),
       "syn stereospecific addition",
       selectivity=SelectivitySpec(stereoselectivity="syn")),
    _t("alkene-oxidative-cleavage", "Oxidative Cleavage",
       ReactionClassFamily.OXIDATION, "oxidative_cleavage",
       "alkene -> carbonyl", _ALKENE, "O3 then Zn/H2O, or KMnO4",
       "oxidative C=C cleavage",
       ("ozonolysis then reductive workup; or hot KMnO4",),
       "cleaves the double bond into two carbonyls"),
    # -- alkynes ------------------------------------------------------------
    _t("alkyne-hydrogenation", "Hydrogenation",
       ReactionClassFamily.REDUCTION, "hydrogenation", "alkyne -> alkane",
       _ALKYNE, "excess H2 / Pt, Pd, or Ni",
       "catalytic reduction, two additions",
       ("excess H2, metal catalyst",),
       "reduces the triple bond fully to a single bond"),
    _t("alkyne-partial-reduction", "Partial Reduction (Lindlar)",
       ReactionClassFamily.REDUCTION, "partial_hydrogenation",
       "alkyne -> cis_alkene", _ALKYNE, "H2 / Lindlar catalyst",
       "syn addition, poisoned catalyst stops at the alkene",
       ("H2, Lindlar (Pd/CaCO3, quinoline)",),
       "gives the cis (Z) alkene stereospecifically",
       selectivity=SelectivitySpec(stereoselectivity="syn (cis)")),
    _t("alkyne-hydrohalogenation", "Hydrohalogenation",
       ReactionClassFamily.ADDITION, "hydrohalogenation",
       "alkyne -> haloalkene", _ALKYNE, "HX (HCl, HBr)",
       "electrophilic addition",
       ("HX, two equivalents for the dihalide",),
       "second equivalent gives a geminal dihalide",
       selectivity=SelectivitySpec(regioselectivity="Markovnikov")),
    _t("alkyne-hydration", "Hydration",
       ReactionClassFamily.ADDITION, "hydration", "alkyne -> ketone",
       _ALKYNE, "HgSO4 / H2SO4 / H2O",
       "Markovnikov addition then enol-keto tautomerization",
       ("Hg(II) catalysis, aqueous acid",),
       "terminal alkynes give methyl ketones; internal alkynes give ketone mixtures"),
    # -- alcohols -----------------------------------------------------------
    _t("primary-alcohol-oxidation", "Primary Alcohol Oxidation",
       ReactionClassFamily.OXIDATION, "alcohol_oxidation",
       "primary_alcohol -> aldehyde", _PRIMARY_ALCOHOL, "PCC, or Cr(VI) / Swern",
       "two-electron oxidation of the carbinol carbon",
       ("PCC in dichloromethane, or Swern conditions",),
       "over-oxidation to the carboxylic acid must be avoided with mild oxidants"),
    _t("secondary-alcohol-oxidation", "Secondary Alcohol Oxidation",
       ReactionClassFamily.OXIDATION, "alcohol_oxidation",
       "secondary_alcohol -> ketone", _SECONDARY_ALCOHOL, "Na2Cr2O7 / H2SO4, or PCC",
       "two-electron oxidation of the carbinol carbon",
       ("chromium oxidant or PCC, room temperature",),
       "tertiary alcohols do not oxidise by this route"),
    # -- carbonyls ----------------------------------------------------------
    _t("aldehyde-nucleophilic-addition", "Nucleophilic Addition",
       ReactionClassFamily.ADDITION, "nucleophilic_addition",
       "aldehyde -> alcohol", _ALDEHYDE, "RMgX / RLi, or NaBH4",
       "nucleophilic attack at the carbonyl carbon",
       ("organometallic nucleophile, anhydrous, low temperature",),
       "broad; the exact adduct depends on the nucleophile"),
    _t("ketone-reduction", "Ketone Reduction",
       ReactionClassFamily.REDUCTION, "ketone_reduction",
       "ketone -> alcohol", _KETONE, "NaBH4 or LiAlH4",
       "hydride addition to the carbonyl",
       ("NaBH4 in alcohol, room temperature",),
       "LiAlH4 reduces more aggressively; NaBH4 is chemoselective"),
    # -- carboxylic acids ---------------------------------------------------
    _t("carboxylic-acid-reduction", "Carboxylic Acid Reduction",
       ReactionClassFamily.REDUCTION, "carboxylic_acid_reduction",
       "carboxylic_acid -> primary_alcohol", _CARBOXYLIC_ACID, "LiAlH4",
       "hydride reduction of the carboxyl group",
       ("LiAlH4 in ether, then aqueous workup",),
       "requires a strong hydride donor (LiAlH4, not NaBH4)"),
    # -- alkyl halides ------------------------------------------------------
    _t("alkyl-halide-substitution", "Nucleophilic Substitution",
       ReactionClassFamily.SUBSTITUTION, "nucleophilic_substitution",
       "alkyl_halide -> substituted", _ALKYL_HALIDE, "Nu- (OH-, CN-, N3-, RO-, ...)",
       "SN1 or SN2, depending on the substrate",
       ("nucleophile in a polar solvent; heat for SN1",),
       "primary/secondary halides favour SN2; tertiary favour SN1"),
    _t("alkyl-halide-elimination", "Elimination",
       ReactionClassFamily.ELIMINATION, "elimination",
       "alkyl_halide -> alkene", _ALKYL_HALIDE, "strong base (KOH, NaOEt, t-BuO-)",
       "E1 or E2 beta-elimination",
       ("strong base, heat; bulky base favours the less substituted alkene",),
       "competes with substitution; Zaitsev product is typical"),
    # -- aromatic electrophilic substitution (Stage 3.3, §7) -----------------
    _t("aromatic-nitration", "Aromatic Nitration",
       ReactionClassFamily.SUBSTITUTION, "electrophilic_aromatic_substitution",
       "aromatic -> nitroaromatic", _AROMATIC, "HNO3 / H2SO4",
       "electrophilic aromatic substitution (nitronium ion)",
       ("HNO3 / H2SO4, 0-60 C",),
       "deactivated rings nitrate more slowly; multiple nitration possible"),
    _t("aromatic-halogenation", "Aromatic Halogenation",
       ReactionClassFamily.SUBSTITUTION, "electrophilic_aromatic_substitution",
       "aromatic -> aryl_halide", _AROMATIC, "X2 / FeX3 (or AlX3)",
       "electrophilic aromatic substitution",
       ("X2 (Cl2 or Br2) with a Lewis acid catalyst",),
       "requires a Lewis acid catalyst for benzene; very activated rings halogenate without it"),
    _t("friedel-crafts-alkylation", "Friedel-Crafts Alkylation",
       ReactionClassFamily.SUBSTITUTION, "electrophilic_aromatic_substitution",
       "aromatic -> alkylarene", _AROMATIC, "RCl / AlCl3",
       "electrophilic aromatic substitution (carbocation electrophile)",
       ("RCl / AlCl3, room temperature or mild heat",),
       "polyalkylation and carbocation rearrangements are common; strongly deactivated rings do not react"),
    _t("friedel-crafts-acylation", "Friedel-Crafts Acylation",
       ReactionClassFamily.SUBSTITUTION, "electrophilic_aromatic_substitution",
       "aromatic -> aryl_ketone", _AROMATIC, "RCOCl / AlCl3",
       "electrophilic aromatic substitution (acylium ion)",
       ("RCOCl / AlCl3, room temperature",),
       "no rearrangement (acylium is resonance-stabilised); requires >1 equiv AlCl3"),
    _t("aromatic-sulfonation", "Aromatic Sulfonation",
       ReactionClassFamily.SUBSTITUTION, "electrophilic_aromatic_substitution",
       "aromatic -> sulfonic_acid", _AROMATIC, "SO3 / H2SO4 (fuming)",
       "electrophilic aromatic substitution; reversible",
       ("SO3 / concentrated H2SO4, heat",),
       "reversible; the sulfonic acid can be removed by dilute steam"),
    # -- alkene addition -----------------------------------------------------
    _t("alkene-syn-dihydroxylation", "Syn Dihydroxylation",
       ReactionClassFamily.OXIDATION, "dihydroxylation",
       "alkene -> vicinal_diol", _ALKENE, "OsO4 (catalytic) + NMO, or KMnO4 (cold, dilute)",
       "syn addition of two hydroxyl groups",
       ("OsO4 / NMO, or cold dilute KMnO4, then workup",),
       "syn stereospecific; OsO4 is toxic and expensive",
       selectivity=SelectivitySpec(stereoselectivity="syn")),
    # -- alkynes -------------------------------------------------------------
    _t("alkyne-dissolving-metal-reduction", "Dissolving-Metal Reduction",
       ReactionClassFamily.REDUCTION, "trans_hydrogenation",
       "alkyne -> trans_alkene", _ALKYNE, "Na or Li / NH3 (liquid)",
       "single-electron reduction to the trans (E) alkene",
       ("alkali metal in liquid ammonia, low temperature",),
       "gives the trans (E) alkene; the opposite stereochemistry of Lindlar reduction",
       selectivity=SelectivitySpec(stereoselectivity="trans (E)")),
    _t("alkyne-oxidative-cleavage", "Alkyne Oxidative Cleavage",
       ReactionClassFamily.OXIDATION, "oxidative_cleavage",
       "alkyne -> carboxylic_acid", _ALKYNE, "O3 then H2O, or KMnO4 (hot)",
       "oxidative cleavage of the triple bond",
       ("ozonolysis then oxidative workup; or hot KMnO4",),
       "terminal alkynes give the carboxylic acid and CO2"),
    _t("terminal-alkyne-alkylation", "Terminal Alkyne Alkylation",
       ReactionClassFamily.SUBSTITUTION, "alkyne_alkylation",
       "terminal_alkyne -> internal_alkyne", _TERMINAL_ALKYNE, "1) NaNH2 2) R-X",
       "deprotonation of the acidic terminal alkyne, then SN2 alkylation",
       ("NaNH2 (or BuLi), then primary alkyl halide",),
       "best with primary alkyl halides; elimination competes with secondary/tertiary"),
    # -- alkyl halides: mechanism-specific (Stage 3.3, §6) -------------------
    _t("sn2-substitution", "SN2 Substitution",
       ReactionClassFamily.SUBSTITUTION, "nucleophilic_substitution",
       "primary_or_secondary_halide -> substituted", _PRIMARY_SECONDARY_HALIDE,
       "Nu- (OH-, CN-, N3-, RO-, ...)",
       "bimolecular nucleophilic substitution (SN2)",
       ("nucleophile in a polar aprotic solvent",),
       "primary/secondary substrates favour SN2; tertiary substrates do not undergo SN2",
       selectivity=SelectivitySpec(stereoselectivity="inversion of configuration")),
    _t("sn1-substitution", "SN1 Substitution",
       ReactionClassFamily.SUBSTITUTION, "nucleophilic_substitution",
       "tertiary_halide -> substituted", _TERTIARY_HALIDE,
       "weak nucleophile / protic solvent (H2O, ROH)",
       "unimolecular nucleophilic substitution (SN1) via a carbocation",
       ("polar protic solvent, heat",),
       "requires a carbocation-stabilising substrate; racemisation and rearrangements occur"),
    _t("e2-elimination", "E2 Elimination",
       ReactionClassFamily.ELIMINATION, "elimination",
       "alkyl_halide -> alkene", _PRIMARY_SECONDARY_HALIDE,
       "strong base (KOH, NaOEt, t-BuO-)",
       "bimolecular beta-elimination (E2)",
       ("strong base, heat",),
       "requires an anti-periplanar beta-H; competes with SN2"),
    _t("e1-elimination", "E1 Elimination",
       ReactionClassFamily.ELIMINATION, "elimination",
       "tertiary_halide -> alkene", _TERTIARY_HALIDE,
       "weak base / protic solvent, heat",
       "unimolecular elimination (E1) via a carbocation",
       ("polar protic solvent, heat",),
       "competes with SN1; Zaitsev (more substituted) alkene is typical"),
    # -- alcohols ------------------------------------------------------------
    _t("alcohol-dehydration", "Alcohol Dehydration",
       ReactionClassFamily.ELIMINATION, "dehydration",
       "alcohol -> alkene", _ALCOHOL, "H2SO4 or H3PO4, heat",
       "acid-catalysed elimination (E1 for secondary/tertiary)",
       ("concentrated acid, heat",),
       "secondary/tertiary alcohols dehydrate readily; primary alcohols need harsher conditions",
       selectivity=SelectivitySpec(regioselectivity="Zaitsev (more substituted alkene)")),
    _t("primary-alcohol-to-acid", "Primary Alcohol Oxidation to Carboxylic Acid",
       ReactionClassFamily.OXIDATION, "alcohol_oxidation",
       "primary_alcohol -> carboxylic_acid", _PRIMARY_ALCOHOL,
       "KMnO4, Jones (CrO3/H2SO4)",
       "two-step oxidation through the aldehyde",
       ("strong oxidant, aqueous conditions",),
       "over-oxidation to the carboxylic acid; the aldehyde is an intermediate"),
    _t("alcohol-to-halide", "Alcohol to Alkyl Halide",
       ReactionClassFamily.SUBSTITUTION, "alcohol_halogenation",
       "alcohol -> alkyl_halide", _ALCOHOL, "SOCl2, PBr3, or HX",
       "substitution of the hydroxyl by a halide",
       ("SOCl2 or PBr3, room temperature; or HX",),
       "tertiary alcohols convert fastest via SN1; rearrangement possible with HX"),
    # -- carbonyls -----------------------------------------------------------
    _t("aldehyde-reduction", "Aldehyde Reduction",
       ReactionClassFamily.REDUCTION, "aldehyde_reduction",
       "aldehyde -> primary_alcohol", _ALDEHYDE, "NaBH4 or LiAlH4",
       "hydride addition to the carbonyl",
       ("NaBH4 in alcohol, room temperature",),
       "NaBH4 is chemoselective for aldehydes/ketones"),
    _t("aldehyde-oxidation", "Aldehyde Oxidation",
       ReactionClassFamily.OXIDATION, "aldehyde_oxidation",
       "aldehyde -> carboxylic_acid", _ALDEHYDE, "Tollens (Ag2O), Jones, or KMnO4",
       "oxidation of the aldehyde carbon",
       ("mild aqueous oxidant",),
       "aldehydes oxidise more readily than ketones; Tollens is selective for aldehydes"),
    _t("ketone-grignard-addition", "Ketone Grignard Addition",
       ReactionClassFamily.ADDITION, "nucleophilic_addition",
       "ketone -> tertiary_alcohol", _KETONE, "RMgX (Grignard) or RLi",
       "nucleophilic addition of a carbanion",
       ("anhydrous ether, low temperature",),
       "adds an alkyl/aryl group; water must be excluded"),
    _t("carbonyl-olefination", "Carbonyl Olefination (Wittig)",
       ReactionClassFamily.CONDENSATION, "olefination",
       "aldehyde_or_ketone -> alkene", [_ALDEHYDE, _KETONE],
       "Ph3P=CHR (Wittig ylide)",
       "phosphorus ylide addition then oxaphosphetane collapse",
       ("ylide in an inert solvent",),
       "installs a C=C at the carbonyl position; stereoselectivity depends on the ylide"),
    _t("cyanohydrin-formation", "Cyanohydrin Formation",
       ReactionClassFamily.ADDITION, "cyanohydrin",
       "aldehyde_or_ketone -> cyanohydrin", [_ALDEHYDE, _KETONE],
       "HCN or NaCN / acid",
       "nucleophilic addition of cyanide to the carbonyl",
       ("HCN or NaCN under mildly acidic conditions",),
       "HCN is highly toxic; equilibrium favours cyanohydrin for ketones"),
    # -- amines --------------------------------------------------------------
    _t("amine-acylation", "Amine Acylation",
       ReactionClassFamily.SUBSTITUTION, "acylation",
       "primary_or_secondary_amine -> amide", _AMINE,
       "acyl chloride or anhydride",
       "nucleophilic acyl substitution",
       ("RCOCl or (RCO)2O, with a base",),
       "primary amines give amides; tertiary amines do not acylate"),
    _t("amine-alkylation", "Amine Alkylation",
       ReactionClassFamily.SUBSTITUTION, "amine_alkylation",
       "primary_or_secondary_amine -> substituted_amine", _AMINE,
       "alkyl halide (or reductive amination)",
       "nucleophilic substitution at the amine",
       ("alkyl halide, base",),
       "over-alkylation to quaternary ammonium salts is common"),
    # -- nitriles / esters ---------------------------------------------------
    _t("nitrile-reduction", "Nitrile Reduction",
       ReactionClassFamily.REDUCTION, "nitrile_reduction",
       "nitrile -> primary_amine", _NITRILE, "LiAlH4, or H2 / catalyst",
       "reduction of the C-N triple bond",
       ("LiAlH4 in ether",),
       "LiAlH4 reduces nitriles to primary amines"),
    _t("nitrile-hydrolysis", "Nitrile Hydrolysis",
       ReactionClassFamily.SUBSTITUTION, "hydrolysis",
       "nitrile -> carboxylic_acid", _NITRILE, "H2O / H+ or OH-, heat",
       "hydrolysis through the amide to the acid",
       ("aqueous acid or base, heat",),
       "proceeds via the amide; harsh conditions may be needed"),
    _t("ester-hydrolysis", "Ester Hydrolysis",
       ReactionClassFamily.SUBSTITUTION, "hydrolysis",
       "ester -> carboxylic_acid", _ESTER, "H2O / H+ or OH-",
       "nucleophilic acyl substitution (saponification with OH-)",
       ("aqueous acid or base, heat",),
       "base-mediated hydrolysis (saponification) is irreversible"),
    # -- epoxides ------------------------------------------------------------
    _t("epoxide-ring-opening", "Epoxide Ring-Opening",
       ReactionClassFamily.SUBSTITUTION, "ring_opening",
       "epoxide -> diol_or_substituted", _EPOXIDE,
       "H2O / H+ (or a nucleophile)",
       "nucleophilic attack at the less-substituted epoxide carbon",
       ("aqueous acid, or nucleophile in a protic solvent",),
       "acidic conditions open at the more substituted carbon; basic at the less"),
    # -- rearrangements ------------------------------------------------------
    _t("pinacol-rearrangement", "Pinacol Rearrangement",
       ReactionClassFamily.REARRANGEMENT, "pinacol_rearrangement",
       "vicinal_diol -> ketone_or_aldehyde", "[C]([OX2H])[C]([OX2H])",
       "H2SO4 (or another strong acid)",
       "1,2-alkyl/aryl shift after protonation and loss of water",
       ("strong acid, heat",),
       "the group that migrates is the one that best stabilises the incipient carbocation"),
]


class ReactionMatcher:
    """Match curated reaction templates against a molecule's structure."""

    def __init__(self, templates: list[ReactionTemplate] | None = None) -> None:
        self._templates = (list(templates)
                           if templates is not None
                           else list(SEED_REACTION_TEMPLATES))

    def match(self, molecule) -> list[ReactionMatch]:
        """The reactions whose substrate pattern matches the molecule."""
        out = []
        for template in self._templates:
            matched_atoms: list[list[int]] = []
            for pattern in template.substrate_patterns:
                matched_atoms.extend(list(m) for m in matches_smarts(molecule, pattern))
            if matched_atoms:
                out.append(ReactionMatch(
                    template=template, matched=True,
                    matched_atoms=matched_atoms))
        return out


def list_templates() -> list[ReactionTemplate]:
    """All reaction templates, in seed order (§19)."""
    return list(SEED_REACTION_TEMPLATES)


def get_template(template_id: str) -> ReactionTemplate | None:
    """One template by its internal id, or None."""
    for template in SEED_REACTION_TEMPLATES:
        if template.template_id == template_id:
            return template
    return None


__all__ = [
    "ReactionMatcher",
    "SEED_REACTION_TEMPLATES",
    "get_template",
    "list_templates",
]

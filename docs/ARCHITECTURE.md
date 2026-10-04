# Architecture — Phase 1 (Scenario A)

## 1. Scope

Phase 1 implements one scenario end to end:

```
SMILES -> Molecule -> canonical SMILES / InChIKey / descriptors
       -> Morgan fingerprint -> Tanimoto        (screening only)
       -> Murcko scaffold                        (shared ring skeleton)
       -> MCS + atom map                         (atom-resolved common core)
       -> fragment decomposition                 (ring_linker / brics / recap)
       -> attachment point analysis              (the layer that discriminates)
       -> explainable match report
```

Scenario B (literature), Scenario C (PDF to evidence) and retrosynthesis are
**not** implemented. Only the provider *boundary* exists, so that the adapter
shape is decided and testable before any of it is written.

## 2. Layer rules, enforced not documented

| Rule | Enforcement |
|---|---|
| `chem/` imports no network, no LLM, no providers | AST check in `test_layering.py` |
| `core/` imports no RDKit and nothing above it | AST check |
| `services/` does not import RDKit directly | AST check |
| `MolFromSmiles` / `MolFromSmarts` only inside `chem/` | source scan |
| A screening result cannot pose as a conclusion | `Literal[True]` on the field |

## 3. The five layers, and what each one can and cannot see

| Layer | Module | Sees a position difference? | Can it be a conclusion? |
|---|---|---|---|
| 1 fingerprint | `chem/fingerprint.py` | no | **no** — typed out of it |
| 2 common core | `chem/align.py` | no | yes, at atom level |
| 3 scaffold | `chem/scaffold.py` | no | yes, with caveat |
| 3b fragments | `chem/fragment.py` | depends on method | yes |
| 4 attachment | `chem/attachment.py` | **yes** | yes |

Measured, on rdkit 2026.03.6, for ortho- vs para-xylene:

```
Tanimoto (ECFP4)     0.385        <- high enough to be misleading
Murcko scaffold      c1ccccc1    <- identical
MCS                  7 atoms / 7 bonds, identical SMARTS
ring fragment        c1ccccc1    <- identical
attachment descriptor *c1ccccc1*  vs  *c1ccc(*)cc1    <- the difference
```

Every layer above 4 is blind to this. That is not a shortcoming of the
implementation; it is a property of those representations, and the report says
so in its limitations section every time.

## 4. Attachment points

The descriptor is a **rooted canonical attachment descriptor (RCAD)**: the
fragment molecule is written as canonical SMILES with the dummy attachment atom
as the root. Canonical, so it is comparable across molecules; rooted, so it
encodes *where* the fragment connects.

| Fragment | RCAD | Attachment count |
|---|---|---|
| benzene ring in o-xylene | `*c1ccccc1*` | 2, ortho |
| benzene ring in m-xylene | `*c1cccc(*)c1` | 2, meta |
| benzene ring in p-xylene | `*c1ccc(*)cc1` | 2, para |
| alkyne in phenylacetylene | `*C#C` | 1, terminal |
| alkyne in diphenylacetylene | `*C#C*` | 2, internal |

Around it, `LocalEnvironment` carries six progressively tighter descriptors
(anchor properties, radius-1/2/3 rooted SMILES, the full distance-matrix
signature, Crippen contributions, and named functional groups within radius 2).
The distance matrix is the backstop: it is a complete topological signature and
cannot be ambiguous the way a sliced aromatic ring can be.

`RoleSignature` is rule-derived and every field carries its `rule_id`. It is
rendered as a heuristic, never as an experimental fact.

`embedded_match` is deliberately conservative. `embedded_full` is claimed only
when the parent-level MCS atom map demonstrably covers the fragment's anchor
atoms on *both* sides. Without that map the engine says `role_context`.

## 5. RDKit API constraints this code is written against

Established by probing rdkit 2026.03.6 and pinned in
`tests/spikes/test_rdkit_api.py`:

1. Substructure search is `target.GetSubstructMatch(query)`. Reversed, every
   "smaller pattern in larger target" search silently returns nothing and looks
   exactly like a broken build.
2. `MCSResult` no longer exposes `smartsAtomMapNumber`. Both sides are therefore
   enumerated from the MCS SMARTS and paired by score; no undocumented ordering
   assumption is relied on.
3. `BondCompare.CompareOrder` treats an aromatic bond and a single bond as
   equivalent, so benzene matches cyclohexane. Both profiles use
   `CompareOrderExact`; `strict` adds `matchValences` and `matchChiralTag`.
4. `MolFragmentToSmiles` does not add boundary dummies here; it truncates the
   SMILES mid-string. Fragment molecules are therefore built explicitly.
5. `MolFragmentToSmilesWithAtomLabels` is absent; environments are submol +
   rooted SMILES.
6. `Chem.MolFromSmarts` is the only pattern entry point; `Chem.ExplicitBitVect`
   lives in `DataStructs`.
7. `BRICS.BreakBRICSBonds` returns a Mol, not bond indices;
   `BRICS.FindBRICSBonds` returns a generator of `((a, b), (la, lb))`.
8. `RECAP.RecapDecompose` returns a `RecapHierarchyNode`; use `.GetLeaves()`.
   An empty dict means "no cleavable bond", which is normal for a xylene.
9. `BaseFeatures.fdef` yields pharmacophore families, not chemical functional
   groups. Role rules use the `FunctionalGroups` hierarchy instead.
10. `rdMolDescriptors.CrippenContribs` is spelled `_CalcCrippenContribs`.
11. Degenerate MCS returns `numAtoms == 0`, `smartsString == ""`.

One design error this project made and corrected: the InChIKey skeleton block
was originally used to flag positional isomers. It does not — it encodes
*connectivity*, so ortho-, meta- and para-xylene have three different blocks
and only stereoisomers share one. `SimilarityScreen` now reports
`constitutional_isomer_hint` and `stereo_isomer_hint` separately, both derived
from formula + heavy-atom count + InChIKey.

## 6. Standardization profiles

Chosen by the caller, never guessed:

| Profile | Operations |
|---|---|
| `identity` | none |
| `parent` (default) | `Cleanup` + `FragmentParent` |
| `canonical_tautomer` | + `TautomerEnumerator().Canonicalize()` |

`Uncharger` is off by default: neutralizing rewrites protonation state, which
changes chemistry silently. Every operation taken is recorded in
`Molecule.normalization_actions` and printed in the report, because a similarity
number is not interpretable without knowing what was standardized away.

## 7. Known limitations

- Ring sizes come from SSSR, so a fused bicyclic reports six-membered rings
  rather than the perimeter ring. `Descriptors.num_rings` is the count to trust.
- `Fragment.formula` is the molecular formula of the fragment treated as a
  molecule, so a lone methyl reads `CH4`.
- BRICS does not cut alkynes in this RDKit build, so BRICS alone cannot
  separate the phenylacetylene / diphenylacetylene / diyne family. The
  `ring_linker` decomposition does.
- Ring path distances use the full graph shortest path, which can shortcut
  through a fused ring. Correct for the single-ring cases this phase targets.
- `role_confidence` is always `rule_derived`; no inferred roles are produced yet.

# Chemical Entity Registry

Phase 3.8.1. The stable, citable, traceable internal identifier layer for
chemical entities — the first link of the real research knowledge chain
`Literature → Chemical Entity → Reaction → Evidence`.

## What this is

A paper may name one compound as `phenylacetylene`, `ethynylbenzene`, a
SMILES string, `PubChem CID 733`, or an InChIKey. Those are strings until
something says what they refer to. This layer is the *thing they refer to*:

```text
CHEM-00000001  =  one chemical identity that the identity layer established
```

> **`CHEM-00000001` is CREA's own stable internal identifier. It is not the
> chemical structure itself, and it is not a PubChem CID.**

The structure lives on the entity (SMILES / InChI / InChIKey). The PubChem
CID lives on the entity as an *external identifier* pointing outwards. The
id is the label CREA hands around — in reports, in future reactions, in
future citations — so that one established identity has exactly one name in
this system, whatever the world calls it.

## ChemicalEntity

```text
ChemicalEntity
├── entity_id               CHEM-00000001 (internal, stable)
├── identity
│   ├── identity_key        crea-identity-key-v1:InChI=1S/...
│   ├── identity_key_version
│   ├── identity_basis      exact_structure | provider_identity | ...
│   └── facets              connectivity / stereo / isotope / tautomer
├── structure
│   ├── canonical_smiles
│   ├── inchi
│   ├── inchikey
│   └── molecular_formula / molecular_weight
├── names                   labels only; never identity
├── external_identifiers    namespace / identifier / source (e.g. pubchem / 733)
├── provenance              identity_source, source_id, source_url, retrieved_at
└── created_at
```

Defined in `src/crea/core/models/entity.py`. The `identity_basis` and
`facets` fields are Phase 3.6's own (`crea.core.models.identity`), reused
rather than redefined.

## ChemicalEntity ID

Format: `CHEM-` + 8 digits, starting at `CHEM-00000001`, allocated in
registration order.

- fixed prefix and width;
- allocation order is the whole story of the number — sorting a list never
  renumbers it;
- restarting a process and re-registering the same identities in the same
  order produces the same ids;
- no random UUIDs in user-visible ids;
- a PubChem CID is **never** an entity id.

One `ChemicalEntity` per current identity key: registering an identity that
is already known returns the existing entity (and merges any new names or
external identifiers into it) instead of minting a second id.

## Identity Key

```text
IDENTITY_KEY_VERSION = "crea-identity-key-v1"
identity_key = crea-identity-key-v1:<standard InChI>
```

The key is derived from the **standard InChI** of the established structure —
the same string Phase 3.6's identity policy compares layer by layer
(`/c` connectivity, `/h` mobile-H tautomer class, `/t /m /s /b` stereo, `/i`
isotopes, `/q /p` charge and protons). So the key reproduces that policy's
distinctions exactly:

- same standard InChI → one identity → one entity (InChI's mobile-H
  treatment merges the tautomers the policy merges);
- different protonation, stereochemistry or isotope → different InChI →
  different entity (the policy separates them).

A key is only minted from an InChI. A name, a guess or an empty string is
not an identity substrate and raises instead of producing a key.

The version stamp exists so a later identity-policy change can say which
keys must be re-derived: `CHEM-00000001` may need re-resolution under a new
`IDENTITY_KEY_VERSION`, and the stamp says which derivation produced it.

## Name / synonym

Names are labels attached to an entity:

```text
CHEM-00000001
  names: phenylacetylene, ethynylbenzene
```

**A name can never create an entity.** If the identity layer proves two
names refer to one structure, they are one entity with two names. The
failure this rule prevents:

```text
phenylacetylene  → CHEM-00000001   ← wrong if both names mean one structure
ethynylbenzene   → CHEM-00000002
```

## External Identifier

```text
ExternalIdentifier
├── namespace    "pubchem"
├── identifier   "733"
└── source       who told us the pairing
```

An external identifier is a pointer into someone else's scheme. The only
networked source wired in this phase is the existing PubChem provider: a
`StructureInput(kind=PUBCHEM_CID)` or a PubChem-sourced candidate records
`namespace=pubchem, identifier=<CID>` on the entity. No ChEMBL/ZINC access
was added — those namespaces are representable, not contacted.

## Provenance

Every entity says where its identity came from, in the same vocabulary as
`Evidence` provenance:

```text
identity_source: pubchem        (or user_provided, ...)
source_id:       733
source_url:      https://pubchem.ncbi.nlm.nih.gov/compound/733
retrieved_at:    ...
provenance:      Provenance(tool="chemical-entity-registry", source=..., ...)
```

There is deliberately no second provenance system.

## Ambiguous / Unresolved

The registry never merges an uncertain identity into an entity:

| identity resolution | registry behaviour |
|---|---|
| `RESOLVED` | registers `ChemicalEntity` |
| `AMBIGUOUS` | refuses; the candidates stay `ChemicalIdentityCandidate`s |
| `UNAVAILABLE` | refuses; nothing is guessed from the name |
| `NOT_FOUND` / `INVALID` / `INPUT` | refuses |
| `RESOLVED` without an InChI | refuses (`NO_IDENTITY_KEY`) |

A `ChemicalEntityCandidate` concept is intentionally *not* introduced —
`ChemicalIdentityCandidate` (Phase 3.6) already is the candidate
representation, and a second one would be a second identity system.

## Future: ChemicalMention

The boundary for literature is fixed now, before any PDF parsing exists:

```text
ChemicalMention
├── mention_id
├── raw_text / normalized_text
├── entity_id            only when resolution_status = resolved
├── resolution_status    resolved | ambiguous | unresolved
├── source
├── locator
└── evidence_refs
```

The model refuses a mention that carries an entity id without a resolved
status. This phase defines no mention extractor and does no NLP.

## The knowledge chain to come

```text
Paper
  │
  ├── ChemicalMention
  │       │
  │       ↓
  │   ChemicalEntity ──→ Structure
  │
  └── Reaction
          │
          ├── reactants → ChemicalEntity
          ├── reagents  → ChemicalEntity
          └── products  → ChemicalEntity
```

and ultimately:

```text
                 Chemical Entity
                  /      |      \
                 /       |       \
          Literature   Reaction   Structure
              |           |          |
              └───────────┴──────────┘
                       Evidence
```

## Storage

`ChemicalEntityRegistry` talks to storage through a
`ChemicalEntityRepository` protocol (`get / add / find_by_identity_key /
find_by_external_id / all`). The only implementation now is
`InMemoryChemicalEntityRepository` — no database in this phase. A future
`SQLiteChemicalEntityRepository` or `PostgreSQLChemicalEntityRepository`
implements the same protocol; the registry does not change.

## Layering

- `core/models/entity.py` imports core only: no agent, no llm, no sources,
  no providers — and therefore no PubChem, no network, no RDKit.
- `services/entity_registry.py` consumes `ChemicalIdentityResolution`
  objects the identity service produced. No RDKit import anywhere in it.
- RDKit remains confined to `crea.chem`; the entity layer never parses a
  structure.
- Enforced by `tests/architecture/test_layering.py`
  (`test_the_entity_layer_stays_inside_its_boundaries`).

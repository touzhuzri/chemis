# Phase 3.2 — Reaction & Experimental Intelligence

Experimental text becomes structured, verifiable conditions.

```
PDF ─► Evidence ─► model proposes ─► we check ─► ReactionRecord
                    (may say anything)
```

The model reads prose. It is not a source of facts. Everything it proposes is
checked against the Evidence it was handed before any of it reaches a record,
and the record is only as good as those checks.

## 1. What was added

```
src/crea/core/models/reaction.py       ReactionRecord and friends
src/crea/core/policy/reaction.py       the checks
src/crea/services/reaction_extraction.py  propose -> validate
src/crea/agent/reaction_prompt.py      the extraction instructions
                                        + the eighth agent tool,
                                          get_reaction_details
src/crea/core/models/research.py       ResearchReport.reaction_details
src/crea/report/research.py            the Chinese rendering
```

Nothing in `chem/`, the providers, the PDF parser, the agent loop or the LLM
provider abstraction changed shape.

## 2. ReactionRecord

```
ReactionRecord
├── reaction_id                 content-derived, stable
├── document_id / paper_id
├── reaction_description        + description_evidence_refs
├── conditions
│   ├── reactants / reagents / catalysts / ligands / solvents   [Component]
│   ├── unassigned              named, role not established
│   ├── temperature / time / pressure / concentration           [Quantity]
│   ├── yield_value + yield_type
│   ├── atmosphere / workup / purification
├── evidence_refs               the passages this came from
├── raw_experimental_text      the text, for re-reading if we got it wrong
├── conflicts                   passages that disagree
├── rejected                    proposals that did not survive
└── policy_version             "reaction-v1"
```

Every `Quantity` and every `Component` carries its **own** `evidence_refs`, so
`temperature` and `yield` can point at different passages. A single
record-wide source would blur exactly the distinction that matters.

A `Quantity` is unconverted: `raw_value`, `raw_unit`, `value`, `unit`. Nothing
here turns 80 °C into 353 K. A conversion is an interpretation, and this layer
reports what the paper wrote.

**No confidence scores.** `FieldStatus` is `EXPLICIT / INFERRED / NOT_FOUND /
AMBIGUOUS` — four statements about provenance, not four shades of certainty.
The project has already declined to invent a number nobody defined, and it does
not do so here either.

## 3. `get_reaction_details`

```
input    document_id, query | evidence_ids
   ↓
  find the passages (or take the ones named)
   ↓
  model proposes an ExtractionProposal
   ↓
  validate          ← the part that matters
   ↓
  ReactionRecord + ReactionExtractionResult
```

`ReactionExtractionResult` is deliberately not `ReactionRecord`: it carries
`outcome`, `conflicts`, `rejected` and `notes`, so a report can say what failed
rather than quietly showing less.

The extractor takes an `LLMProvider`, not a vendor class. `extract()` also
accepts a pre-built proposal, which is how the validation is tested without a
model and how a caller with its own extractor reuses the checks unchanged.

## 4. What stops an invention

| Failure | What happens |
|---|---|
| a cited evidence id that was never supplied | the field is dropped, the reason recorded |
| a number not in the quoted text | the field is dropped — and the match is on **number boundaries**, so 80 cannot be read as 8 |
| a yield outside 0–100 | refused, not repaired to a plausible digit |
| a pressure the paper never mentions | `NOT_FOUND`; the report says 未说明 |
| "82% yield" read as an isolated yield | requires the word "isolated" in the quote, or it stays `UNKNOWN` |
| two passages saying 80 and 90 | a `Conflict`; both kept, neither chosen |
| a component with no role | kept in `unassigned` rather than dropped or guessed |
| a unit we do not recognise | kept verbatim, not converted |

`tests/unit/test_reaction_extraction.py` has a test for each row. The ones worth
reading first are `test_a_field_citing_an_unknown_evidence_is_rejected` and
`test_a_subtle_digit_change_is_caught`.

## 5. What this is not

* No atom mapping, no reaction SMARTS, no product prediction.
* No name→SMILES. A name stays a name, because there is no verified resolver
  here and attaching a structure would assert an identification nobody checked.
* No ranking of conditions, no "best catalyst", no multi-step synthesis graph.
* No plausibility judgement. CuI at 500 mol% is recorded as written; deciding it
  is odd is a later phase.

## 6. Verified against a real model

`tests/live/test_reaction_live.py` runs the extraction prompt against the real
provider on a synthetic procedure. It checks three things: that a real
proposal survives validation, that no pressure or atmosphere appears when the
paper mentions none, and that every field cites the evidence it was given.

It passes. Two bugs it found in the process, both real:

* the extraction prompt taught the model `EXPLICIT` while the model accepted
  only `explicit`, so every real extraction failed at the first field;
* the model puts the substance name in `name` and the amount in `value`, and the
  component validator was reading the name out of `value` — which turned
  "Phenylacetylene, 1.0 mmol" into a substance called "1.0".

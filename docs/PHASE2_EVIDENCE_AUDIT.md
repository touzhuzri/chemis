# Evidence model audit

Audit of the repository as it stood before the Evidence chain was built
(552 passed / 31 skipped). Every claim below was read out of the code, not
recalled from a design document.

## 1. What exists

| Thing | Status | Where |
|---|---|---|
| `EvidenceStrength` | **exists**, 4 values | `core/models/literature.py` |
| `AbstractSource` | **exists**, 5 values | `core/models/literature.py` |
| `AbstractRecord` | **exists**, with `is_reconstructed` and `evidence_strength` | `core/models/literature.py` |
| `Provenance` | **exists**, but tool-oriented | `core/provenance.py` |
| `ids.content_hash` | **exists**, reusable | `core/ids.py` |
| `Paper` provenance | **exists**: `sources`, `source_ids`, `retrieved_at`, `raw_hashes` | `core/models/literature.py` |
| `AccessAssessment` | **exists** | `core/models/literature.py` |
| `Evidence` | **DOES NOT EXIST** | — |
| `Claim` | **DOES NOT EXIST** | — |
| `Locator` | **DOES NOT EXIST** | — |
| `EvidenceRegistry` / claim validation | **DOES NOT EXIST** | — |

`Evidence`, `Claim` and `Locator` appear in `docs/PHASE2_DESIGN.md` §6 as a
*draft*. They were never written as code. Anything downstream that assumed
otherwise would have found nothing.

## 2. Answering the eight questions

**1. Does the current Evidence model have fields?**
No model exists. `EvidenceStrength` is the only evidence-adjacent type, and it
is an enum, not a record.

**2. Does the current Claim model have fields?**
No `Claim` type exists. The design draft specifies `claim_id`, `text`,
`claim_type`, `evidence_refs`, `confidence`, `provenance`. None of it is code.

**3. What is the current `Claim.evidence_refs` type constraint?**
None, because there is no `Claim`. "Claims must have evidence" currently lives
in prose in a design document and is enforced by nothing. It will be enforced by
`Field(min_length=1)`, which is a type-level guarantee rather than a comment.

**4. How would Evidence point at a Paper?**
It cannot yet. `Paper.paper_id` is the normalized InChIKey-style key, and
`Paper.identifiers.doi` is the DOI. Evidence will carry `paper_id` plus the
paper's own DOI so the link survives a `Paper` being rebuilt.

**5. Do `source` / `source_id` / `retrieved_at` / `raw_quote` / `locator` exist?**
On `Paper`: `sources` (list), `source_ids` (dict), `retrieved_at`, `raw_hashes`
— all present and required. On `Evidence`: none, because it does not exist.
`raw_quote` and `locator` appear nowhere in the codebase.

**6. Which fields are already established facts?**
`Paper.sources` / `source_ids` / `retrieved_at` / `raw_hashes` are populated by
every parser and asserted by tests. `AbstractRecord.source`,
`is_reconstructed`, `retrieved_at`, `raw_excerpt` are populated and asserted.
`AccessAssessment.oa_status` and `oa_status_raw` are structured fields, not
notes — fixed in Phase 2.3.

**7. Which fields are still string conventions?**
- `Paper.upstream.notes` — human-readable. It **must not** be parsed. The
  reconciliation layer was migrated off it in Phase 2.3 and a test now fails if
  the wording changes.
- `AccessAssessment.notes` — human-readable, same rule.
- `Paper.merge_conflicts` — machine-readable, but free text with a `"field: …"`
  prefix convention. Carrying a conflict is fine; parsing one is not.
- `FieldAlternative.value` — a rendered string, deliberately for display.

**8. Which fields will future PDF / HTML / user documents use?**
`raw_quote`, `locator` (page / section / character range), `source_url`,
`strength`. None exist. They are what this phase adds, and they are added so a
future document producer fills them in rather than redesigning the model.

**9. Can one Evidence point at Crossref metadata, OpenAlex metadata and a
user PDF at once?**
There is no Evidence, so the question is moot — and the answer must be *no*.
A single `Evidence` carries exactly one `source` and one `source_id`. The
relatedness between several sources is the Claim's `evidence_refs`, not a field
on one Evidence. A record that says "Crossref and OpenAlex and my PDF all say
this" is a Claim with three references, not an Evidence with three sources.

**10. Is there an Evidence ID stability problem?**
Not yet, because there is no ID. The rule adopted here:

```
evidence_id = content_hash(source | source_id | evidence_type |
                           locator.canonical() | raw_quote)
```

`retrieved_at` is **excluded**. Re-fetching the same work and finding the same
sentence is the same piece of evidence, not a new one; minting a fresh ID on
every poll would make `Claim.evidence_refs` rot the first time a citation was
resolved. The reasoning is recorded in the code next to the rule, and
`test_evidence_id_is_stable_across_re_retrieval` pins it.

## 3. Provenance: one system, not two

`core/provenance.py::Provenance` is **tool-oriented**: it records which code
computed something, when, and how long it took. Evidence provenance needs
*source*-oriented fields — which provider, which record, which payload hash.

Rather than build a second class, `Provenance` gains three optional fields
(`source`, `source_id`, `raw_payload_hash`) and Evidence uses it directly. One
type, one import, one thing to audit.

## 4. Strength: reuse, do not reinvent

`EvidenceStrength` already has the four values this phase needs, and
`AbstractRecord.evidence_strength` already computes the right one for an
abstract — including keeping an OpenAlex reconstruction at
`DISCOVERY_EVIDENCE` rather than promoting it.

No second strength vocabulary is created. The only thing missing is the mapping
from `EvidenceType` to strength for the types that have no producer yet, and
that is policy, so it lives in `core/policy/evidence.py` beside the access
policy.

## 5. Boundaries this phase holds

- `LiteratureService` is untouched by Evidence production. Paper → Evidence is a
  separate service, because a search and a citation are different acts.
- `Paper → Evidence` is a pure function. It mutates nothing, calls nothing, and
  downloads nothing. `test_paper_to_evidence_does_not_mutate_the_paper` pins it.
- No PDF parser, no downloader, no HTML parser, no OCR, no LLM.
- `EvidenceType` carries `FULLTEXT`, `EXPERIMENTAL` and `USER_PROVIDED_DOCUMENT`
  with no producer, so the model can carry them when one arrives.

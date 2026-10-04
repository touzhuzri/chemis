# Phase 2.3 — Cross-provider integration

Three independent sources, gradually converging, without any source's facts,
licences, locations or permissions being wrongly conflated.

```
OpenAlex   ─┐
Crossref   ─┼─→  Paper  →  identity  →  field-level merge  →  provenance
Unpaywall  ─┘                                     │
                                                   ▼
                                        access reconciliation
```

The rule that shapes everything below: **a provider reports, the merge layer
resolves conflicts, the policy layer interprets permission.** No provider knows
another exists; no merge decides a permission; no policy invents a fact.

---

## 1. Identity: DOI, and only DOI

`services.literature_merge.classify_identity` decides whether records are the
same work. It uses one signal.

| Situation | Status | Result |
|---|---|---|
| all DOIs normalise equal | `DOI_MATCH` | merged |
| different DOIs | `DOI_CONFLICT` | not merged, both DOIs recorded |
| some have no DOI | `IDENTITY_UNKNOWN` | not merged |
| none has a DOI | `NO_DOI` | not merged |

**Title similarity is not used here, and never will be.** It already exists in
`services/dedup.py`, with its own screen, four independent gates, a
`threshold_version` and a full audit trail. A second, looser identity algorithm
inside the merge would be a way for two records to become one without anyone
deciding so. `test_identity_never_falls_back_to_the_title` pins this with two
records that have an identical title and identical year and different DOIs.

No DOI, no merge. Conservative by design: two papers wrongly merged is a
fabricated record, two wrongly left apart is a wasted click.

## 2. Field-level resolution

Every field is resolved independently. For each one the result records the
selected value, **every source that offered it**, everything else on the table,
and whether the sources disagreed.

```python
FieldResolution(
    field, state,          # AGREED | CONFLICT | MISSING | UNIONED
    selected,
    selected_from,         # all sources offering the selected value
    alternatives,          # what was passed over, with who offered it
    conflict,              # human-readable, lands on Paper.merge_conflicts
)
```

There is no global "OpenAlex beats Crossref" rule. The policy is per field,
because the sources differ in what they are authoritative *for*: Crossref is
the DOI registration agency and the better record of a title, a journal and a
publisher; neither source is authoritative about the other.

```python
DEFAULT_POLICY = MergePolicy(version="merge-v1", field_priority={
    "title": ("crossref", "openalex", "unpaywall"),
    "journal": ("crossref", "openalex"),
    "access": ("unpaywall", "openalex", "crossref"),
    "publisher": ("crossref",),          # only Crossref reports it
    "upstream_notes": ("crossref", "openalex", "unpaywall"),
    ...
})
```

### Fields that are combined, not chosen between

| Field | Why |
|---|---|
| `abstract` | a publisher deposit and a positional reconstruction are different objects. Concatenating or averaging them destroys the only property that distinguishes them. Both are kept, each tagged. |
| `authors` | order carries meaning, and a shorter list from one source is not evidence the other is wrong. `author_lists` keeps each source's list whole. |
| `upstream.notes` | a note like "the deposit date was not used as the publication date" belongs to the record that made it. Dropping it because another source was preferred would lose the reason the value looks the way it does. |
| `issn`, `identifiers` | union; they describe the same work from different angles. |

### Dates

Each provider's date type survives. OpenAlex publishes a year; Crossref
publishes a typed date chosen by the priority in `sources/crossref.py`
(`published-print` → `published-online` → `issued`). The merge copies the
selected one and records the others as alternatives. It does not re-derive a
date, and it never lets `created` stand in for publication.

## 3. Order independence

`merge_papers` sorts its inputs deterministically and selects by policy rank,
then source name, then paper id. The caller's list order is never consulted.

Three tests pin it: the same records forward, backward and shuffled produce an
identical fingerprint; the alternative that was passed over is the same one;
and the conflict list is the same.

`test_a_custom_policy_can_move_the_preference` additionally shows the policy is
really applied rather than recorded — reversing `title` priority moves the
selected source, and the alternative is still recorded either way.

## 4. Access reconciliation

`services/access_reconciliation.reconcile` takes the independent access claims
of every provider and answers one question: **what evidence do we hold, and is
it enough?**

```
evidence  →  conflicts recorded  →  decision  →  selected assessment + basis
```

The rules are the frozen ones from `PHASE2_PROVIDER_CONTRACT.md` §6:

- `MAY_RETRIEVE` requires an explicitly permissive licence applicable to what
  we would retrieve. A `content-version: "tdm"` grant is a scoped permission,
  not a licence, and cannot produce it — `RESTRICTED_CONTENT_VERSIONS`.
- `UNKNOWN` is a refusal to act without evidence. **It is never upgraded to
  `NOT_PERMITTED`.** `test_unknown_is_never_upgraded_to_not_permitted` walks
  four separate "we could not establish it" cases and demands `UNKNOWN` from
  all of them.
- `NOT_PERMITTED` requires evidence that retrieval is refused.

**Absence of a claim does not overturn a claim.** If OpenAlex reports CC-BY and
Unpaywall reports nothing, the licence stands. If OpenAlex reports a URL and
Crossref reports no URL, the URL is not permission. Knowing less is not knowing
the opposite.

Conflicts are recorded, not resolved away: differing OA status, differing
licences, and differing knowledge of a location each become an `AccessConflict`
with the per-source values attached.

## 5. What this layer deliberately does not do

| Not implemented | Why |
|---|---|
| `LiteratureService` | query → papers. The merge and reconciliation primitives are proven; composing them behind a user-facing question is a separate design with its own questions. |
| ranking / relevance scoring | a product decision, and it would be the first place an LLM gets invited in |
| full-text retrieval | OpenAlex content retrieval is $10 a file with its own rights model; Crossref links are TDM-scoped; Unpaywall locations carry no grant. No provider is permitted to fetch. |
| a database | the merge is pure. Its inputs are `Paper` objects and its output is a `Paper` plus an audit trail. |

## 6. Layering

```
chem/          no network, no provider, no LLM            (enforced)
core/          no network, no provider implementation       (enforced)
sources/       provider-specific upstream behaviour        (enforced)
services/      cross-provider orchestration                (enforced)
```

`test_a_provider_never_imports_another_provider` and
`test_the_service_layer_does_not_import_provider_modules` hold the last two.
The services reason about `Paper` objects; they never reach into an adapter.

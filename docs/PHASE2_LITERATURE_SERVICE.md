# Phase 2.4 — LiteratureService

Deterministic orchestration. Not an agent.

```
    LiteratureSearchRequest
            │
            ├── OpenAlex  search  ──┐
            │                      ├─→ DOI grouping ─→ field-level merge
            └── Crossref  search  ──┘
                                          │
                             (opt-in)      ▼
                                     Unpaywall lookup
                                          │
                                 access reconciliation
                                          │
                                          ▼
                                LiteratureSearchResult
```

## 1. What it is responsible for

| Decision | Owner |
|---|---|
| which providers to ask, and what to do when one fails | **here** |
| which records are the same work | `services/literature_merge.py` (DOI only) |
| which value each field takes | `services/literature_merge.py` (field-level policy) |
| whether a full text may be retrieved | `core/policy/access.py` + `services/access_reconciliation.py` |
| any chemistry | `chem/` — never called from here |
| any HTTP | `sources/*` behind the transport — never called directly |
| ranking | **nobody, on purpose** |

It is not a chemistry layer, not a provider, not a policy engine and not a
model. It calls no LLM and has no ranking of any kind.

## 2. Search

```python
LiteratureSearchRequest(
    query="1,4-diphenyl-1,3-diyne synthesis",
    limit=20,
    include_access=False,     # opt-in; see §5
    max_access_calls=25,      # hard ceiling regardless of record count
    providers=("openalex", "crossref"),
)
```

Providers are asked in the order given. Results keep **discovery order** —
within a provider, the order it returned; across providers, the order they were
asked. Nothing here sorts by anything.

## 3. Provider failure

One provider failing does not lose the others, and does not raise.

| Situation | `SearchOutcome` |
|---|---|
| every requested provider answered | `SUCCESS` |
| some answered, some did not | `PARTIAL_SUCCESS` |
| every provider answered, none found anything | `NO_RESULTS` |
| no provider could answer | `ALL_PROVIDERS_FAILED` |

**`NO_RESULTS` and `ALL_PROVIDERS_FAILED` are different answers** and the model
makes that impossible to confuse. A caller that renders both as an empty list
will show "nothing found" when it should show "the search failed". Each provider
gets a `ProviderRun` recording its status, availability, record count, error
type, error text and duration — so a partial result says *which* source is
missing rather than silently looking thin.

`SKIPPED` is distinct from `ERROR`: a provider that was not asked, or that
cannot run here (no credential), is not a failure.

## 4. DOI lookup

```python
DoiLookupRequest(doi="https://doi.org/10.xxxx/abc", include_access=False)
```

Every DOI spelling normalises to the same lookup. A record nobody has is a
**normal answer**, not an error: the result says `found=False` with a reason, and
a provider answering with a *different* work reports what it actually holds.

## 5. Access enrichment is opt-in and bounded

Unpaywall costs one call per DOI. A search that quietly spends 40 of them turns
a metadata lookup into a budget decision nobody asked for, so:

- `include_access=False` (default) — OpenAlex and Crossref search, merge, stop.
  `access_skipped_reason` says so.
- `include_access=True` — Unpaywall is asked for **merged** DOIs only, never
  per provider record. Two providers agreeing on a DOI cost one call, not two.
- `max_access_calls` — a hard ceiling. When it is reached the result says so.
- A record with **no DOI** is never sent to Unpaywall. There is no identifier
  to give it, and inventing a query would be a different, unverified API.
- An access provider that fails, or is unavailable for want of a credential,
  degrades the *access* answer and leaves the search intact.

## 6. Merge boundary

The service groups by normalised DOI and delegates:

```python
groups = self._group_by_doi(collected)     # insertion-ordered
merged, merges, unresolved = self._merge(groups)
```

A record without a DOI gets its own group and is never absorbed — the same
conservatism the merge layer applies internally. `merged_count` and
`unresolved_count` are both reported, so a result set that is mostly singletons
does not look like a result set that merged cleanly.

## 7. Ranking is intentionally absent

There is no relevance score, no citation count ordering, no semantic ranking and
no LLM in the loop. **This is not relevance ranking.** Two runs of the same
query return the same order, and the order is the order the sources gave:

```
OpenAlex results, in OpenAlex's order
then Crossref results, in Crossref's order
deduplicated by DOI, first appearance winning
```

Making this a ranked list is a product decision, and it is the point at which a
model would first be useful — and therefore the point at which a result stops
being reproducible. That decision has not been made.

## 8. Result shape

```
LiteratureSearchResult
├── request              what was asked
├── outcome              SUCCESS | PARTIAL_SUCCESS | NO_RESULTS | ALL_PROVIDERS_FAILED
├── papers               merged, in discovery order
├── provider_runs        per provider: status, records, error, duration
├── merges               per identity group: resolutions, conflicts, author lists
├── merged_count / unresolved_count
├── access               reconciliations, only when include_access
├── access_calls         one entry per Unpaywall call: ok | error | skipped
├── access_skipped_reason
├── warnings
└── generated_at
```

None of this is folded into `Paper.notes`. A caller that wants to know whether
the search succeeded, which source answered, or whether access was checked reads
a field, not a sentence it has to parse.

## 9. Testing

`tests/integration_offline/test_literature_service.py` runs the real
`LiteratureService` against real providers with a `FakeTransport` underneath, so
the paths under test are the ones a live run takes. It covers: both providers
merging one DOI into one paper; each failure mode above; Unpaywall not being
called by default; the access budget as a ceiling; a record with no DOI never
reaching Unpaywall; a failing access provider not losing the search; order
stability across runs; and a `title=None` record surviving the entire chain.

The live path (`HttpxTransport`) is **NOT YET VERIFIED** — see the final
report.

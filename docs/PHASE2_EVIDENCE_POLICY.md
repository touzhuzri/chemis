# Evidence policy

How strong a piece of evidence is, and what that strength licenses.

## 1. Strength answers "how close is this to the thing itself"

Not "how much do we like it" and not "how confident are we". `EvidenceStrength`
already existed with four values and is reused unchanged — no second vocabulary
was created.

| Strength | Means |
|---|---|
| `PRIMARY_DOCUMENT_EVIDENCE` | the document itself: its full text, its experimental section, or a user-supplied file |
| `SECONDARY_METADATA_EVIDENCE` | a record *about* the work: a deposit, an index entry, an abstract |
| `DISCOVERY_EVIDENCE` | reconstructed text, or a claim whose own provenance is unestablished |
| `USER_PROVIDED_EVIDENCE` | a document the user brought |

## 2. The mapping

```
METADATA              -> SECONDARY_METADATA_EVIDENCE
STRUCTURED_DATABASE   -> SECONDARY_METADATA_EVIDENCE
FULLTEXT              -> PRIMARY_DOCUMENT_EVIDENCE
EXPERIMENTAL          -> PRIMARY_DOCUMENT_EVIDENCE
USER_PROVIDED_DOCUMENT-> USER_PROVIDED_EVIDENCE
ABSTRACT              -> decided by where the abstract text came from
```

An abstract's strength is **not** decided by its type, because an abstract can
come from three very different places. `AbstractRecord.evidence_strength`
already answers it and the policy reuses that answer:

| Abstract source | Strength |
|---|---|
| `PUBLISHER` | `SECONDARY_METADATA_EVIDENCE` |
| `CROSSREF` | `SECONDARY_METADATA_EVIDENCE` |
| `OPENALEX_RECONSTRUCTED` | **`DISCOVERY_EVIDENCE`** |
| `USER_PROVIDED` | `USER_PROVIDED_EVIDENCE` |

With no abstract record supplied at all, the answer is `DISCOVERY_EVIDENCE`.
Assuming a publisher deposit because nobody said otherwise is the exact failure
this policy exists to prevent.

## 3. Three rules that have each cost someone something

**An abstract is never primary document evidence.** "Compound X showed activity
against Y" is the authors' summary of a result. It is not a method. A Claim that
rests on it for an experimental fact is *flagged*, not rejected — the claim may
still be true, but nobody should be told a synthesis came from an abstract.

**A reconstruction is never promoted.** An OpenAlex abstract is reassembled from
a positional inverted index. The `reconstructed` flag travels on the Evidence
itself, so it cannot be lost in a merge or a re-derivation.

**A database record is secondary, whoever the database is.** Crossref is where a
publisher deposited its metadata; that is still metadata. A record having a DOI
does not make it a document, and the data licence of an index is never the
licence of a work in it.

## 4. Identity

```
evidence_id = content_hash(
    "evidence-id-v1" | source | source_id | evidence_type | locator.canonical() | raw_quote
)
```

`retrieved_at` is **deliberately excluded**. Re-fetching the same work and
finding the same sentence is the same piece of evidence, not a new one. Were the
timestamp an input, every `Claim.evidence_refs` entry would rot the first time a
citation was re-resolved, and two runs of the same pipeline would disagree about
whether they had ever cited the same thing.

Change the rule and the ID of already-cited evidence changes with it, which is
why the version string is an input.

## 5. One Evidence, one source

`Evidence.source` and `Evidence.source_id` are single non-empty strings, and
both are required by the type. A statement supported by three sources is a Claim
with three `evidence_refs` — never an Evidence with three sources, because
otherwise there is no way to say which one said what, and "two of them agree"
becomes unrepresentable.

## 6. Claim validation

`Claim.evidence_refs` is `Field(min_length=1)`. An unevidenced claim cannot be
constructed, which is the point: "trust me" is not an option the model offers.

`validate_claim` then checks referential integrity against an
`EvidenceRegistry` the caller supplies:

| Check | Result |
|---|---|
| reference not in the registry | **invalid** |
| the same reference twice | **invalid** — decided, not left open: citing one source twice is either a mistake or an attempt to make it look like corroboration |
| an experimental-fact claim resting on non-primary evidence | **flagged**, not invalid |
| registry id mismatch | noted |

It makes **no chemical judgement**. Whether the cited text really supports the
claim is a question for a reader; pretending otherwise in code would be worse
than saying nothing. Nothing is looked up: a missing reference is reported, not
fetched.

## 7. No confidence number

`Claim` has no `confidence`, `ai_confidence` or `score` field. No such quantity
is defined anywhere in this project, and a field with no definition invites a
number in it. A test pins the field set.

## 8. What has no producer yet

`FULLTEXT`, `EXPERIMENTAL`, `USER_PROVIDED_DOCUMENT` and `STRUCTURED_DATABASE`
are carried in the enum, are mapped by policy, and are covered by tests — with
no producer. That is deliberate: the model can carry a document source before
one exists, so a future producer fills them in rather than redesigning Evidence.

`Locator` is shaped for them already: `document_id`, `page`, `section` and
`char_range` are all expressible today, and all of them are part of the hashed
identity, so a locator produced later is comparable with one produced now.

# CREA Knowledge Base

Phase 3.11. Persistent chemical research knowledge in SQLite: what a
research run established -- entities, mentions, papers, evidence,
reactions, routes, sessions -- survives the process that created it, and
the next run *reuses* it instead of minting `CHEM-00000099` for a compound
that already has an id.

This is **knowledge, not cache** (§106): structured, traceable, with
provenance on every fact. It is also **not memory about people** (§45) --
no user profiles, no preferences, no chat history, and no delete API
(§90): research evidence accumulates.

## Architecture

```text
Domain Models
      ↓
Repository Protocol          (ChemicalEntityRepository / ReactionRepository,
      ↓                       the swap points 3.8.1/3.9 designed)
 ┌────┴─────────────┐
 ▼                  ▼
InMemory          SQLite     (src/crea/repositories/sqlite.py)
```

SQLite is a new *adapter*, not a new architecture. The registries
(`ChemicalEntityRegistry`, `ReactionRegistry`) run unchanged over either
backend; the in-memory implementations stay exactly where they were. The
`KnowledgeBase` facade (`src/crea/services/knowledge_base.py`) is the one
door: storage methods, deterministic queries, and `persist_research_result`
for one atomic research commit.

Layering (enforced by `tests/architecture/test_layering.py`):

- `core/` never sees `sqlite3` -- the domain is storage-free.
- `repositories/` may speak SQLite and nothing else: no LLM, no RDKit, no
  provider, no agent (§85). A repository that called PubChem would be a
  provider wearing a database's name.
- `services/` runs no SQL directly; the facade delegates to the store.
- `report/` and `agent/` never open a database. The agent may import the
  repository *error taxonomy* (`crea.repositories.errors`) -- those names
  exist so callers branch on decisions, not on SQLite strings (§59).
- RDKit stays solely in `chem/`. Structure questions go up to the
  chemistry abstraction (`exact` / `substructure`), never into SQL (§33).

## Schema (v1)

`schema_meta` stamps `schema_version = 1`. `initialize()` is idempotent
(`CREATE TABLE IF NOT EXISTS`); a database stamped with any other version
is **refused** with `UNSUPPORTED_SCHEMA_VERSION` -- never migrated in
place, never silently rewritten (§7/§8). The migration registry is the
future entry point; v1 has no migrations and pretends to no others.

Tables:

| table | holds | identity |
|---|---|---|
| `entities`, `entity_names`, `entity_external_identifiers` | ChemicalEntity + labels + outward pointers | `entity_id` (CHEM-…) UNIQUE, `identity_key` UNIQUE |
| `papers` | Paper (literature model, unchanged) | `paper_id` (DOI-primary) |
| `evidence` | Evidence, immutable once stored | `evidence_id` (content hash) UNIQUE |
| `mentions`, `paper_mentions` | ChemicalMention with its scope; Paper→Mention | `mention_id` (content hash of scope+locator+text) |
| `reactions`, `reaction_components`, `reaction_evidence` | Reaction + components + citations | `reaction_id`; UNIQUE(`reaction_key`,`paper_scope`) for identity-based keys only |
| `routes`, `route_steps`, `route_reaction_links` | CandidateRoute, steps, measured RouteStep↔Reaction links | `route_id`, `step_id`, `link_id` |
| `research_sessions`, `research_session_*` | one run's transcript + object links | `session_id` |

A note on `reaction_key UNIQUE` (§15): the reaction-key semantics take
precedence -- the same key in two papers is two reactions, and a
name-tainted key must never force a merge. So uniqueness is
(`reaction_key`, `paper_scope`) **restricted to identity-based keys** via
a partial unique index. Name-tainted keys (`name:...` materials) are
never merged (§21).

Structural relations carry FOREIGN KEYs (§89): reaction→components,
reaction→evidence citations, paper→mentions, route→steps, links, session
links. `PRAGMA foreign_keys = ON` at every connection (§53). No WAL: this
is a single-process CLI/test workload and the rollback journal is the
simpler correct choice (§54). One connection per `SQLiteStore`, no global
connection, `with KnowledgeBase(...) as kb:` for a bounded lifetime (§96).

JSON columns use one pinned serialization: `sort_keys=True`, fixed
separators, UTF-8 (§57). Never `repr`. Timestamps follow the project's
existing UTC ISO 8601 policy (`utc_now_iso`) (§58).

## Identity

The database mints **nothing**. `CHEM-00000001`, `RXN-…` hashes,
`evidence-id` content hashes -- all come from the layers above, unchanged
(§12/§18/§21). An INTEGER rowid may sit under a row as an implementation
detail; it is never the id anyone sees. No UUIDs, no autoincrement
business ids, no pickle, no RDKit objects (§34): the database stores
SMILES / InChI / InChIKey strings.

`entity_id` allocation continues across reopens: a new identity after a
restart mints the next free `CHEM-` number, never one that exists (§12).

## Deduplication / merge

| object | rule |
|---|---|
| Entity | one identity key = one entity. Re-registration returns the existing id and merges names / external identifiers / basis upgrades (§11/§92). Names never create entities. |
| Reaction | same `reaction_id` merges; identity-based key + same paper scope merges; name-tainted keys never merge; same key, other paper = two reactions (§15/§21/§93). Merges append evidence refs and notes; a CANDIDATE status may be raised by a later view and nothing else moves. |
| Evidence | `evidence_id` UNIQUE. A second write returns the stored evidence; quotes are **never rewritten** (§19/§48). New information = new Evidence. |
| Mention | keyed by scope + locator + text. `compound 7` in Paper A is never the mention of Paper B (§20). |
| Paper | DOI is the identity, per the existing literature merge semantics. NO_DOI works stay separate rows; nothing merges on title+year (§17). Stored merge conflicts never shrink (§94). |
| Session | sessions accumulate and are never overwritten (§44). |

Updates are append-only (§91): names, identifiers, provenance, evidence
refs and merge conflicts accumulate; source evidence never gets
overwritten. There is no delete API (§90); a test database is deleted as
a file.

## Queries

Deterministic always: listings order by `created_at` + stable id (§29).
There is no "best", "recommended", "most promising" or probability
anywhere. Queries return domain models, never rows or dicts (§95), and
are read-only (§108).

- Entities: by `entity_id`, `identity_key`, external identifier, name.
  `search_entities_by_name` returns **every** match and never creates one
  (§30/§31).
- Reactions: `search_reactions` requires at least one filter
  (entity/product/reactants/paper/doi/key/structure) or raises
  `INVALID_QUERY` (§32). Structure filters go through `crea.chem`
  (exact / substructure); SQLite only narrows candidates (§33).
- `get_papers_for_entity` goes through the mention layer
  Paper → ChemicalMention → ChemicalEntity (§65). `get_reactions_for_paper`
  reads the paper field of stored reactions (§66).
- `get_evidence_for_reaction` returns only citations that actually
  resolve -- a reaction with nothing behind it gets nothing (§67/§77).
- `get_routes_for_reaction` returns **stored** RouteStepReactionLink
  records only; nothing is generated from a structure match at query time
  (§68).
- `get_routes_for_entity` returns `{route_id, relation_role, step_id}`
  with the role stated (target / reactant / product / participant) (§69).
- `get_session` / `list_sessions` read run transcripts (§44).

## Transactions

Every `save_*` is one unit; `persist_research_result(...)` commits one
research result -- entities, mentions, papers, evidence, reactions
(each with components + citations), routes, links, session -- as one
transaction (§51/§52). A dangling citation FK mid-save rolls the whole
unit back: a reaction is never half-stored (§79). The error taxonomy is
`RepositoryError` / `NotFound` / `Duplicate` / `ConstraintViolation` /
`TransactionError` / `SchemaError`; `sqlite3.IntegrityError` never
escapes (§59).

Write order in a live run: held Evidence is knowledge the moment it is
held (§18), so the workflow and the extraction tool store it before
registering reactions whose citations must resolve at registration.

## KnowledgeBase ↔ ResearchWorkflow

```python
from crea.services.knowledge_base import KnowledgeBase

with KnowledgeBase(db_path) as kb:            # or KnowledgeBase() -> data/crea.db
    workflow, library, sink, registry = build_offline_workflow_stack(
        knowledge_base=kb)                    # knowledge_base=None stays in memory
    report = workflow.run(task, structure_input=..., document_id=...,
                          paper_id="paper-a") # the caller's paper assertion
```

With a knowledge base wired, the entity and reaction registries are the
persistent ones (one shared registry per stack) and the run's result is
committed when it finishes. Repeat research of the same PDF merges into
the established knowledge (content-addressed document/evidence/reaction
ids make this deterministic); incremental research accumulates
(§41-§43). `run(..., paper_id=, doi=)` is the caller's assertion of which
paper a supplied document is -- never inferred; without it, document-scope
is the honest scope.

## CLI

```bash
crea research --question "..." --smiles "..." [--pdf P] [--db PATH]
crea kb entity CHEM-00000001        [--db PATH] [--json]
crea kb reactions CHEM-00000001     [--db PATH] [--json]
crea kb papers CHEM-00000001        [--db PATH] [--json]
crea kb reaction RXN-...            [--db PATH] [--json]
crea kb evidence RXN-...            [--db PATH] [--json]
crea kb session <id> | kb sessions  [--db PATH] [--json]
crea kb export --output knowledge.json [--db PATH]
```

`--db` defaults to `data/crea.db` under the working directory (never a
hard-coded path; Windows/Linux alike) (§37/§38/§97/§98). Query commands
exit 0 on success, 1 on lookup miss, 2 on usage/schema errors. `--json`
is machine-readable JSON -- never a Python repr (§40).

## Export

`kb export` writes `knowledge.json`: entities, mentions, papers,
evidence, reactions, routes, sessions -- deterministic ordering, stable
ids, UTF-8, no credentials (§70/§109). It is a **logical knowledge
export**, explicitly not a SQLite backup (§71). Import is deliberately
not implemented (§72).

## What is not here

- No graph database, embeddings, vector index, RDF/SPARQL, PostgreSQL,
  Redis or Elasticsearch (§3). SQLite + repository + deterministic query,
  nothing else.
- No import/merge of foreign knowledge dumps (§72).
- No LLM writes (§107): only validated domain objects reach storage. The
  chain is LLM proposal → policy → domain object → repository.
- No status promotion by the database (§49): reaction status is computed
  by ReactionBuilder / evidence validation and merely *stored* here.

## Limitations

- `get_routes_for_entity`'s structure relation compares the identity
  layer's canonical SMILES strings; it is not a structure search (§33).
- A reaction may name a paper the database has not stored; the paper↔
  mention relation then appears only once the paper itself is stored.
  The reaction keeps its own claim in the meantime.
- Reaction *conditions* (temperature, yield, solvent) live on
  `ReactionRecord` and the evidence quotes behind it, not on the Reaction
  row; conflicting conditions across papers stay as separate reactions or
  as citations, never merged into one averaged row (§63/§64).
- `task.report` / `task.evidence` are not stored inside a session: they
  are renderings/copies of objects the tables already hold (§25/§106).
  Persist reports as Markdown files if you want them as artifacts.
- Single-process, single-connection. Multi-user concurrency is out of
  scope (§53).

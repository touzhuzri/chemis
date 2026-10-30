"""SQLite persistence for chemical research knowledge.

Phase 3.11. This is a new *adapter*, not a new architecture: the domain
models stay where they are, the repository protocols the registries were
built against stay where they are (`ChemicalEntityRepository`,
`ReactionRepository`), and this module is the second implementation of
them -- the one that survives a process ending.

```text
Domain Models
      ↓
Repository Protocol
      ↓
 ┌────┴─────────────┐
 ▼                  ▼
InMemory          SQLite   <- here
```

What lives in these tables is *knowledge*, not cache (§106): entities with
their identity keys, papers as literature records, evidence with its raw
quotes, reactions with their component/mention/entity chains, routes with
their step<->reaction links, and research sessions as transcripts. So the
rules are the ones research knowledge demands:

* **Append-only.** Re-saving evidence never changes its quote (§48).
  Entity names, external identifiers, reaction evidence refs and paper
  merge conflicts accumulate; they never shrink (§91).
* **Identity is the domain's.** `CHEM-00000001`, `RXN-...`, the content
  hashes -- all minted by the layers above. An INTEGER PRIMARY KEY may sit
  under a row as an implementation detail; it is never the id anyone sees
  (§12). No UUID, no autoincrement business identity, no pickle (§13/§34).
* **Dedup is the domain's too.** `evidence_id` UNIQUE (§19), entity
  `identity_key` UNIQUE (§10), reaction uniqueness as the reaction-key
  semantics demand -- identity-based keys unique per paper scope, name-
  tainted keys never merged (§15/§21). This module enforces and indexes;
  it never invents a merge rule.
* **No chemistry, no network, no LLM.** Structure questions go back up to
  `crea.chem` through the services (§33/§85). A repository that called
  PubChem would be a provider wearing a database's name.

Connection discipline (§53/§54): one connection per store, `foreign_keys`
ON at open, no global shared connection, `with SQLiteStore(path) as store`
for a bounded lifetime. No WAL -- this is a single-process CLI and test
workload, and a rollback journal is the simpler correct choice.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from crea.core.ids import normalize_doi
from crea.core.models.entity import ChemicalEntity, ChemicalMention, ExternalIdentifier
from crea.core.models.evidence import Evidence, Locator
from crea.core.models.identity import IdentityBasis, IdentityFacets
from crea.core.models.literature import Paper
from crea.core.models.reaction import (
    ComponentRole,
    ReactionConditions,
    ReactionRecord,
)
from crea.core.models.reaction_knowledge import (
    Reaction,
    ReactionComponent,
    ReactionStatus,
    RouteStepReactionLink,
    key_is_identity_based,
)
from crea.core.models.route import (
    CandidateRoute,
    ReactionMatchType,
    ReactionStep,
    RouteMolecule,
    ScoreSource,
    StepStatus,
    StockAssumption,
    SupportLevel,
    TransformationCandidate,
)
from crea.core.models.reaction_evidence import ReactionEvidence
from crea.core.models.research_study import (
    ArtifactType,
    ReferenceKind,
    ResearchStudy,
    ResearchStudyDetail,
    StudyArtifact,
    StudyEvent,
    StudyEventType,
    StudyReference,
    StudyStatus,
)
from crea.core.models.task_workflow import (
    TaskStatus,
    TaskType,
    TaskWorkflow,
    TaskWorkflowDetail,
    WorkflowStatus as TaskWorkflowStatus,
    WorkflowTask,
)
from crea.core.models.session import ResearchSession, WorkflowStatus
from crea.core.models.study_context import (
    FactVerification,
    HypothesisStatus,
    IdentityCandidate,
    IdentitySelectionRecord,
    IssueStatus,
    StudyDecision,
    StudyFact,
    StudyHypothesis,
    UnresolvedIssue,
)
from crea.core.models.golden_case import (
    EvaluationResult,
    GoldenCase,
    GoldenCaseStatus,
    GoldenCaseType,
    ReferenceMaterial,
    ResearchTrace,
)
from crea.core.models.golden_case_access import UnsealEvent, UnsealOutcome
from crea.core.models.workspace import Workspace, WorkspaceMessage
from crea.core.provenance import Provenance, utc_now_iso
from crea.repositories.errors import (
    ConstraintViolation,
    Duplicate,
    NotFound,
    RepositoryError,
    SchemaError,
    TransactionError,
    UNSUPPORTED_SCHEMA_VERSION,
)

#: The one schema version this build writes and reads. Bumping it means
#: writing a migration; older/newer files are refused, never silently
#: rewritten (§7/§8).
SCHEMA_VERSION = 1

_SCHEMA_VERSION_KEY = "schema_version"

#: Column order constants that carry meaning: a reaction's components sit
#: in the role buckets the model defines, in list order inside each bucket.
_BUCKET_ORDER = (
    ("reactants", ComponentRole.REACTANT),
    ("products", ComponentRole.PRODUCT),
    ("reagents", ComponentRole.REAGENT),
    ("catalysts", ComponentRole.CATALYST),
    ("solvents", ComponentRole.SOLVENT),
    ("unassigned", ComponentRole.UNKNOWN),
)


def default_db_path() -> Path:
    """Where the knowledge base lives when nobody says (§37/§98).

    Project-relative `data/crea.db`, resolved against the working
    directory. Never a hard-coded drive or absolute path: the CLI runs on
    Windows and Linux, and `--db` always wins over this default.
    """
    return Path("data") / "crea.db"


def dump_json(value) -> str:
    """The one JSON shape anything is stored in (§57).

    `sort_keys` pinned, separators pinned: the same value produces the
    same bytes today and after a restart, so a re-save that changes
    nothing is visible as a no-op rather than as churn. Never `repr`.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


#: Keys that look like credentials never enter the database (§87/§88):
#: provider tokens, Authorization headers, API keys. Provenance params are
#: caller-controlled free-form, so they are scrubbed on the way in -- a
#: redaction beats a leaked bearer token in an export.
_SECRET_KEY_RE = re.compile(
    r"(authorization|api[-_]?key|apikey|token|secret|password|credential"
    r"|bearer|cookie|set-cookie)", re.IGNORECASE)


def scrub(value):
    """Recursively redact secret-looking keys before anything is stored."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                out[key] = "[REDACTED]"
            else:
                out[key] = scrub(item)
        return out
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def load_json(text: str | None):
    if not text:
        return None
    return json.loads(text)


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    # -- entities (§10/§11/§12): one row per established identity. The
    #    INTEGER rowid is an implementation detail nobody outside this
    #    module ever sees.
    """CREATE TABLE IF NOT EXISTS entities (
        entity_id            TEXT PRIMARY KEY,
        identity_key         TEXT NOT NULL UNIQUE,
        identity_key_version TEXT NOT NULL,
        identity_basis       TEXT NOT NULL,
        facets_json          TEXT NOT NULL,
        canonical_smiles     TEXT NOT NULL DEFAULT '',
        inchi                TEXT NOT NULL DEFAULT '',
        inchikey             TEXT NOT NULL DEFAULT '',
        molecular_formula    TEXT,
        molecular_weight     REAL,
        identity_source      TEXT NOT NULL DEFAULT '',
        source_id            TEXT NOT NULL DEFAULT '',
        source_url           TEXT,
        retrieved_at         TEXT,
        provenance_json      TEXT,
        notes_json           TEXT NOT NULL,
        created_at           TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS entity_names (
        entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
        name      TEXT NOT NULL,
        position  INTEGER NOT NULL,
        PRIMARY KEY (entity_id, name)
    )""",
    # A PubChem CID is an outward pointer, never the entity's id (§12).
    """CREATE TABLE IF NOT EXISTS entity_external_identifiers (
        entity_id  TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
        namespace  TEXT NOT NULL,
        identifier TEXT NOT NULL,
        source     TEXT NOT NULL DEFAULT '',
        position   INTEGER NOT NULL,
        PRIMARY KEY (namespace, identifier)
    )""",
    # Standard InChIKey is a search key, not the dedup key (§22): the UNIQUE
    # identity_key (Standard InChI) already prevents duplicate identities.
    """CREATE INDEX IF NOT EXISTS idx_entities_inchikey ON entities(inchikey)""",
    # -- papers (§16/§17): the literature identity the merge layer already
    #    established. DOI is the identity; two NO_DOI works stay two rows,
    #    and nothing here ever merges on title+year.
    """CREATE TABLE IF NOT EXISTS papers (
        paper_id            TEXT PRIMARY KEY,
        doi                 TEXT,
        title               TEXT,
        journal             TEXT,
        year                INTEGER,
        volume              TEXT,
        issue               TEXT,
        pages               TEXT,
        authors_json        TEXT NOT NULL,
        abstracts_json      TEXT NOT NULL,
        identifiers_json    TEXT NOT NULL,
        access_json         TEXT NOT NULL,
        url                 TEXT,
        upstream_json       TEXT NOT NULL,
        sources_json        TEXT NOT NULL,
        source_ids_json     TEXT NOT NULL,
        retrieved_at        TEXT NOT NULL,
        raw_hashes_json     TEXT NOT NULL,
        merge_conflicts_json TEXT NOT NULL
    )""",
    # -- evidence (§18/§19/§48): citable units, immutable once stored.
    """CREATE TABLE IF NOT EXISTS evidence (
        evidence_id    TEXT PRIMARY KEY,
        evidence_type  TEXT NOT NULL,
        strength       TEXT NOT NULL,
        source         TEXT NOT NULL,
        source_id      TEXT NOT NULL,
        source_url     TEXT,
        retrieved_at   TEXT NOT NULL,
        raw_quote      TEXT NOT NULL,
        locator_json   TEXT NOT NULL,
        provenance_json TEXT,
        paper_id       TEXT,
        doi            TEXT
    )""",
    # -- mentions (§20/§21): every occurrence keeps its scope. "compound 7"
    #    in one paper is never the mention of "compound 7" in another; the
    #    id is content_hash(source_id|start|text) from the extraction layer
    #    and is reused here rather than re-minted.
    """CREATE TABLE IF NOT EXISTS mentions (
        mention_id         TEXT PRIMARY KEY,
        source             TEXT NOT NULL DEFAULT '',
        source_id          TEXT NOT NULL DEFAULT '',
        raw_text           TEXT NOT NULL,
        normalized_text    TEXT NOT NULL DEFAULT '',
        entity_id          TEXT REFERENCES entities(entity_id),
        resolution_status  TEXT NOT NULL,
        locator_json       TEXT,
        evidence_refs_json TEXT NOT NULL,
        explicit_smiles    TEXT NOT NULL DEFAULT '',
        explicit_inchi     TEXT NOT NULL DEFAULT '',
        context_before     TEXT NOT NULL DEFAULT '',
        context_after      TEXT NOT NULL DEFAULT ''
    )""",
    # Paper -> ChemicalMention -> ChemicalEntity (§65): the middle layer is
    # a relation, not a column on either side.
    """CREATE TABLE IF NOT EXISTS paper_mentions (
        paper_id   TEXT NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
        mention_id TEXT NOT NULL REFERENCES mentions(mention_id) ON DELETE CASCADE,
        PRIMARY KEY (paper_id, mention_id)
    )""",
    # -- reactions (§13/§15). Uniqueness follows the reaction-key semantics
    #    exactly: the id is the hard identity (same report registered twice
    #    is one reaction), and the (key, scope) unique index applies only to
    #    identity-based keys -- a name-tainted key must never force a merge
    #    of two reports that merely spelled things the same way (§21).
    """CREATE TABLE IF NOT EXISTS reactions (
        reaction_id                 TEXT PRIMARY KEY,
        reaction_key                TEXT NOT NULL,
        reaction_key_version        TEXT NOT NULL,
        key_is_identity_based       INTEGER NOT NULL CHECK (key_is_identity_based IN (0, 1)),
        status                      TEXT NOT NULL,
        record_id                   TEXT NOT NULL DEFAULT '',
        paper_id                    TEXT NOT NULL DEFAULT '',
        doi                         TEXT NOT NULL DEFAULT '',
        source_id                   TEXT NOT NULL DEFAULT '',
        document_id                 TEXT NOT NULL DEFAULT '',
        paper_scope                 TEXT NOT NULL,
        evidence_refs_json          TEXT NOT NULL,
        unresolved_evidence_refs_json TEXT NOT NULL,
        locator_json                TEXT,
        provenance_json             TEXT,
        notes_json                  TEXT NOT NULL,
        created_at                  TEXT NOT NULL,
        reaction_class              TEXT NOT NULL DEFAULT '',
        atom_mapping_json           TEXT
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS uniq_identity_reaction_key
        ON reactions(reaction_key, paper_scope)
        WHERE key_is_identity_based = 1""",
    # -- reaction components (§14): roles stay distinct. There is no
    #    "materials" bucket; a substance the source never named a role for
    #    keeps UNKNOWN rather than being filed somewhere plausible.
    """CREATE TABLE IF NOT EXISTS reaction_components (
        component_pk     INTEGER PRIMARY KEY,
        reaction_id      TEXT NOT NULL REFERENCES reactions(reaction_id) ON DELETE CASCADE,
        bucket           TEXT NOT NULL,
        position         INTEGER NOT NULL,
        role             TEXT NOT NULL,
        name             TEXT NOT NULL DEFAULT '',
        mention_id       TEXT REFERENCES mentions(mention_id),
        entity_id        TEXT REFERENCES entities(entity_id),
        identity_key     TEXT NOT NULL DEFAULT '',
        canonical_smiles TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE INDEX IF NOT EXISTS idx_reaction_components_entity
        ON reaction_components(entity_id)""",
    # Reaction <-> Evidence is a relation of citations (§66/§67): only refs
    # that actually resolve live here, and the FK enforces it. The reaction
    # row keeps its full ref list; this table is what "evidence behind it"
    # can mean.
    """CREATE TABLE IF NOT EXISTS reaction_evidence (
        reaction_id TEXT NOT NULL REFERENCES reactions(reaction_id) ON DELETE CASCADE,
        evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
        position    INTEGER NOT NULL,
        PRIMARY KEY (reaction_id, evidence_id)
    )""",
    # -- routes (§22): candidate routes and their steps, as domain models.
    #    No AiZynthFinder object is stored or referenced.
    """CREATE TABLE IF NOT EXISTS routes (
        route_id            TEXT PRIMARY KEY,
        target_json         TEXT NOT NULL,
        score               REAL,
        score_source        TEXT NOT NULL,
        source              TEXT NOT NULL,
        provider_version    TEXT,
        stock_configuration TEXT,
        evidence_refs_json  TEXT NOT NULL,
        warnings_json       TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS route_steps (
        step_id                   TEXT PRIMARY KEY,
        route_id                  TEXT NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
        step_index                INTEGER NOT NULL,
        products_json             TEXT NOT NULL,
        reactants_json            TEXT NOT NULL,
        reagents_json             TEXT NOT NULL,
        transformation_json       TEXT,
        source                    TEXT NOT NULL,
        score                     REAL,
        score_source              TEXT NOT NULL,
        stock_assumptions_json    TEXT NOT NULL,
        status                    TEXT NOT NULL,
        support_level             TEXT NOT NULL,
        evidence_refs_json        TEXT NOT NULL,
        paper_ids_json            TEXT NOT NULL,
        reaction_record_ids_json  TEXT NOT NULL,
        warnings_json             TEXT NOT NULL
    )""",
    # RouteStep -> Reaction (§23). The link is a comparison record; the
    # evidence rung lives in the evidence chain, not on this row.
    """CREATE TABLE IF NOT EXISTS route_reaction_links (
        link_id        TEXT PRIMARY KEY,
        route_id       TEXT NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
        step_id        TEXT NOT NULL REFERENCES route_steps(step_id) ON DELETE CASCADE,
        reaction_id    TEXT NOT NULL REFERENCES reactions(reaction_id) ON DELETE CASCADE,
        match_type     TEXT NOT NULL,
        link_basis     TEXT NOT NULL,
        screening_only INTEGER NOT NULL CHECK (screening_only IN (0, 1)),
        source_id      TEXT NOT NULL DEFAULT '',
        doi            TEXT NOT NULL DEFAULT '',
        notes_json     TEXT NOT NULL
    )""",
    # -- sessions (§24/§25): one research run's transcript. The id lists are
    #    relation tables, not copies of the objects themselves.
    """CREATE TABLE IF NOT EXISTS research_sessions (
        session_id       TEXT PRIMARY KEY,
        task_json        TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        status           TEXT NOT NULL,
        phases_json      TEXT NOT NULL,
        findings_json    TEXT NOT NULL,
        sources_json     TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS research_session_entities (
        session_id TEXT NOT NULL REFERENCES research_sessions(session_id) ON DELETE CASCADE,
        entity_id  TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
        position   INTEGER NOT NULL,
        PRIMARY KEY (session_id, entity_id)
    )""",
    """CREATE TABLE IF NOT EXISTS research_session_mentions (
        session_id TEXT NOT NULL REFERENCES research_sessions(session_id) ON DELETE CASCADE,
        mention_id TEXT NOT NULL REFERENCES mentions(mention_id) ON DELETE CASCADE,
        position   INTEGER NOT NULL,
        PRIMARY KEY (session_id, mention_id)
    )""",
    """CREATE TABLE IF NOT EXISTS research_session_reactions (
        session_id  TEXT NOT NULL REFERENCES research_sessions(session_id) ON DELETE CASCADE,
        reaction_id TEXT NOT NULL REFERENCES reactions(reaction_id) ON DELETE CASCADE,
        position    INTEGER NOT NULL,
        PRIMARY KEY (session_id, reaction_id)
    )""",
    """CREATE TABLE IF NOT EXISTS research_session_papers (
        session_id TEXT NOT NULL REFERENCES research_sessions(session_id) ON DELETE CASCADE,
        paper_id   TEXT NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
        position   INTEGER NOT NULL,
        PRIMARY KEY (session_id, paper_id)
    )""",
    """CREATE TABLE IF NOT EXISTS research_session_evidence (
        session_id  TEXT NOT NULL REFERENCES research_sessions(session_id) ON DELETE CASCADE,
        evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
        position    INTEGER NOT NULL,
        PRIMARY KEY (session_id, evidence_id)
    )""",
    """CREATE TABLE IF NOT EXISTS research_session_routes (
        session_id TEXT NOT NULL REFERENCES research_sessions(session_id) ON DELETE CASCADE,
        route_id   TEXT NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
        position   INTEGER NOT NULL,
        PRIMARY KEY (session_id, route_id)
    )""",
    # -- reaction records (Phase 3.12, §25-§28): the experimental basis of a
    #    Reaction. `ReactionRecord` is the intermediate extraction result;
    #    `Reaction` is the stable knowledge object -- this table is the
    #    chain between them, never a second parallel reaction system. The
    #    conditions keep raw_value/raw_unit exactly as written (§27); no
    #    unit conversion happens in storage or anywhere else.
    """CREATE TABLE IF NOT EXISTS reaction_records (
        record_id                    TEXT PRIMARY KEY,
        reaction_id                  TEXT NOT NULL REFERENCES reactions(reaction_id) ON DELETE CASCADE,
        source                       TEXT NOT NULL DEFAULT '',
        document_id                  TEXT,
        paper_id                     TEXT,
        reaction_description         TEXT NOT NULL DEFAULT '',
        description_evidence_refs_json TEXT NOT NULL,
        conditions_json              TEXT NOT NULL,
        evidence_refs_json           TEXT NOT NULL,
        raw_experimental_text        TEXT NOT NULL DEFAULT '',
        conflicts_json               TEXT NOT NULL,
        rejected_json                TEXT NOT NULL,
        extractor                    TEXT NOT NULL DEFAULT '',
        policy_version               TEXT NOT NULL DEFAULT '',
        created_at                   TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_reaction_records_reaction
        ON reaction_records(reaction_id)""",
    # -- chemistry workspaces (CREA 4.0 Stage F, §4/§20): a conversation
    #    container and its messages, nothing more. `position` is the append
    #    order inside one workspace; the message body is a single JSON
    #    column so the AgentResult fields (tool_calls/evidence/provenance/
    #    diagnostics) travel as one object, never a second message schema.
    """CREATE TABLE IF NOT EXISTS workspaces (
        workspace_id TEXT PRIMARY KEY,
        title        TEXT NOT NULL,
        created_at   TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS workspace_messages (
        workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
        position     INTEGER NOT NULL,
        message_json TEXT NOT NULL,
        PRIMARY KEY (workspace_id, position)
    )""",
    # -- reaction-template evidence (Stage 3.4, §21): what supports a
    #    ReactionTemplate. Distinct from `reaction_evidence`, which links a
    #    Reaction object to literature Evidence. The template library is
    #    code-defined, so no FK -- the body is one JSON column keyed by id.
    """CREATE TABLE IF NOT EXISTS reaction_template_evidence (
        evidence_id          TEXT PRIMARY KEY,
        reaction_template_id TEXT NOT NULL,
        source_type          TEXT NOT NULL DEFAULT '',
        source_id            TEXT NOT NULL DEFAULT '',
        evidence_json        TEXT NOT NULL,
        created_at           TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_reaction_template_evidence_template
        ON reaction_template_evidence(reaction_template_id)""",
    # Reverse lookup: which evidence cites this source (Stage 4.4 §9).
    """CREATE INDEX IF NOT EXISTS idx_reaction_template_evidence_source
        ON reaction_template_evidence(source_type, source_id)""",
    # -- research sessions (Stage 7.1): a process container. References point
    #    at existing objects (never copied); events are an append-only timeline;
    #    artifacts hold AI results that cannot be recomputed from the database.
    """CREATE TABLE IF NOT EXISTS research_studies (
        study_id            TEXT PRIMARY KEY,
        title               TEXT NOT NULL DEFAULT '',
        question            TEXT NOT NULL DEFAULT '',
        status              TEXT NOT NULL,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        start_molecule_id   TEXT NOT NULL DEFAULT '',
        target_molecule_id  TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS research_study_references (
        study_id     TEXT NOT NULL REFERENCES research_studies(study_id) ON DELETE CASCADE,
        reference_id TEXT NOT NULL,
        kind         TEXT NOT NULL,
        object_id    TEXT NOT NULL,
        label        TEXT NOT NULL DEFAULT '',
        added_at     TEXT NOT NULL,
        note         TEXT NOT NULL DEFAULT '',
        position     INTEGER NOT NULL,
        PRIMARY KEY (study_id, reference_id)
    )""",
    """CREATE TABLE IF NOT EXISTS research_study_events (
        study_id     TEXT NOT NULL REFERENCES research_studies(study_id) ON DELETE CASCADE,
        event_id     TEXT NOT NULL,
        event_type   TEXT NOT NULL,
        at           TEXT NOT NULL,
        summary      TEXT NOT NULL DEFAULT '',
        reference_id TEXT NOT NULL DEFAULT '',
        artifact_id  TEXT NOT NULL DEFAULT '',
        note         TEXT NOT NULL DEFAULT '',
        position     INTEGER NOT NULL,
        PRIMARY KEY (study_id, event_id)
    )""",
    """CREATE TABLE IF NOT EXISTS research_study_artifacts (
        study_id      TEXT NOT NULL REFERENCES research_studies(study_id) ON DELETE CASCADE,
        artifact_id   TEXT NOT NULL,
        artifact_type TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        label         TEXT NOT NULL DEFAULT '',
        content_json  TEXT NOT NULL,
        position      INTEGER NOT NULL,
        PRIMARY KEY (study_id, artifact_id)
    )""",
    # -- study context (M1.5): what one study knows across sessions. Every
    #    table is keyed by study_id first: isolation is the schema's job, not
    #    only the service's. Additive -- schema version stays v1; initialize()
    #    creates missing tables on an existing v1 database.
    """CREATE TABLE IF NOT EXISTS study_context_facts (
        study_id          TEXT NOT NULL REFERENCES research_studies(study_id) ON DELETE CASCADE,
        fact_id           TEXT NOT NULL,
        session_id        TEXT NOT NULL DEFAULT '',
        fact_type         TEXT NOT NULL,
        verification      TEXT NOT NULL,
        source            TEXT NOT NULL DEFAULT '',
        entity_id         TEXT NOT NULL DEFAULT '',
        summary           TEXT NOT NULL DEFAULT '',
        payload_json      TEXT NOT NULL DEFAULT '{}',
        provenance_json   TEXT,
        evidence_ids_json TEXT NOT NULL DEFAULT '[]',
        created_at        TEXT NOT NULL,
        position          INTEGER NOT NULL,
        PRIMARY KEY (study_id, fact_id)
    )""",
    """CREATE TABLE IF NOT EXISTS study_context_hypotheses (
        study_id                    TEXT NOT NULL REFERENCES research_studies(study_id) ON DELETE CASCADE,
        hypothesis_id               TEXT NOT NULL,
        session_id                  TEXT NOT NULL DEFAULT '',
        content                     TEXT NOT NULL,
        entity_id                   TEXT NOT NULL DEFAULT '',
        status                      TEXT NOT NULL,
        supporting_evidence_json    TEXT NOT NULL DEFAULT '[]',
        contradicting_evidence_json TEXT NOT NULL DEFAULT '[]',
        created_at                  TEXT NOT NULL,
        updated_at                  TEXT NOT NULL,
        note                        TEXT NOT NULL DEFAULT '',
        position                    INTEGER NOT NULL,
        PRIMARY KEY (study_id, hypothesis_id)
    )""",
    """CREATE TABLE IF NOT EXISTS study_context_decisions (
        study_id             TEXT NOT NULL REFERENCES research_studies(study_id) ON DELETE CASCADE,
        decision_id          TEXT NOT NULL,
        session_id           TEXT NOT NULL DEFAULT '',
        decision             TEXT NOT NULL,
        reason               TEXT NOT NULL DEFAULT '',
        observation_kind     TEXT NOT NULL DEFAULT '',
        plan_change_kinds_json TEXT NOT NULL DEFAULT '[]',
        entity_ids_json      TEXT NOT NULL DEFAULT '[]',
        evidence_ids_json    TEXT NOT NULL DEFAULT '[]',
        hypothesis_ids_json  TEXT NOT NULL DEFAULT '[]',
        created_at           TEXT NOT NULL,
        position             INTEGER NOT NULL,
        PRIMARY KEY (study_id, decision_id)
    )""",
    """CREATE TABLE IF NOT EXISTS study_context_issues (
        study_id    TEXT NOT NULL REFERENCES research_studies(study_id) ON DELETE CASCADE,
        issue_id    TEXT NOT NULL,
        session_id  TEXT NOT NULL DEFAULT '',
        kind        TEXT NOT NULL,
        content     TEXT NOT NULL,
        status      TEXT NOT NULL,
        entity_id   TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        resolved_at TEXT NOT NULL DEFAULT '',
        resolution  TEXT NOT NULL DEFAULT '',
        position    INTEGER NOT NULL,
        PRIMARY KEY (study_id, issue_id)
    )""",
    """CREATE TABLE IF NOT EXISTS study_context_identity_records (
        study_id                 TEXT NOT NULL REFERENCES research_studies(study_id) ON DELETE CASCADE,
        record_id                TEXT NOT NULL,
        session_id               TEXT NOT NULL DEFAULT '',
        question                 TEXT NOT NULL DEFAULT '',
        candidates_json          TEXT NOT NULL DEFAULT '[]',
        selected_index           INTEGER,
        selected_by              TEXT NOT NULL DEFAULT '',
        user_selection_statement TEXT NOT NULL DEFAULT '',
        selected_at              TEXT NOT NULL DEFAULT '',
        created_at               TEXT NOT NULL,
        position                 INTEGER NOT NULL,
        PRIMARY KEY (study_id, record_id)
    )""",
    # -- research workflows (Stage 7.2): a bounded task set over a session.
    #    Tasks reference artifacts by id; they never copy results.
    """CREATE TABLE IF NOT EXISTS task_workflows (
        workflow_id TEXT PRIMARY KEY,
        study_id    TEXT NOT NULL REFERENCES research_studies(study_id) ON DELETE CASCADE,
        template    TEXT NOT NULL DEFAULT '',
        title       TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_tasks (
        task_id             TEXT PRIMARY KEY,
        workflow_id         TEXT NOT NULL REFERENCES task_workflows(workflow_id) ON DELETE CASCADE,
        task_type           TEXT NOT NULL,
        status              TEXT NOT NULL,
        title               TEXT NOT NULL DEFAULT '',
        input_refs_json     TEXT NOT NULL,
        depends_on_json     TEXT NOT NULL,
        output_artifact_ids_json TEXT NOT NULL,
        attempt_count       INTEGER NOT NULL DEFAULT 0,
        error               TEXT NOT NULL DEFAULT '',
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        position            INTEGER NOT NULL
    )""",
    # -- golden cases (M1.7.2): the evaluation infrastructure. Cases,
    #    references, traces, evaluations and unseal events live in their
    #    own tables, keyed by case_id, with no foreign key into
    #    research_studies: deleting or archiving a case can never touch a
    #    study's data, and a study knows nothing about cases. Content_json
    #    is the source of truth (the full model); scalar columns exist for
    #    querying and are rewritten from the same object on every save, so
    #    they cannot drift. Append-only where the domain demands it:
    #    unseal events have INSERT and LIST and no update path at all.
    """CREATE TABLE IF NOT EXISTS golden_cases (
        case_id        TEXT PRIMARY KEY,
        name           TEXT NOT NULL,
        case_type      TEXT NOT NULL,
        case_version   TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        status         TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        content_json   TEXT NOT NULL,
        position       INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS golden_case_references (
        case_id       TEXT NOT NULL REFERENCES golden_cases(case_id) ON DELETE CASCADE,
        reference_id  TEXT NOT NULL,
        hidden        INTEGER NOT NULL DEFAULT 1,
        content_json  TEXT NOT NULL,
        position      INTEGER NOT NULL,
        PRIMARY KEY (case_id, reference_id)
    )""",
    """CREATE TABLE IF NOT EXISTS golden_case_traces (
        trace_id      TEXT PRIMARY KEY,
        case_id       TEXT NOT NULL REFERENCES golden_cases(case_id) ON DELETE CASCADE,
        case_version  TEXT NOT NULL,
        session_id    TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT '',
        content_json  TEXT NOT NULL,
        position      INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS golden_case_evaluations (
        evaluation_id      TEXT PRIMARY KEY,
        case_id            TEXT NOT NULL REFERENCES golden_cases(case_id) ON DELETE CASCADE,
        case_version       TEXT NOT NULL,
        trace_id           TEXT NOT NULL,
        evaluation_version TEXT NOT NULL,
        content_json       TEXT NOT NULL,
        position           INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS golden_case_unseal_events (
        event_id         TEXT PRIMARY KEY,
        case_id          TEXT NOT NULL REFERENCES golden_cases(case_id) ON DELETE CASCADE,
        case_version     TEXT NOT NULL,
        reference_id     TEXT NOT NULL,
        outcome          TEXT NOT NULL,
        actor            TEXT NOT NULL,
        reason           TEXT NOT NULL,
        previous_hidden  INTEGER NOT NULL,
        new_hidden       INTEGER NOT NULL,
        diagnostics      TEXT NOT NULL DEFAULT '',
        at               TEXT NOT NULL,
        position         INTEGER NOT NULL
    )""",
    # Standard DOI uniqueness (Stage 4.1 §26): one record per normalized DOI.
    # NULL DOIs are unlimited (a work with no DOI is still a record, §8), which
    # SQLite permits in a UNIQUE index.
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_doi_unique
        ON papers(doi)""",
)


def _model_dump(model) -> str:
    """JSON column text for a pydantic model or a list of them.

    Scrubbed on the way in (§87): provenance params are free-form caller
    data, and a credential-looking key is redacted before storage.
    """
    if isinstance(model, list):
        return dump_json(scrub([m.model_dump(mode="json") for m in model]))
    return dump_json(scrub(model.model_dump(mode="json")))


def _merge_str_lists(keep: list[str], incoming: list[str]) -> list[str]:
    """Append-only list union: original order first, new entries after (§91)."""
    out = list(keep)
    for item in incoming:
        if item not in out:
            out.append(item)
    return out


class SQLiteStore:
    """The SQLite adapter: schema, transactions, and typed knowledge I/O.

    One instance owns one connection (§53). Use it as a context manager
    (§96); `close()` is idempotent and every method refuses to run on a
    closed store with a `RepositoryError` rather than leaking an
    `sqlite3.ProgrammingError`.

    The read methods return domain models, never rows or dicts (§95). SQL
    is this module's business and stays here.
    """

    def __init__(self, db_path) -> None:
        self._path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._tx_depth = 0
        self._closed = False

    # -- lifecycle ----------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def schema_version(self) -> int:
        """The version stamped in this database (0 before `initialize`)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                (_SCHEMA_VERSION_KEY,)).fetchone()
        except sqlite3.Error:
            return 0
        return int(row[0]) if row else 0

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise RepositoryError("the knowledge store is closed")
        if self._conn is None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._conn = sqlite3.connect(self._path)
                self._conn.row_factory = sqlite3.Row
                # Per-connection and mandatory: the FKs in the schema are
                # decoration unless this is on (§53).
                self._conn.execute("PRAGMA foreign_keys = ON")
                # Concurrent research jobs (Phase 4 §38) queue on the write
                # lock instead of failing on "database is locked"; 5s is far
                # beyond any research transaction in this system.
                self._conn.execute("PRAGMA busy_timeout = 5000")
            except sqlite3.Error as exc:
                raise RepositoryError(
                    f"cannot open knowledge base at {self._path}: {exc}") from exc
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
                self._tx_depth = 0
        self._closed = True

    def __enter__(self) -> "SQLiteStore":
        self.initialize()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def initialize(self) -> None:
        """Create the schema if absent, verify it if present (§6/§7).

        Idempotent: running this on an existing v1 database is a no-op.
        A database stamped with any other version is refused with
        `UNSUPPORTED_SCHEMA_VERSION` -- never migrated in place, never
        silently rewritten (§8: the migration registry is the future entry
        point; v1 has no migrations and pretends to no others).
        """
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_meta ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                row = conn.execute(
                    "SELECT value FROM schema_meta WHERE key = ?",
                    (_SCHEMA_VERSION_KEY,)).fetchone()
                if row is None:
                    for statement in _SCHEMA_STATEMENTS:
                        conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
                        (_SCHEMA_VERSION_KEY, str(SCHEMA_VERSION)))
                    self._ensure_additive_columns()
                    return
                stored = int(row[0])
                if stored != SCHEMA_VERSION:
                    raise SchemaError(
                        f"UNSUPPORTED_SCHEMA_VERSION: this build speaks "
                        f"schema {SCHEMA_VERSION}, the database at "
                        f"{self._path} is schema {stored}; refusing to "
                        f"touch it",
                        code=UNSUPPORTED_SCHEMA_VERSION)
                # Same version: make sure every table exists (idempotent)
                # without rewriting anything that does.
                for statement in _SCHEMA_STATEMENTS:
                    conn.execute(statement)
            self._ensure_additive_columns()
        except SchemaError:
            raise
        except sqlite3.Error as exc:
            raise SchemaError(f"cannot initialize schema: {exc}") from exc

    def _ensure_additive_columns(self) -> None:
        """Additive columns only (Stage D §20): a new column with a default
        never rewrites or invalidates stored rows. Schema version stays
        v1 -- additive evolution is the compatible case; anything that is
        not additive still demands a version bump and a refusal.
        """
        wanted = {
            "reactions": {
                "reaction_class": "TEXT NOT NULL DEFAULT ''",
                "atom_mapping_json": "TEXT",
            },
            "reaction_template_evidence": {
                "source_type": "TEXT NOT NULL DEFAULT ''",
                "source_id": "TEXT NOT NULL DEFAULT ''",
            },
        }
        conn = self._connect()
        for table, columns in wanted.items():
            existing = {row["name"] for row in conn.execute(
                f"PRAGMA table_info({table})")}
            for name, declaration in columns.items():
                if name not in existing:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    @contextmanager
    def transaction(self):
        """One unit of work (§51). Nested calls join the outer transaction.

        Anything that raises inside rolls the whole unit back: a reaction
        never lands with half its components or with a citation the
        evidence table cannot resolve (§52/§79). Integrity failures surface
        as `ConstraintViolation`; other SQLite failures as
        `TransactionError`. `sqlite3.IntegrityError` itself never escapes.
        """
        conn = self._connect()
        if self._tx_depth == 0:
            conn.execute("BEGIN IMMEDIATE")
        self._tx_depth += 1
        try:
            yield self
        except Exception:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        else:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                try:
                    conn.execute("COMMIT")
                except sqlite3.Error as exc:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise TransactionError(
                        f"commit failed: {exc}") from exc

    def _execute(self, sql: str, params=()) -> sqlite3.Cursor:
        conn = self._connect()
        try:
            if self._tx_depth > 0:
                return conn.execute(sql, params)
            with conn:
                return conn.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            text = str(exc)
            if "UNIQUE" in text or "PRIMARY KEY" in text:
                raise Duplicate(f"write collided with stored knowledge: "
                                f"{text}") from exc
            raise ConstraintViolation(f"constraint refused the write: "
                                      f"{text}") from exc
        except sqlite3.Error as exc:
            raise TransactionError(f"statement failed: {exc}") from exc

    def _query(self, sql: str, params=()) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise TransactionError(f"query failed: {exc}") from exc

    # -- entities (§10-§12) -------------------------------------------------

    def save_entity(self, entity: ChemicalEntity) -> ChemicalEntity:
        """Store an entity. Re-storing enriches, never duplicates (§92).

        Scalar identity fields follow the incoming object (the registry
        already merged what was mergeable); names, external identifiers and
        notes accumulate; `created_at` of the first store stands, so
        registration order is stable across re-saves (§29).
        """
        with self.transaction():
            row = self._query(
                "SELECT created_at, notes_json FROM entities WHERE entity_id = ?",
                (entity.entity_id,))
            notes = list(entity.notes)
            created_at = entity.created_at
            if row:
                created_at = row[0]["created_at"]
                notes = _merge_str_lists(list(load_json(row[0]["notes_json"]) or []),
                                         entity.notes)
                existing_names = [r["name"] for r in self._query(
                    "SELECT name FROM entity_names WHERE entity_id = ? "
                    "ORDER BY position", (entity.entity_id,))]
                names = _merge_str_lists(existing_names, entity.names)
                existing_ext = [
                    ExternalIdentifier(namespace=r["namespace"],
                                        identifier=r["identifier"],
                                        source=r["source"])
                    for r in self._query(
                        "SELECT namespace, identifier, source FROM "
                        "entity_external_identifiers WHERE entity_id = ? "
                        "ORDER BY position", (entity.entity_id,))]
                external = list(existing_ext)
                for identifier in entity.external_identifiers:
                    if all(identifier.key() != e.key() for e in external):
                        external.append(identifier)
            else:
                names = _merge_str_lists([], entity.names)
                external = list(entity.external_identifiers)

            self._execute(
                """INSERT INTO entities (
                    entity_id, identity_key, identity_key_version, identity_basis,
                    facets_json, canonical_smiles, inchi, inchikey,
                    molecular_formula, molecular_weight, identity_source,
                    source_id, source_url, retrieved_at, provenance_json,
                    notes_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(entity_id) DO UPDATE SET
                    identity_key=excluded.identity_key,
                    identity_key_version=excluded.identity_key_version,
                    identity_basis=excluded.identity_basis,
                    facets_json=excluded.facets_json,
                    canonical_smiles=excluded.canonical_smiles,
                    inchi=excluded.inchi,
                    inchikey=excluded.inchikey,
                    molecular_formula=excluded.molecular_formula,
                    molecular_weight=excluded.molecular_weight,
                    identity_source=excluded.identity_source,
                    source_id=excluded.source_id,
                    source_url=excluded.source_url,
                    retrieved_at=excluded.retrieved_at,
                    provenance_json=excluded.provenance_json,
                    notes_json=excluded.notes_json""",
                (entity.entity_id, entity.identity_key,
                 entity.identity_key_version, entity.identity_basis.value,
                 _model_dump(entity.facets), entity.canonical_smiles,
                 entity.inchi, entity.inchikey, entity.molecular_formula,
                 entity.molecular_weight, entity.identity_source,
                 entity.source_id, entity.source_url, entity.retrieved_at,
                 _model_dump(entity.provenance) if entity.provenance else None,
                 dump_json(notes), created_at))
            self._execute(
                "DELETE FROM entity_names WHERE entity_id = ?", (entity.entity_id,))
            for position, name in enumerate(names):
                self._execute(
                    "INSERT INTO entity_names (entity_id, name, position) "
                    "VALUES (?,?,?)", (entity.entity_id, name, position))
            self._execute(
                "DELETE FROM entity_external_identifiers WHERE entity_id = ?",
                (entity.entity_id,))
            for position, identifier in enumerate(external):
                self._execute(
                    "INSERT INTO entity_external_identifiers "
                    "(entity_id, namespace, identifier, source, position) "
                    "VALUES (?,?,?,?,?)",
                    (entity.entity_id, identifier.namespace,
                     identifier.identifier, identifier.source, position))
            # The returned object reflects what is stored: a caller that
            # re-saved an older view gets the merged state back.
            entity.names = names
            entity.external_identifiers = external
            entity.notes = notes
            entity.created_at = created_at
            return entity

    def _entity_from_row(self, row) -> ChemicalEntity:
        entity_id = row["entity_id"]
        names = [r["name"] for r in self._query(
            "SELECT name FROM entity_names WHERE entity_id = ? ORDER BY position",
            (entity_id,))]
        external = [
            ExternalIdentifier(namespace=r["namespace"],
                                identifier=r["identifier"], source=r["source"])
            for r in self._query(
                "SELECT namespace, identifier, source FROM "
                "entity_external_identifiers WHERE entity_id = ? ORDER BY position",
                (entity_id,))]
        provenance_raw = load_json(row["provenance_json"])
        return ChemicalEntity(
            entity_id=entity_id,
            identity_key=row["identity_key"],
            identity_key_version=row["identity_key_version"],
            identity_basis=IdentityBasis(row["identity_basis"]),
            facets=IdentityFacets.model_validate(load_json(row["facets_json"]) or {}),
            canonical_smiles=row["canonical_smiles"],
            inchi=row["inchi"],
            inchikey=row["inchikey"],
            molecular_formula=row["molecular_formula"],
            molecular_weight=row["molecular_weight"],
            names=names,
            external_identifiers=external,
            identity_source=row["identity_source"],
            source_id=row["source_id"],
            source_url=row["source_url"],
            retrieved_at=row["retrieved_at"],
            provenance=(Provenance.model_validate(provenance_raw)
                        if provenance_raw else None),
            notes=list(load_json(row["notes_json"]) or []),
            created_at=row["created_at"],
        )

    def get_entity(self, entity_id: str) -> ChemicalEntity | None:
        rows = self._query("SELECT * FROM entities WHERE entity_id = ?",
                           (entity_id,))
        return self._entity_from_row(rows[0]) if rows else None

    def find_entity_by_identity_key(self, key: str) -> ChemicalEntity | None:
        rows = self._query("SELECT * FROM entities WHERE identity_key = ?",
                           (key,))
        return self._entity_from_row(rows[0]) if rows else None

    def find_entity_by_external_id(self, namespace: str,
                                   identifier: str) -> ChemicalEntity | None:
        rows = self._query(
            "SELECT e.* FROM entities e JOIN entity_external_identifiers x "
            "ON e.entity_id = x.entity_id WHERE x.namespace = ? AND x.identifier = ?",
            (namespace, identifier))
        return self._entity_from_row(rows[0]) if rows else None

    def find_entities_by_name(self, name: str) -> list[ChemicalEntity]:
        """Every entity that carries this name (§30/§31).

        Exact match, case-insensitive -- no substring guessing, and nothing
        is created. Several hits are all returned; nothing picks a first.
        """
        cleaned = (name or "").strip()
        if not cleaned:
            return []
        rows = self._query(
            "SELECT DISTINCT e.* FROM entities e JOIN entity_names n "
            "ON e.entity_id = n.entity_id WHERE lower(n.name) = lower(?) "
            "ORDER BY e.created_at, e.entity_id", (cleaned,))
        return [self._entity_from_row(r) for r in rows]

    def list_entities(self) -> list[ChemicalEntity]:
        rows = self._query(
            "SELECT * FROM entities ORDER BY created_at, entity_id")
        return [self._entity_from_row(r) for r in rows]

    def find_entity_by_inchikey(self, inchikey: str) -> ChemicalEntity | None:
        """The record whose Standard InChIKey matches (Stage 2.2, §9)."""
        if not (inchikey or "").strip():
            return None
        rows = self._query("SELECT * FROM entities WHERE inchikey = ?",
                           (inchikey,))
        return self._entity_from_row(rows[0]) if rows else None

    def count_entities(self) -> int:
        rows = self._query("SELECT COUNT(*) AS n FROM entities")
        return rows[0]["n"]

    def delete_entity(self, entity_id: str) -> bool:
        """Remove one molecule record (Stage 2.2 library). False when absent.

        Foreign keys stay enforced: deleting an entity that established
        reaction/mention knowledge is refused by the database, never silently
        cascaded. Names and external identifiers cascade with the record.
        """
        with self.transaction():
            cursor = self._execute(
                "DELETE FROM entities WHERE entity_id = ?", (entity_id,))
            return cursor.rowcount > 0

    # -- papers (§16/§17) ---------------------------------------------------

    def save_paper(self, paper: Paper) -> Paper:
        """Store a paper under its literature identity.

        The merge rules are the literature layer's (§94); what this method
        guards is that stored conflict information never shrinks: sources,
        raw hashes and merge conflicts accumulate across saves. DOI is the
        identity -- nothing here merges on title+year (§17).
        """
        with self.transaction():
            row = self._query(
                "SELECT sources_json, source_ids_json, raw_hashes_json, "
                "merge_conflicts_json FROM papers WHERE paper_id = ?",
                (paper.paper_id,))
            sources = list(paper.sources)
            source_ids = dict(paper.source_ids)
            raw_hashes = dict(paper.raw_hashes)
            merge_conflicts = list(paper.merge_conflicts)
            if row:
                sources = _merge_str_lists(
                    list(load_json(row[0]["sources_json"]) or []), sources)
                stored_ids = load_json(row[0]["source_ids_json"]) or {}
                source_ids = {**stored_ids, **source_ids}
                raw_hashes = {**(load_json(row[0]["raw_hashes_json"]) or {}),
                              **raw_hashes}
                merge_conflicts = _merge_str_lists(
                    list(load_json(row[0]["merge_conflicts_json"]) or []),
                    merge_conflicts)
            # The DOI column and identifiers_json must agree (§7): normalize
            # both to the canonical form, without mutating the caller's record.
            identifiers = paper.identifiers.model_copy(deep=True)
            identifiers.doi = normalize_doi(identifiers.doi)
            self._execute(
                """INSERT INTO papers (
                    paper_id, doi, title, journal, year, volume, issue, pages,
                    authors_json, abstracts_json, identifiers_json, access_json,
                    url, upstream_json, sources_json, source_ids_json,
                    retrieved_at, raw_hashes_json, merge_conflicts_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(paper_id) DO UPDATE SET
                    doi=excluded.doi, title=excluded.title,
                    journal=excluded.journal, year=excluded.year,
                    volume=excluded.volume, issue=excluded.issue,
                    pages=excluded.pages, authors_json=excluded.authors_json,
                    abstracts_json=excluded.abstracts_json,
                    identifiers_json=excluded.identifiers_json,
                    access_json=excluded.access_json, url=excluded.url,
                    upstream_json=excluded.upstream_json,
                    sources_json=excluded.sources_json,
                    source_ids_json=excluded.source_ids_json,
                    retrieved_at=excluded.retrieved_at,
                    raw_hashes_json=excluded.raw_hashes_json,
                    merge_conflicts_json=excluded.merge_conflicts_json""",
                (paper.paper_id, identifiers.doi, paper.title,
                 paper.journal, paper.year, paper.volume, paper.issue,
                 paper.pages, _model_dump(paper.authors),
                 _model_dump(paper.abstracts), _model_dump(identifiers),
                 _model_dump(paper.access), paper.url,
                 _model_dump(paper.upstream), dump_json(sources),
                 dump_json(source_ids), paper.retrieved_at,
                 dump_json(raw_hashes), dump_json(merge_conflicts)))
            paper.sources = sources
            paper.source_ids = source_ids
            paper.raw_hashes = raw_hashes
            paper.merge_conflicts = merge_conflicts
            paper.identifiers = identifiers
            return paper

    def _paper_from_row(self, row) -> Paper:
        return Paper(
            paper_id=row["paper_id"],
            title=row["title"],
            journal=row["journal"],
            year=row["year"],
            volume=row["volume"],
            issue=row["issue"],
            pages=row["pages"],
            authors=[a for a in (load_json(row["authors_json"]) or [])],
            abstracts=[a for a in (load_json(row["abstracts_json"]) or [])],
            identifiers=load_json(row["identifiers_json"]) or {},
            access=load_json(row["access_json"]) or {},
            url=row["url"],
            upstream=load_json(row["upstream_json"]) or {},
            sources=list(load_json(row["sources_json"]) or []),
            source_ids=load_json(row["source_ids_json"]) or {},
            retrieved_at=row["retrieved_at"],
            raw_hashes=load_json(row["raw_hashes_json"]) or {},
            merge_conflicts=list(load_json(row["merge_conflicts_json"]) or []),
        )

    def get_paper(self, paper_id: str) -> Paper | None:
        rows = self._query("SELECT * FROM papers WHERE paper_id = ?", (paper_id,))
        return self._paper_from_row(rows[0]) if rows else None

    def find_paper_by_doi(self, doi: str) -> Paper | None:
        """The record for a normalized DOI (§7/§9). Stored DOIs are
        normalized, so the lookup normalizes the query and matches exactly."""
        cleaned = normalize_doi(doi)
        if not cleaned:
            return None
        rows = self._query("SELECT * FROM papers WHERE doi = ?", (cleaned,))
        return self._paper_from_row(rows[0]) if rows else None

    def list_papers(self) -> list[Paper]:
        rows = self._query("SELECT * FROM papers ORDER BY paper_id")
        return [self._paper_from_row(r) for r in rows]

    # -- evidence (§18/§19/§48) --------------------------------------------

    def save_evidence(self, evidence: Evidence) -> Evidence:
        """Store evidence, or return the identical evidence already stored.

        `evidence_id` is the content hash of the citation itself, so a
        second write of the same id IS the same evidence (§19). Stored
        evidence is never rewritten (§48): the row that exists is returned
        as it stands, whatever the caller brought.
        """
        with self.transaction():
            existing = self.get_evidence(evidence.evidence_id)
            if existing is not None:
                return existing
            self._execute(
                """INSERT INTO evidence (
                    evidence_id, evidence_type, strength, source, source_id,
                    source_url, retrieved_at, raw_quote, locator_json,
                    provenance_json, paper_id, doi
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (evidence.evidence_id, evidence.evidence_type.value,
                 evidence.strength.value, evidence.source, evidence.source_id,
                 evidence.source_url, evidence.retrieved_at, evidence.raw_quote,
                 _model_dump(evidence.locator),
                 _model_dump(evidence.provenance) if evidence.provenance else None,
                 evidence.paper_id, evidence.doi))
            return evidence

    def _evidence_from_row(self, row) -> Evidence:
        provenance_raw = load_json(row["provenance_json"])
        return Evidence(
            evidence_id=row["evidence_id"],
            evidence_type=row["evidence_type"],
            strength=row["strength"],
            source=row["source"],
            source_id=row["source_id"],
            source_url=row["source_url"],
            retrieved_at=row["retrieved_at"],
            raw_quote=row["raw_quote"],
            locator=Locator.model_validate(load_json(row["locator_json"]) or {}),
            provenance=(Provenance.model_validate(provenance_raw)
                        if provenance_raw else None),
            paper_id=row["paper_id"],
            doi=row["doi"],
        )

    def get_evidence(self, evidence_id: str) -> Evidence | None:
        rows = self._query("SELECT * FROM evidence WHERE evidence_id = ?",
                           (evidence_id,))
        return self._evidence_from_row(rows[0]) if rows else None

    def list_evidence(self) -> list[Evidence]:
        rows = self._query("SELECT * FROM evidence ORDER BY evidence_id")
        return [self._evidence_from_row(r) for r in rows]

    def evidence_for_reaction(self, reaction_id: str) -> list[Evidence]:
        """The evidence actually standing behind a reaction (§67).

        Only real, resolvable citations come back. A ref that does not
        resolve is not evidence, and this join cannot return it.
        """
        rows = self._query(
            "SELECT e.* FROM evidence e JOIN reaction_evidence re "
            "ON e.evidence_id = re.evidence_id WHERE re.reaction_id = ? "
            "ORDER BY re.position", (reaction_id,))
        return [self._evidence_from_row(r) for r in rows]

    # -- mentions (§20/§21) -------------------------------------------------

    def save_mention(self, mention: ChemicalMention) -> ChemicalMention:
        """Store one occurrence, in its scope. Never merged by text (§20)."""
        with self.transaction():
            self._execute(
                """INSERT INTO mentions (
                    mention_id, source, source_id, raw_text, normalized_text,
                    entity_id, resolution_status, locator_json,
                    evidence_refs_json, explicit_smiles, explicit_inchi,
                    context_before, context_after
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(mention_id) DO UPDATE SET
                    source=excluded.source, source_id=excluded.source_id,
                    raw_text=excluded.raw_text,
                    normalized_text=excluded.normalized_text,
                    entity_id=excluded.entity_id,
                    resolution_status=excluded.resolution_status,
                    locator_json=excluded.locator_json,
                    evidence_refs_json=excluded.evidence_refs_json,
                    explicit_smiles=excluded.explicit_smiles,
                    explicit_inchi=excluded.explicit_inchi,
                    context_before=excluded.context_before,
                    context_after=excluded.context_after""",
                (mention.mention_id, mention.source, mention.source_id,
                 mention.raw_text, mention.normalized_text, mention.entity_id,
                 mention.resolution_status.value,
                 _model_dump(mention.locator) if mention.locator else None,
                 dump_json(list(mention.evidence_refs)),
                 mention.explicit_smiles, mention.explicit_inchi,
                 mention.context_before, mention.context_after))
            return mention

    def _mention_from_row(self, row) -> ChemicalMention:
        locator_raw = load_json(row["locator_json"])
        return ChemicalMention(
            mention_id=row["mention_id"],
            text=row["raw_text"],
            normalized_text=row["normalized_text"],
            entity_id=row["entity_id"],
            resolution_status=row["resolution_status"],
            source=row["source"],
            source_id=row["source_id"],
            locator=Locator.model_validate(locator_raw) if locator_raw else None,
            evidence_refs=list(load_json(row["evidence_refs_json"]) or []),
            explicit_smiles=row["explicit_smiles"],
            explicit_inchi=row["explicit_inchi"],
            context_before=row["context_before"],
            context_after=row["context_after"],
        )

    def get_mention(self, mention_id: str) -> ChemicalMention | None:
        rows = self._query("SELECT * FROM mentions WHERE mention_id = ?",
                           (mention_id,))
        return self._mention_from_row(rows[0]) if rows else None

    def list_mentions(self) -> list[ChemicalMention]:
        rows = self._query("SELECT * FROM mentions ORDER BY mention_id")
        return [self._mention_from_row(r) for r in rows]

    def link_paper_mention(self, paper_id: str, mention_id: str) -> None:
        """Paper -> ChemicalMention, as a relation (§65)."""
        self._execute(
            "INSERT OR IGNORE INTO paper_mentions (paper_id, mention_id) "
            "VALUES (?,?)", (paper_id, mention_id))

    def mentions_for_paper(self, paper_id: str) -> list[ChemicalMention]:
        rows = self._query(
            "SELECT m.* FROM mentions m JOIN paper_mentions pm "
            "ON m.mention_id = pm.mention_id WHERE pm.paper_id = ? "
            "ORDER BY m.mention_id", (paper_id,))
        return [self._mention_from_row(r) for r in rows]

    def papers_for_entity(self, entity_id: str) -> list[Paper]:
        """Paper -> Mention -> Entity (§65): through the mention layer.

        Never a `paper.entity_ids` shortcut -- the relation is the one the
        architecture has, and a paper whose mentions never resolved to this
        entity is not a paper about it.
        """
        rows = self._query(
            "SELECT DISTINCT p.* FROM papers p "
            "JOIN paper_mentions pm ON p.paper_id = pm.paper_id "
            "JOIN mentions m ON pm.mention_id = m.mention_id "
            "WHERE m.entity_id = ? ORDER BY p.paper_id", (entity_id,))
        return [self._paper_from_row(r) for r in rows]

    # -- reactions (§13-§15) ------------------------------------------------

    def save_reaction(self, reaction: Reaction) -> Reaction:
        """Store a reaction and everything it points at, in one unit (§51).

        The row, its components, their mentions and the resolvable evidence
        citations land together or not at all. Re-saving the same reaction
        follows the registry's merge semantics (§93): citations and notes
        accumulate, a CANDIDATE status may be raised by a later view and
        nothing else moves, components follow the incoming view (they are
        derived state, and a later resolution may know more).

        Every `evidence_refs` entry must resolve -- the citation relation is
        FK-enforced (§89). A ref that cannot resolve belongs in
        `unresolved_evidence_refs`, where the builder already puts it.
        """
        with self.transaction():
            existing_row = self._query(
                "SELECT evidence_refs_json, unresolved_evidence_refs_json, "
                "notes_json, status, created_at FROM reactions WHERE reaction_id = ?",
                (reaction.reaction_id,))
            evidence_refs = list(reaction.evidence_refs)
            unresolved = list(reaction.unresolved_evidence_refs)
            notes = list(reaction.notes)
            status = reaction.status
            created_at = reaction.created_at
            if existing_row:
                created_at = existing_row[0]["created_at"]
                evidence_refs = _merge_str_lists(
                    list(load_json(existing_row[0]["evidence_refs_json"]) or []),
                    evidence_refs)
                unresolved = _merge_str_lists(
                    list(load_json(existing_row[0]["unresolved_evidence_refs_json"]) or []),
                    unresolved)
                notes = _merge_str_lists(
                    list(load_json(existing_row[0]["notes_json"]) or []), notes)
                stored_status = ReactionStatus(existing_row[0]["status"])
                if (stored_status is not ReactionStatus.CANDIDATE
                        and status is ReactionStatus.CANDIDATE):
                    status = stored_status

            # component mentions first: the FK points up at them
            for component in reaction.components():
                if component.mention is not None:
                    self.save_mention(component.mention)

            self._execute(
                """INSERT INTO reactions (
                    reaction_id, reaction_key, reaction_key_version,
                    key_is_identity_based, status, record_id, paper_id, doi,
                    source_id, document_id, paper_scope, evidence_refs_json,
                    unresolved_evidence_refs_json, locator_json,
                    provenance_json, notes_json, created_at,
                    reaction_class, atom_mapping_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(reaction_id) DO UPDATE SET
                    reaction_key=excluded.reaction_key,
                    reaction_key_version=excluded.reaction_key_version,
                    key_is_identity_based=excluded.key_is_identity_based,
                    status=excluded.status, record_id=excluded.record_id,
                    paper_id=excluded.paper_id, doi=excluded.doi,
                    source_id=excluded.source_id,
                    document_id=excluded.document_id,
                    paper_scope=excluded.paper_scope,
                    evidence_refs_json=excluded.evidence_refs_json,
                    unresolved_evidence_refs_json=excluded.unresolved_evidence_refs_json,
                    locator_json=excluded.locator_json,
                    provenance_json=excluded.provenance_json,
                    notes_json=excluded.notes_json,
                    reaction_class=excluded.reaction_class,
                    atom_mapping_json=excluded.atom_mapping_json""",
                (reaction.reaction_id, reaction.reaction_key,
                 reaction.reaction_key_version,
                 1 if key_is_identity_based(reaction.reaction_key) else 0,
                 status.value, reaction.record_id, reaction.paper_id, reaction.doi,
                 reaction.source_id, reaction.document_id, reaction.paper_scope(),
                 dump_json(evidence_refs), dump_json(unresolved),
                 _model_dump(reaction.locator) if reaction.locator else None,
                 _model_dump(reaction.provenance) if reaction.provenance else None,
                 dump_json(notes), created_at,
                 reaction.reaction_class,
                 dump_json(reaction.atom_mapping) if reaction.atom_mapping
                 else None))

            self._execute(
                "DELETE FROM reaction_components WHERE reaction_id = ?",
                (reaction.reaction_id,))
            for bucket, _role in _BUCKET_ORDER:
                components = getattr(reaction, bucket)
                for position, component in enumerate(components):
                    self._execute(
                        """INSERT INTO reaction_components (
                            reaction_id, bucket, position, role, name,
                            mention_id, entity_id, identity_key, canonical_smiles
                        ) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (reaction.reaction_id, bucket, position,
                         component.role.value, component.name,
                         component.mention.mention_id if component.mention else None,
                         component.entity_id, component.identity_key,
                         component.canonical_smiles))

            self._execute(
                "DELETE FROM reaction_evidence WHERE reaction_id = ?",
                (reaction.reaction_id,))
            for position, ref in enumerate(evidence_refs):
                # FK-enforced: a dangling citation rolls the whole save back.
                self._execute(
                    "INSERT INTO reaction_evidence (reaction_id, evidence_id, "
                    "position) VALUES (?,?,?)",
                    (reaction.reaction_id, ref, position))

            reaction.evidence_refs = evidence_refs
            reaction.unresolved_evidence_refs = unresolved
            reaction.notes = notes
            reaction.status = status
            reaction.created_at = created_at
            return reaction

    def _reaction_from_row(self, row) -> Reaction:
        reaction_id = row["reaction_id"]
        mentions: dict[str, ChemicalMention] = {}
        buckets: dict[str, list[ReactionComponent]] = {
            name: [] for name, _ in _BUCKET_ORDER}
        for component_row in self._query(
                "SELECT * FROM reaction_components WHERE reaction_id = ? "
                "ORDER BY component_pk", (reaction_id,)):
            mention = None
            mention_id = component_row["mention_id"]
            if mention_id:
                if mention_id not in mentions:
                    mentions[mention_id] = self.get_mention(mention_id)
                mention = mentions[mention_id]
            buckets[component_row["bucket"]].append(ReactionComponent(
                role=ComponentRole(component_row["role"]),
                name=component_row["name"],
                mention=mention,
                entity_id=component_row["entity_id"],
                identity_key=component_row["identity_key"],
                canonical_smiles=component_row["canonical_smiles"]))
        provenance_raw = load_json(row["provenance_json"])
        locator_raw = load_json(row["locator_json"])
        return Reaction(
            reaction_id=reaction_id,
            reaction_key=row["reaction_key"],
            reaction_key_version=row["reaction_key_version"],
            reactants=buckets["reactants"],
            products=buckets["products"],
            reagents=buckets["reagents"],
            catalysts=buckets["catalysts"],
            solvents=buckets["solvents"],
            unassigned=buckets["unassigned"],
            status=ReactionStatus(row["status"]),
            record_id=row["record_id"],
            paper_id=row["paper_id"],
            doi=row["doi"],
            source_id=row["source_id"],
            document_id=row["document_id"],
            evidence_refs=list(load_json(row["evidence_refs_json"]) or []),
            unresolved_evidence_refs=list(
                load_json(row["unresolved_evidence_refs_json"]) or []),
            locator=Locator.model_validate(locator_raw) if locator_raw else None,
            provenance=(Provenance.model_validate(provenance_raw)
                        if provenance_raw else None),
            notes=list(load_json(row["notes_json"]) or []),
            created_at=row["created_at"],
            reaction_class=row["reaction_class"] or "",
            atom_mapping=load_json(row["atom_mapping_json"]),
        )

    def get_reaction(self, reaction_id: str) -> Reaction | None:
        rows = self._query("SELECT * FROM reactions WHERE reaction_id = ?",
                           (reaction_id,))
        return self._reaction_from_row(rows[0]) if rows else None

    def find_reaction_by_scope_key(self, paper_scope: str,
                                   reaction_key: str) -> Reaction | None:
        rows = self._query(
            "SELECT * FROM reactions WHERE paper_scope = ? AND reaction_key = ?",
            (paper_scope, reaction_key))
        return self._reaction_from_row(rows[0]) if rows else None

    def find_reactions_by_paper(self, paper_id: str) -> list[Reaction]:
        rows = self._query(
            "SELECT * FROM reactions WHERE paper_id = ? "
            "ORDER BY created_at, reaction_id", (paper_id,))
        return [self._reaction_from_row(r) for r in rows]

    def find_reactions_by_doi(self, doi: str) -> list[Reaction]:
        rows = self._query(
            "SELECT * FROM reactions WHERE doi = ? "
            "ORDER BY created_at, reaction_id", (doi,))
        return [self._reaction_from_row(r) for r in rows]

    def find_reactions_by_component_entity(self, entity_id: str) -> list[Reaction]:
        rows = self._query(
            "SELECT DISTINCT r.* FROM reactions r "
            "JOIN reaction_components c ON r.reaction_id = c.reaction_id "
            "WHERE c.entity_id = ? ORDER BY r.created_at, r.reaction_id",
            (entity_id,))
        return [self._reaction_from_row(r) for r in rows]

    def find_reactions_by_product_entity(self, entity_id: str) -> list[Reaction]:
        """Reactions that make this entity (Stage D §11)."""
        rows = self._query(
            "SELECT DISTINCT r.* FROM reactions r "
            "JOIN reaction_components c ON r.reaction_id = c.reaction_id "
            "WHERE c.entity_id = ? AND c.bucket = 'products' "
            "ORDER BY r.created_at, r.reaction_id", (entity_id,))
        return [self._reaction_from_row(r) for r in rows]

    def find_reactions_by_reactant_entity(self, entity_id: str) -> list[Reaction]:
        """Reactions that consume this entity (Stage D §11)."""
        rows = self._query(
            "SELECT DISTINCT r.* FROM reactions r "
            "JOIN reaction_components c ON r.reaction_id = c.reaction_id "
            "WHERE c.entity_id = ? AND c.bucket = 'reactants' "
            "ORDER BY r.created_at, r.reaction_id", (entity_id,))
        return [self._reaction_from_row(r) for r in rows]

    def find_reactions_between(self, reactant_ids: list[str],
                               product_ids: list[str]) -> list[Reaction]:
        """Subset semantics: every named reactant is consumed and every
        named product made, in ONE reaction (n:m co-occurrence preserved,
        never decomposed into edges, Stage D §4)."""
        out = []
        for reaction in self.list_reactions():
            reactants = {c.entity_id for c in reaction.reactants if c.entity_id}
            products = {c.entity_id for c in reaction.products if c.entity_id}
            if set(reactant_ids) <= reactants and set(product_ids) <= products:
                out.append(reaction)
        return out

    def list_reactions(self) -> list[Reaction]:
        rows = self._query(
            "SELECT * FROM reactions ORDER BY created_at, reaction_id")
        return [self._reaction_from_row(r) for r in rows]

    # -- routes (§22/§23) ---------------------------------------------------

    def save_route(self, route: CandidateRoute) -> CandidateRoute:
        with self.transaction():
            self._execute(
                """INSERT INTO routes (
                    route_id, target_json, score, score_source, source,
                    provider_version, stock_configuration, evidence_refs_json,
                    warnings_json
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(route_id) DO UPDATE SET
                    target_json=excluded.target_json, score=excluded.score,
                    score_source=excluded.score_source, source=excluded.source,
                    provider_version=excluded.provider_version,
                    stock_configuration=excluded.stock_configuration,
                    evidence_refs_json=excluded.evidence_refs_json,
                    warnings_json=excluded.warnings_json""",
                (route.route_id, _model_dump(route.target), route.score,
                 route.score_source.value, route.source, route.provider_version,
                 route.stock_configuration, dump_json(list(route.evidence_refs)),
                 dump_json(list(route.warnings))))
            self._execute("DELETE FROM route_steps WHERE route_id = ?",
                          (route.route_id,))
            for step in route.steps:
                self._execute(
                    """INSERT INTO route_steps (
                        step_id, route_id, step_index, products_json,
                        reactants_json, reagents_json, transformation_json,
                        source, score, score_source, stock_assumptions_json,
                        status, support_level, evidence_refs_json,
                        paper_ids_json, reaction_record_ids_json, warnings_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (step.step_id, route.route_id, step.step_index,
                     _model_dump(step.products), _model_dump(step.reactants),
                     _model_dump(step.reagents),
                     _model_dump(step.transformation) if step.transformation else None,
                     step.source, step.score, step.score_source.value,
                     dump_json({k: v.value for k, v in
                                step.stock_assumptions.items()}),
                     step.status.value, step.support_level.value,
                     dump_json(list(step.evidence_refs)),
                     dump_json(list(step.paper_ids)),
                     dump_json(list(step.reaction_record_ids)),
                     dump_json(list(step.warnings))))
            return route

    def _step_from_row(self, row) -> ReactionStep:
        return ReactionStep(
            step_id=row["step_id"],
            step_index=row["step_index"],
            products=[RouteMolecule.model_validate(m)
                      for m in (load_json(row["products_json"]) or [])],
            reactants=[RouteMolecule.model_validate(m)
                       for m in (load_json(row["reactants_json"]) or [])],
            reagents=[RouteMolecule.model_validate(m)
                      for m in (load_json(row["reagents_json"]) or [])],
            transformation=(TransformationCandidate.model_validate(
                load_json(row["transformation_json"]))
                if load_json(row["transformation_json"]) else None),
            source=row["source"],
            score=row["score"],
            score_source=ScoreSource(row["score_source"]),
            stock_assumptions={
                k: StockAssumption(v) for k, v in
                (load_json(row["stock_assumptions_json"]) or {}).items()},
            status=StepStatus(row["status"]),
            support_level=SupportLevel(row["support_level"]),
            evidence_refs=list(load_json(row["evidence_refs_json"]) or []),
            paper_ids=list(load_json(row["paper_ids_json"]) or []),
            reaction_record_ids=list(
                load_json(row["reaction_record_ids_json"]) or []),
            warnings=list(load_json(row["warnings_json"]) or []),
        )

    def _route_from_row(self, row) -> CandidateRoute:
        route_id = row["route_id"]
        step_rows = self._query(
            "SELECT * FROM route_steps WHERE route_id = ? ORDER BY step_index, step_id",
            (route_id,))
        return CandidateRoute(
            route_id=route_id,
            target=RouteMolecule.model_validate(load_json(row["target_json"])),
            steps=[self._step_from_row(s) for s in step_rows],
            score=row["score"],
            score_source=ScoreSource(row["score_source"]),
            source=row["source"],
            provider_version=row["provider_version"],
            stock_configuration=row["stock_configuration"],
            evidence_refs=list(load_json(row["evidence_refs_json"]) or []),
            warnings=list(load_json(row["warnings_json"]) or []),
        )

    def get_route(self, route_id: str) -> CandidateRoute | None:
        rows = self._query("SELECT * FROM routes WHERE route_id = ?", (route_id,))
        return self._route_from_row(rows[0]) if rows else None

    def list_routes(self) -> list[CandidateRoute]:
        rows = self._query("SELECT * FROM routes ORDER BY route_id")
        return [self._route_from_row(r) for r in rows]

    def save_route_reaction_link(self, link: RouteStepReactionLink,
                                 *, route_id: str | None = None
                                 ) -> RouteStepReactionLink:
        """Store a step<->reaction link (§23). The route is derived from the
        step when the caller does not name it: a link always belongs to a
        route's step, and the table keeps the full triple."""
        with self.transaction():
            resolved_route = route_id
            if resolved_route is None:
                rows = self._query(
                    "SELECT route_id FROM route_steps WHERE step_id = ?",
                    (link.step_id,))
                if not rows:
                    raise NotFound(
                        f"cannot link: step {link.step_id!r} is not stored")
                resolved_route = rows[0]["route_id"]
            self._execute(
                """INSERT INTO route_reaction_links (
                    link_id, route_id, step_id, reaction_id, match_type,
                    link_basis, screening_only, source_id, doi, notes_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(link_id) DO UPDATE SET
                    route_id=excluded.route_id, step_id=excluded.step_id,
                    reaction_id=excluded.reaction_id,
                    match_type=excluded.match_type,
                    link_basis=excluded.link_basis,
                    screening_only=excluded.screening_only,
                    source_id=excluded.source_id, doi=excluded.doi,
                    notes_json=excluded.notes_json""",
                (link.link_id, resolved_route, link.step_id, link.reaction_id,
                 link.match_type.value, link.link_basis.value,
                 1 if link.screening_only else 0, link.source_id, link.doi,
                 dump_json(list(link.notes))))
            return link

    def _link_from_row(self, row) -> RouteStepReactionLink:
        return RouteStepReactionLink(
            link_id=row["link_id"],
            step_id=row["step_id"],
            reaction_id=row["reaction_id"],
            match_type=ReactionMatchType(row["match_type"]),
            link_basis=row["link_basis"],
            screening_only=bool(row["screening_only"]),
            source_id=row["source_id"],
            doi=row["doi"],
            notes=list(load_json(row["notes_json"]) or []),
        )

    def links_for_reaction(self, reaction_id: str
                           ) -> list[tuple[str, RouteStepReactionLink]]:
        """(route_id, link) for every stored link to this reaction (§68).

        Only comparisons that were actually recorded come back. Nothing is
        generated on the way out -- a structure match made at query time is
        not a link somebody measured.
        """
        rows = self._query(
            "SELECT * FROM route_reaction_links WHERE reaction_id = ? "
            "ORDER BY route_id, step_id, link_id", (reaction_id,))
        return [(r["route_id"], self._link_from_row(r)) for r in rows]

    def links_for_step(self, step_id: str) -> list[RouteStepReactionLink]:
        rows = self._query(
            "SELECT * FROM route_reaction_links WHERE step_id = ? "
            "ORDER BY link_id", (step_id,))
        return [self._link_from_row(r) for r in rows]

    def links_for_route(self, route_id: str) -> list[RouteStepReactionLink]:
        rows = self._query(
            "SELECT * FROM route_reaction_links WHERE route_id = ? "
            "ORDER BY step_id, link_id", (route_id,))
        return [self._link_from_row(r) for r in rows]

    # -- reaction records (Phase 3.12, §25-§28) -----------------------------

    def save_reaction_record(self, record: ReactionRecord, *,
                             reaction_id: str) -> ReactionRecord:
        """Store one procedure record as the experimental basis of a reaction.

        The record is the intermediate extraction result; the reaction is
        the stable knowledge object -- this row is the chain between them
        (§25). Conditions are stored as the record holds them: `raw_value`
        and `raw_unit` exactly as the paper wrote them, `value`/`unit` as
        the policy validated them, and **no unit conversion anywhere**
        (§27). Missing fields stay NOT_FOUND in the stored record (§27).
        """
        with self.transaction():
            self._execute(
                """INSERT INTO reaction_records (
                    record_id, reaction_id, source, document_id, paper_id,
                    reaction_description, description_evidence_refs_json,
                    conditions_json, evidence_refs_json,
                    raw_experimental_text, conflicts_json, rejected_json,
                    extractor, policy_version, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(record_id) DO UPDATE SET
                    reaction_id=excluded.reaction_id,
                    source=excluded.source,
                    document_id=excluded.document_id,
                    paper_id=excluded.paper_id,
                    reaction_description=excluded.reaction_description,
                    description_evidence_refs_json=excluded.description_evidence_refs_json,
                    conditions_json=excluded.conditions_json,
                    evidence_refs_json=excluded.evidence_refs_json,
                    raw_experimental_text=excluded.raw_experimental_text,
                    conflicts_json=excluded.conflicts_json,
                    rejected_json=excluded.rejected_json,
                    extractor=excluded.extractor,
                    policy_version=excluded.policy_version""",
                (record.reaction_id, reaction_id, record.source,
                 record.document_id, record.paper_id,
                 record.reaction_description,
                 dump_json(list(record.description_evidence_refs)),
                 _model_dump(record.conditions),
                 dump_json(list(record.evidence_refs)),
                 record.raw_experimental_text,
                 _model_dump(record.conflicts),
                 _model_dump(record.rejected),
                 record.extractor, record.policy_version,
                 utc_now_iso()))
            return record

    def _record_from_row(self, row) -> ReactionRecord:
        return ReactionRecord(
            reaction_id=row["record_id"],
            source=row["source"],
            document_id=row["document_id"],
            paper_id=row["paper_id"],
            reaction_description=row["reaction_description"],
            description_evidence_refs=list(
                load_json(row["description_evidence_refs_json"]) or []),
            conditions=ReactionConditions.model_validate(
                load_json(row["conditions_json"]) or {}),
            evidence_refs=list(load_json(row["evidence_refs_json"]) or []),
            raw_experimental_text=row["raw_experimental_text"],
            conflicts=[c for c in (load_json(row["conflicts_json"]) or [])],
            rejected=[r for r in (load_json(row["rejected_json"]) or [])],
            extractor=row["extractor"],
            policy_version=row["policy_version"],
        )

    def get_reaction_records(self, reaction_id: str) -> list[ReactionRecord]:
        """The experimental basis of a reaction: which record, what it said
        (§28). Records without a reaction link never appear here."""
        rows = self._query(
            "SELECT * FROM reaction_records WHERE reaction_id = ? "
            "ORDER BY record_id", (reaction_id,))
        return [self._record_from_row(r) for r in rows]

    def get_reaction_record_by_id(self, record_id: str) -> ReactionRecord | None:
        rows = self._query(
            "SELECT * FROM reaction_records WHERE record_id = ?", (record_id,))
        return self._record_from_row(rows[0]) if rows else None

    def list_reaction_records(self) -> list[ReactionRecord]:
        rows = self._query(
            "SELECT * FROM reaction_records ORDER BY created_at, record_id")
        return [self._record_from_row(r) for r in rows]

    def reaction_record_links(self) -> list[tuple[str, str]]:
        """(record_id, reaction_id) pairs -- the record chain (§25/§28).

        The link lives on the stored row (a `ReactionRecord` is the
        extraction result and carries only its own id); this is how a
        caller reads the chain both ways.
        """
        rows = self._query(
            "SELECT record_id, reaction_id FROM reaction_records "
            "ORDER BY record_id")
        return [(r["record_id"], r["reaction_id"]) for r in rows]

    def routes_for_entity(self, entity_id: str) -> list[dict]:
        """Which routes involve this entity, and in what role (§69).

        Each hit says `route_id`, `relation_role` and `step_id` -- the
        context, never dropped. The roles are the ones §69 names: `target`
        (the route's goal), `reactant`/`product` (a step's sides), and
        `participant` (everything else this entity does in the route --
        reagents, and the components of reactions linked to its steps).

        The structure comparison is string identity on the identity layer's
        own canonical SMILES -- two mentions of one identity share it, and
        this module performs no chemistry (§33). A route step linked to a
        reaction brings that reaction's component entities in as
        participants: the link was measured, so the participation is real.
        """
        entity = self.get_entity(entity_id)
        smiles = (entity.canonical_smiles if entity else "") or ""
        hits: list[dict] = []

        def same(value: str) -> bool:
            return bool(smiles) and bool(value) and value == smiles

        for route in self.list_routes():
            if same(route.target.canonical_smiles):
                hits.append({"route_id": route.route_id,
                             "relation_role": "target", "step_id": None})
            for step in route.steps:
                if any(same(m.canonical_smiles) for m in step.reactants):
                    hits.append({"route_id": route.route_id,
                                 "relation_role": "reactant",
                                 "step_id": step.step_id})
                if any(same(m.canonical_smiles) for m in step.products):
                    hits.append({"route_id": route.route_id,
                                 "relation_role": "product",
                                 "step_id": step.step_id})
                if any(same(m.canonical_smiles) for m in step.reagents):
                    hits.append({"route_id": route.route_id,
                                 "relation_role": "participant",
                                 "step_id": step.step_id})
                for link in self.links_for_step(step.step_id):
                    reaction = self.get_reaction(link.reaction_id)
                    if reaction is None:
                        continue
                    if any(c.entity_id == entity_id for c in reaction.components()):
                        if not any(h["step_id"] == step.step_id
                                   and h["route_id"] == route.route_id
                                   for h in hits):
                            hits.append({"route_id": route.route_id,
                                         "relation_role": "participant",
                                         "step_id": step.step_id})
        hits.sort(key=lambda h: (h["route_id"], h["relation_role"],
                                 h["step_id"] or ""))
        return hits

    # -- sessions (§24/§25) -------------------------------------------------

    def save_session(self, session: ResearchSession) -> ResearchSession:
        """Store one research run's transcript, and never overwrite another.

        The object id lists live in relation tables (§25): the session
        *points at* the knowledge this run used and produced, it does not
        copy it. The task's report and evidence lists are deliberately not
        stored -- they are renderings and copies of objects the tables
        already hold (§25/§106), and a session that embedded them would be
        a second, drifting database inside the first.
        """
        with self.transaction():
            task = session.task.model_dump(mode="json")
            task.pop("report", None)
            task.pop("evidence", None)
            self._execute(
                """INSERT INTO research_sessions (
                    session_id, task_json, created_at, status, phases_json,
                    findings_json, sources_json
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                    task_json=excluded.task_json,
                    status=excluded.status, phases_json=excluded.phases_json,
                    findings_json=excluded.findings_json,
                    sources_json=excluded.sources_json""",
                (session.session_id, dump_json(task), session.created_at,
                 session.status.value, dump_json(list(session.phases)),
                 _model_dump(session.findings), _model_dump(session.sources)))
            self._save_session_links(session)
            return session

    def _save_session_links(self, session: ResearchSession) -> None:
        tables = (
            ("research_session_entities", "entity_id",
             list(dict.fromkeys(session.chemical_entity_ids))),
            ("research_session_mentions", "mention_id",
             list(dict.fromkeys(session.chemical_mention_ids))),
            ("research_session_reactions", "reaction_id",
             list(dict.fromkeys(session.reaction_ids))),
            ("research_session_evidence", "evidence_id",
             list(dict.fromkeys(session.evidence_ids))),
            ("research_session_routes", "route_id",
             list(dict.fromkeys(session.route_ids))),
        )
        for table, column, ids in tables:
            self._execute(f"DELETE FROM {table} WHERE session_id = ?",
                          (session.session_id,))
            for position, object_id in enumerate(ids):
                # FK-enforced (§89): a session may only point at knowledge
                # that exists, which is exactly what "this run used it" means.
                self._execute(
                    f"INSERT INTO {table} (session_id, {column}, position) "
                    f"VALUES (?,?,?)", (session.session_id, object_id, position))
        # Papers come from the session's sources; a source that never became
        # a stored paper stays a source, not a phantom row (§25).
        self._execute(
            "DELETE FROM research_session_papers WHERE session_id = ?",
            (session.session_id,))
        position = 0
        for source in session.sources:
            if source.kind != "paper":
                continue
            paper = self.get_paper(source.identifier)
            if paper is None:
                paper = self.find_paper_by_doi(source.identifier or "")
            if paper is None:
                continue
            self._execute(
                "INSERT OR IGNORE INTO research_session_papers "
                "(session_id, paper_id, position) VALUES (?,?,?)",
                (session.session_id, paper.paper_id, position))
            position += 1

    def _session_ids(self, table: str, column: str,
                     session_id: str) -> list[str]:
        return [r[column] for r in self._query(
            f"SELECT {column} FROM {table} WHERE session_id = ? "
            f"ORDER BY position", (session_id,))]

    def get_session(self, session_id: str) -> ResearchSession | None:
        rows = self._query("SELECT * FROM research_sessions WHERE session_id = ?",
                           (session_id,))
        if not rows:
            return None
        row = rows[0]
        task_raw = load_json(row["task_json"]) or {}
        return ResearchSession(
            session_id=row["session_id"],
            task=task_raw,
            created_at=row["created_at"],
            status=WorkflowStatus(row["status"]),
            phases=list(load_json(row["phases_json"]) or []),
            findings=[f for f in (load_json(row["findings_json"]) or [])],
            sources=[s for s in (load_json(row["sources_json"]) or [])],
            evidence_ids=self._session_ids(
                "research_session_evidence", "evidence_id", session_id),
            route_ids=self._session_ids(
                "research_session_routes", "route_id", session_id),
            reaction_ids=self._session_ids(
                "research_session_reactions", "reaction_id", session_id),
            chemical_entity_ids=self._session_ids(
                "research_session_entities", "entity_id", session_id),
            chemical_mention_ids=self._session_ids(
                "research_session_mentions", "mention_id", session_id),
        )

    def list_sessions(self) -> list[ResearchSession]:
        rows = self._query(
            "SELECT session_id FROM research_sessions "
            "ORDER BY created_at, session_id")
        out = []
        for row in rows:
            session = self.get_session(row["session_id"])
            if session is not None:
                out.append(session)
        return out

    # -- chemistry workspaces (Stage F §4/§20) ------------------------------

    def save_workspace(self, workspace: Workspace) -> Workspace:
        self._execute(
            "INSERT INTO workspaces (workspace_id, title, created_at) "
            "VALUES (?, ?, ?)",
            (workspace.workspace_id, workspace.title, workspace.created_at))
        return workspace

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        rows = self._query(
            "SELECT workspace_id, title, created_at FROM workspaces "
            "WHERE workspace_id = ?", (workspace_id,))
        if not rows:
            return None
        row = rows[0]
        return Workspace(workspace_id=row["workspace_id"],
                         title=row["title"], created_at=row["created_at"])

    def list_workspaces(self) -> list[Workspace]:
        rows = self._query(
            "SELECT workspace_id, title, created_at FROM workspaces "
            "ORDER BY created_at, workspace_id")
        return [Workspace(workspace_id=r["workspace_id"], title=r["title"],
                          created_at=r["created_at"]) for r in rows]

    def append_message(self, message: WorkspaceMessage) -> WorkspaceMessage:
        """Append one message; the store assigns the position (§20)."""
        with self.transaction():
            row = self._query(
                "SELECT COALESCE(MAX(position), 0) + 1 AS next "
                "FROM workspace_messages WHERE workspace_id = ?",
                (message.workspace_id,))
            position = row[0]["next"]
            self._execute(
                "INSERT INTO workspace_messages "
                "(workspace_id, position, message_json) VALUES (?, ?, ?)",
                (message.workspace_id, position,
                 dump_json(scrub(message.model_dump(mode="json")))))
        return message

    def list_messages(self, workspace_id: str) -> list[WorkspaceMessage]:
        rows = self._query(
            "SELECT message_json FROM workspace_messages "
            "WHERE workspace_id = ? ORDER BY position", (workspace_id,))
        return [WorkspaceMessage.model_validate(load_json(r["message_json"]))
                for r in rows]

    # -- reaction evidence (Stage 3.4 §21) ----------------------------------

    def save_reaction_evidence(self,
                               evidence: ReactionEvidence) -> ReactionEvidence:
        """Upsert one evidence record, keyed by its internal evidence_id."""
        self._execute(
            "INSERT INTO reaction_template_evidence "
            "(evidence_id, reaction_template_id, source_type, source_id, "
            "evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(evidence_id) DO UPDATE SET "
            "reaction_template_id=excluded.reaction_template_id, "
            "source_type=excluded.source_type, source_id=excluded.source_id, "
            "evidence_json=excluded.evidence_json",
            (evidence.evidence_id, evidence.reaction_template_id,
             evidence.source.source_type.value if evidence.source else "",
             evidence.source.source_id if evidence.source else "",
             dump_json(scrub(evidence.model_dump(mode="json"))),
             evidence.source.retrieved_at if evidence.source else utc_now_iso()))
        return evidence

    def get_reaction_evidence(self,
                              evidence_id: str) -> ReactionEvidence | None:
        rows = self._query(
            "SELECT evidence_json FROM reaction_template_evidence "
            "WHERE evidence_id = ?", (evidence_id,))
        if not rows:
            return None
        return ReactionEvidence.model_validate(load_json(rows[0]["evidence_json"]))

    def list_reaction_evidence(self,
                               reaction_template_id: str) -> list[ReactionEvidence]:
        rows = self._query(
            "SELECT evidence_json FROM reaction_template_evidence "
            "WHERE reaction_template_id = ? ORDER BY created_at, evidence_id",
            (reaction_template_id,))
        return [ReactionEvidence.model_validate(load_json(r["evidence_json"]))
                for r in rows]

    def find_reaction_evidence_by_source(self, source_type: str,
                                         source_id: str) -> list[ReactionEvidence]:
        """Evidence that cites a given source -- the reverse query (§2/§9)."""
        if not source_id:
            return []
        rows = self._query(
            "SELECT evidence_json FROM reaction_template_evidence "
            "WHERE source_type = ? AND source_id = ? "
            "ORDER BY created_at, evidence_id", (source_type, source_id))
        return [ReactionEvidence.model_validate(load_json(r["evidence_json"]))
                for r in rows]

    # -- research sessions (Stage 7.1) --------------------------------------

    def save_study(self, study: ResearchStudy) -> ResearchStudy:
        """Upsert a study's metadata."""
        self._execute(
            "INSERT INTO research_studies (study_id, title, question, status, "
            "created_at, updated_at, start_molecule_id, target_molecule_id) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(study_id) DO UPDATE SET title=excluded.title, "
            "question=excluded.question, status=excluded.status, "
            "updated_at=excluded.updated_at, "
            "start_molecule_id=excluded.start_molecule_id, "
            "target_molecule_id=excluded.target_molecule_id",
            (study.study_id, study.title, study.question, study.status.value,
             study.created_at, study.updated_at, study.start_molecule_id,
             study.target_molecule_id))
        return study

    def get_study(self, study_id: str) -> ResearchStudy | None:
        rows = self._query(
            "SELECT * FROM research_studies WHERE study_id = ?", (study_id,))
        if not rows:
            return None
        return self._study_from_row(rows[0])

    def list_studies(self, status: str | None = None, limit: int = 100,
                     offset: int = 0) -> list[ResearchStudy]:
        if status:
            rows = self._query(
                "SELECT * FROM research_studies WHERE status = ? "
                "ORDER BY updated_at DESC, study_id LIMIT ? OFFSET ?",
                (status, limit, offset))
        else:
            rows = self._query(
                "SELECT * FROM research_studies "
                "ORDER BY updated_at DESC, study_id LIMIT ? OFFSET ?",
                (limit, offset))
        return [self._study_from_row(r) for r in rows]

    @staticmethod
    def _study_from_row(row) -> ResearchStudy:
        return ResearchStudy(
            study_id=row["study_id"], title=row["title"],
            question=row["question"], status=StudyStatus(row["status"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
            start_molecule_id=row["start_molecule_id"],
            target_molecule_id=row["target_molecule_id"])

    def save_study_reference(self, study_id: str,
                             reference: StudyReference) -> StudyReference:
        rows = self._query(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next "
            "FROM research_study_references WHERE study_id = ?", (study_id,))
        self._execute(
            "INSERT INTO research_study_references (study_id, reference_id, "
            "kind, object_id, label, added_at, note, position) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (study_id, reference.reference_id, reference.kind.value,
             reference.object_id, reference.label, reference.added_at,
             reference.note, rows[0]["next"]))
        return reference

    def delete_study_reference(self, study_id: str, reference_id: str) -> bool:
        cursor = self._execute(
            "DELETE FROM research_study_references "
            "WHERE study_id = ? AND reference_id = ?", (study_id, reference_id))
        return cursor.rowcount > 0

    def list_study_references(self, study_id: str) -> list[StudyReference]:
        rows = self._query(
            "SELECT * FROM research_study_references WHERE study_id = ? "
            "ORDER BY position", (study_id,))
        return [StudyReference(
            reference_id=r["reference_id"], kind=ReferenceKind(r["kind"]),
            object_id=r["object_id"], label=r["label"], added_at=r["added_at"],
            note=r["note"]) for r in rows]

    def append_study_event(self, study_id: str,
                           event: StudyEvent) -> StudyEvent:
        rows = self._query(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next "
            "FROM research_study_events WHERE study_id = ?", (study_id,))
        self._execute(
            "INSERT INTO research_study_events (study_id, event_id, event_type, "
            "at, summary, reference_id, artifact_id, note, position) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (study_id, event.event_id, event.event_type.value, event.at,
             event.summary, event.reference_id, event.artifact_id, event.note,
             rows[0]["next"]))
        return event

    def list_study_events(self, study_id: str) -> list[StudyEvent]:
        # deterministic timeline: by time, then by the explicit append position
        # (never DB natural order, §三十五). Position keeps events that share a
        # timestamp in the order they were recorded.
        rows = self._query(
            "SELECT * FROM research_study_events WHERE study_id = ? "
            "ORDER BY at, position", (study_id,))
        return [StudyEvent(
            event_id=r["event_id"], event_type=StudyEventType(r["event_type"]),
            at=r["at"], summary=r["summary"], reference_id=r["reference_id"],
            artifact_id=r["artifact_id"], note=r["note"]) for r in rows]

    def save_study_artifact(self, study_id: str,
                            artifact: StudyArtifact) -> StudyArtifact:
        rows = self._query(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next "
            "FROM research_study_artifacts WHERE study_id = ?", (study_id,))
        self._execute(
            "INSERT INTO research_study_artifacts (study_id, artifact_id, "
            "artifact_type, created_at, label, content_json, position) "
            "VALUES (?,?,?,?,?,?,?)",
            (study_id, artifact.artifact_id, artifact.artifact_type.value,
             artifact.created_at, artifact.label,
             dump_json(scrub(artifact.content)), rows[0]["next"]))
        return artifact

    def get_study_artifact(self, study_id: str,
                           artifact_id: str) -> StudyArtifact | None:
        rows = self._query(
            "SELECT * FROM research_study_artifacts "
            "WHERE study_id = ? AND artifact_id = ?", (study_id, artifact_id))
        if not rows:
            return None
        r = rows[0]
        return StudyArtifact(
            artifact_id=r["artifact_id"],
            artifact_type=ArtifactType(r["artifact_type"]),
            created_at=r["created_at"], label=r["label"],
            content=load_json(r["content_json"]) or {})

    def list_study_artifacts(self, study_id: str) -> list[StudyArtifact]:
        rows = self._query(
            "SELECT * FROM research_study_artifacts WHERE study_id = ? "
            "ORDER BY created_at, position", (study_id,))
        return [StudyArtifact(
            artifact_id=r["artifact_id"],
            artifact_type=ArtifactType(r["artifact_type"]),
            created_at=r["created_at"], label=r["label"],
            content=load_json(r["content_json"]) or {}) for r in rows]

    def get_study_detail(self, study_id: str) -> ResearchStudyDetail | None:
        study = self.get_study(study_id)
        if study is None:
            return None
        return ResearchStudyDetail(
            study=study, references=self.list_study_references(study_id),
            events=self.list_study_events(study_id),
            artifacts=self.list_study_artifacts(study_id))

    # -- study context (M1.5) -----------------------------------------------
    #
    # Every statement is keyed by study_id first and ordered by the explicit
    # append position: isolation and determinism are the storage's job, and a
    # context row that outlived its study cannot exist (ON DELETE CASCADE).

    def save_study_fact(self, study_id: str, fact: "StudyFact") -> "StudyFact":
        rows = self._query(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next "
            "FROM study_context_facts WHERE study_id = ?", (study_id,))
        self._execute(
            "INSERT INTO study_context_facts (study_id, fact_id, session_id, "
            "fact_type, verification, source, entity_id, summary, "
            "payload_json, provenance_json, evidence_ids_json, created_at, "
            "position) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (study_id, fact.fact_id, fact.session_id, fact.fact_type,
             fact.verification.value, fact.source, fact.entity_id,
             fact.summary, dump_json(scrub(fact.payload)),
             dump_json(scrub(fact.provenance)) if fact.provenance is not None
             else None,
             dump_json(fact.evidence_ids), fact.created_at, rows[0]["next"]))
        return fact

    def list_study_facts(self, study_id: str) -> list["StudyFact"]:
        rows = self._query(
            "SELECT * FROM study_context_facts WHERE study_id = ? "
            "ORDER BY position", (study_id,))
        return [StudyFact(
            fact_id=r["fact_id"], study_id=study_id, session_id=r["session_id"],
            fact_type=r["fact_type"],
            verification=FactVerification(r["verification"]),
            source=r["source"], entity_id=r["entity_id"], summary=r["summary"],
            payload=load_json(r["payload_json"]) or {},
            provenance=load_json(r["provenance_json"]),
            evidence_ids=load_json(r["evidence_ids_json"]) or [],
            created_at=r["created_at"]) for r in rows]

    def save_study_hypothesis(self, study_id: str,
                              hypothesis: "StudyHypothesis") -> "StudyHypothesis":
        # Upsert: a status change rewrites one row in place, keeping its id,
        # creation time and position -- the hypothesis is the same object
        # throughout, and its history is the statuses it passed through as
        # recorded by the service, never a second row.
        rows = self._query(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next "
            "FROM study_context_hypotheses WHERE study_id = ?", (study_id,))
        self._execute(
            "INSERT INTO study_context_hypotheses (study_id, hypothesis_id, "
            "session_id, content, entity_id, status, supporting_evidence_json, "
            "contradicting_evidence_json, created_at, updated_at, note, "
            "position) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(study_id, hypothesis_id) DO UPDATE SET "
            "status=excluded.status, "
            "supporting_evidence_json=excluded.supporting_evidence_json, "
            "contradicting_evidence_json=excluded.contradicting_evidence_json, "
            "updated_at=excluded.updated_at, note=excluded.note",
            (study_id, hypothesis.hypothesis_id, hypothesis.session_id,
             hypothesis.content, hypothesis.entity_id, hypothesis.status.value,
             dump_json(hypothesis.supporting_evidence_ids),
             dump_json(hypothesis.contradicting_evidence_ids),
             hypothesis.created_at, hypothesis.updated_at, hypothesis.note,
             rows[0]["next"]))
        return hypothesis

    def list_study_hypotheses(self, study_id: str) -> list["StudyHypothesis"]:
        rows = self._query(
            "SELECT * FROM study_context_hypotheses WHERE study_id = ? "
            "ORDER BY position", (study_id,))
        return [StudyHypothesis(
            hypothesis_id=r["hypothesis_id"], study_id=study_id,
            session_id=r["session_id"], content=r["content"],
            entity_id=r["entity_id"], status=HypothesisStatus(r["status"]),
            supporting_evidence_ids=load_json(
                r["supporting_evidence_json"]) or [],
            contradicting_evidence_ids=load_json(
                r["contradicting_evidence_json"]) or [],
            created_at=r["created_at"], updated_at=r["updated_at"],
            note=r["note"]) for r in rows]

    def save_study_decision(self, study_id: str,
                            decision: "StudyDecision") -> "StudyDecision":
        rows = self._query(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next "
            "FROM study_context_decisions WHERE study_id = ?", (study_id,))
        self._execute(
            "INSERT INTO study_context_decisions (study_id, decision_id, "
            "session_id, decision, reason, observation_kind, "
            "plan_change_kinds_json, entity_ids_json, evidence_ids_json, "
            "hypothesis_ids_json, created_at, position) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (study_id, decision.decision_id, decision.session_id,
             decision.decision, decision.reason, decision.observation_kind,
             dump_json(decision.plan_change_kinds),
             dump_json(decision.entity_ids),
             dump_json(decision.evidence_ids),
             dump_json(decision.hypothesis_ids), decision.created_at,
             rows[0]["next"]))
        return decision

    def list_study_decisions(self, study_id: str) -> list["StudyDecision"]:
        rows = self._query(
            "SELECT * FROM study_context_decisions WHERE study_id = ? "
            "ORDER BY position", (study_id,))
        return [StudyDecision(
            decision_id=r["decision_id"], study_id=study_id,
            session_id=r["session_id"], decision=r["decision"],
            reason=r["reason"], observation_kind=r["observation_kind"],
            plan_change_kinds=load_json(r["plan_change_kinds_json"]) or [],
            entity_ids=load_json(r["entity_ids_json"]) or [],
            evidence_ids=load_json(r["evidence_ids_json"]) or [],
            hypothesis_ids=load_json(r["hypothesis_ids_json"]) or [],
            created_at=r["created_at"]) for r in rows]

    def save_unresolved_issue(self, study_id: str,
                              issue: "UnresolvedIssue") -> "UnresolvedIssue":
        rows = self._query(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next "
            "FROM study_context_issues WHERE study_id = ?", (study_id,))
        self._execute(
            "INSERT INTO study_context_issues (study_id, issue_id, session_id, "
            "kind, content, status, entity_id, created_at, resolved_at, "
            "resolution, position) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (study_id, issue.issue_id, issue.session_id, issue.kind,
             issue.content, issue.status.value, issue.entity_id,
             issue.created_at, issue.resolved_at, issue.resolution,
             rows[0]["next"]))
        return issue

    def update_unresolved_issue(self, study_id: str,
                                issue: "UnresolvedIssue") -> "UnresolvedIssue":
        self._execute(
            "UPDATE study_context_issues SET status = ?, resolved_at = ?, "
            "resolution = ? WHERE study_id = ? AND issue_id = ?",
            (issue.status.value, issue.resolved_at, issue.resolution,
             study_id, issue.issue_id))
        return issue

    def list_unresolved_issues(self, study_id: str) -> list["UnresolvedIssue"]:
        rows = self._query(
            "SELECT * FROM study_context_issues WHERE study_id = ? "
            "ORDER BY position", (study_id,))
        return [UnresolvedIssue(
            issue_id=r["issue_id"], study_id=study_id,
            session_id=r["session_id"], kind=r["kind"], content=r["content"],
            status=IssueStatus(r["status"]), entity_id=r["entity_id"],
            created_at=r["created_at"], resolved_at=r["resolved_at"],
            resolution=r["resolution"]) for r in rows]

    def save_identity_record(self, study_id: str,
                             record: "IdentitySelectionRecord"
                             ) -> "IdentitySelectionRecord":
        rows = self._query(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next "
            "FROM study_context_identity_records WHERE study_id = ?",
            (study_id,))
        self._execute(
            "INSERT INTO study_context_identity_records (study_id, record_id, "
            "session_id, question, candidates_json, selected_index, "
            "selected_by, user_selection_statement, selected_at, created_at, "
            "position) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(study_id, record_id) DO UPDATE SET "
            "selected_index=excluded.selected_index, "
            "selected_by=excluded.selected_by, "
            "user_selection_statement=excluded.user_selection_statement, "
            "selected_at=excluded.selected_at",
            (study_id, record.record_id, record.session_id, record.question,
             dump_json([c.model_dump(mode="json") for c in record.candidates]),
             record.selected_index, record.selected_by,
             record.user_selection_statement, record.selected_at,
             record.created_at, rows[0]["next"]))
        return record

    def list_identity_records(self, study_id: str) -> list["IdentitySelectionRecord"]:
        rows = self._query(
            "SELECT * FROM study_context_identity_records WHERE study_id = ? "
            "ORDER BY position", (study_id,))
        return [IdentitySelectionRecord(
            record_id=r["record_id"], study_id=study_id,
            session_id=r["session_id"], question=r["question"],
            candidates=[IdentityCandidate(**c) for c in (
                load_json(r["candidates_json"]) or [])],
            selected_index=r["selected_index"],
            selected_by=r["selected_by"],
            user_selection_statement=r["user_selection_statement"],
            selected_at=r["selected_at"],
            created_at=r["created_at"]) for r in rows]

    # -- golden cases (M1.7.2) ----------------------------------------------
    #
    # content_json is the source of truth for every record here; the scalar
    # columns exist so queries can filter without parsing JSON, and they are
    # rewritten from the same object on each save so they cannot drift. A
    # row whose content_json no longer reconstructs its model raises
    # TransactionError -- loudly, never a half-object.

    def _golden_row_model(self, model_cls, raw, label: str):
        try:
            return model_cls.model_validate(load_json(raw))
        except (ValueError, TypeError) as exc:  # json errors and ValidationError
            raise TransactionError(
                f"stored {label} record is not reconstructable: {exc}") from exc

    def save_golden_case(self, case: GoldenCase) -> GoldenCase:
        """Create a case. An existing case_id is a Duplicate, never a
        silent overwrite -- identity is the caller's, collisions are news."""
        self._execute(
            "INSERT INTO golden_cases (case_id, name, case_type, "
            "case_version, schema_version, status, created_at, updated_at, "
            "content_json, position) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (case.case_id, case.name, case.case_type.value,
             case.case_version, case.schema_version, case.status.value,
             case.created_at, case.updated_at, _model_dump(case),
             self._next_position("golden_cases")))
        return case

    def update_golden_case(self, case: GoldenCase) -> GoldenCase:
        """Replace a stored case's content. NotFound when absent."""
        cursor = self._execute(
            "UPDATE golden_cases SET name=?, case_type=?, case_version=?, "
            "schema_version=?, status=?, updated_at=?, content_json=? "
            "WHERE case_id=?",
            (case.name, case.case_type.value, case.case_version,
             case.schema_version, case.status.value, case.updated_at,
             _model_dump(case), case.case_id))
        if cursor.rowcount == 0:
            raise NotFound(f"no golden case {case.case_id!r}")
        return case

    def get_golden_case(self, case_id: str) -> GoldenCase | None:
        rows = self._query(
            "SELECT content_json FROM golden_cases WHERE case_id = ?",
            (case_id,))
        if not rows:
            return None
        return self._golden_row_model(GoldenCase, rows[0]["content_json"],
                                      f"golden case {case_id!r}")

    def list_golden_cases(self, *, case_type: GoldenCaseType | None = None,
                          status: GoldenCaseStatus | None = None
                          ) -> list[GoldenCase]:
        sql = ("SELECT content_json FROM golden_cases "
               "WHERE (? IS NULL OR case_type = ?) "
               "AND (? IS NULL OR status = ?) ORDER BY position")
        ct = case_type.value if case_type is not None else None
        st = status.value if status is not None else None
        rows = self._query(sql, (ct, ct, st, st))
        return [self._golden_row_model(GoldenCase, r["content_json"],
                                       "golden case") for r in rows]

    def get_golden_case_version(self, case_id: str) -> str | None:
        rows = self._query(
            "SELECT case_version FROM golden_cases WHERE case_id = ?",
            (case_id,))
        return rows[0]["case_version"] if rows else None

    def save_golden_case_reference(self, case_id: str,
                                   material: ReferenceMaterial
                                   ) -> ReferenceMaterial:
        """Upsert one reference of one case.

        The `hidden` column mirrors `material.hidden` from the same object
        -- both written together, so they cannot disagree. Whether an
        update is *allowed* to change `hidden` is the service layer's
        rule, not this adapter's.
        """
        self._execute(
            "INSERT INTO golden_case_references (case_id, reference_id, "
            "hidden, content_json, position) VALUES (?,?,?,?,?) "
            "ON CONFLICT(case_id, reference_id) DO UPDATE SET "
            "hidden=excluded.hidden, content_json=excluded.content_json",
            (case_id, material.reference_id, 1 if material.hidden else 0,
             _model_dump(material),
             self._next_position("golden_case_references", case_id)))
        return material

    def get_golden_case_reference(self, case_id: str,
                                  reference_id: str) -> ReferenceMaterial | None:
        rows = self._query(
            "SELECT content_json FROM golden_case_references "
            "WHERE case_id = ? AND reference_id = ?", (case_id, reference_id))
        if not rows:
            return None
        return self._golden_row_model(ReferenceMaterial,
                                      rows[0]["content_json"], "reference")

    def list_golden_case_references(self, case_id: str
                                    ) -> list[ReferenceMaterial]:
        rows = self._query(
            "SELECT content_json FROM golden_case_references "
            "WHERE case_id = ? ORDER BY position", (case_id,))
        return [self._golden_row_model(ReferenceMaterial, r["content_json"],
                                       "reference") for r in rows]

    def save_golden_case_trace(self, trace: ResearchTrace) -> ResearchTrace:
        self._execute(
            "INSERT INTO golden_case_traces (trace_id, case_id, "
            "case_version, session_id, status, content_json, position) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(trace_id) DO UPDATE SET "
            "content_json=excluded.content_json, status=excluded.status",
            (trace.trace_id, trace.case_id, trace.case_version,
             trace.session_id, trace.status.value, _model_dump(trace),
             self._next_position("golden_case_traces")))
        return trace

    def get_golden_case_trace(self, trace_id: str) -> ResearchTrace | None:
        rows = self._query(
            "SELECT content_json FROM golden_case_traces WHERE trace_id = ?",
            (trace_id,))
        if not rows:
            return None
        return self._golden_row_model(ResearchTrace, rows[0]["content_json"],
                                      "trace")

    def list_golden_case_traces(self, case_id: str) -> list[ResearchTrace]:
        rows = self._query(
            "SELECT content_json FROM golden_case_traces WHERE case_id = ? "
            "ORDER BY position", (case_id,))
        return [self._golden_row_model(ResearchTrace, r["content_json"],
                                       "trace") for r in rows]

    def save_golden_case_evaluation(self, result: EvaluationResult
                                    ) -> EvaluationResult:
        self._execute(
            "INSERT INTO golden_case_evaluations (evaluation_id, case_id, "
            "case_version, trace_id, evaluation_version, content_json, "
            "position) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(evaluation_id) DO UPDATE SET "
            "content_json=excluded.content_json",
            (result.evaluation_id, result.case_id, result.case_version,
             result.trace_id, result.evaluation_version, _model_dump(result),
             self._next_position("golden_case_evaluations")))
        return result

    def get_golden_case_evaluation(self, evaluation_id: str
                                   ) -> EvaluationResult | None:
        rows = self._query(
            "SELECT content_json FROM golden_case_evaluations "
            "WHERE evaluation_id = ?", (evaluation_id,))
        if not rows:
            return None
        return self._golden_row_model(EvaluationResult, rows[0]["content_json"],
                                      "evaluation")

    def list_golden_case_evaluations(self, case_id: str
                                     ) -> list[EvaluationResult]:
        rows = self._query(
            "SELECT content_json FROM golden_case_evaluations "
            "WHERE case_id = ? ORDER BY position", (case_id,))
        return [self._golden_row_model(EvaluationResult, r["content_json"],
                                       "evaluation") for r in rows]

    def save_unseal_event(self, event: UnsealEvent) -> UnsealEvent:
        """Append one audit event. There is no update and no delete: the
        history of who opened what is not a mutable field."""
        self._execute(
            "INSERT INTO golden_case_unseal_events (event_id, case_id, "
            "case_version, reference_id, outcome, actor, reason, "
            "previous_hidden, new_hidden, diagnostics, at, position) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (event.event_id, event.case_id, event.case_version,
             event.reference_id, event.outcome.value, event.actor,
             event.reason, 1 if event.previous_hidden else 0,
             1 if event.new_hidden else 0, event.diagnostics, event.at,
             self._next_position("golden_case_unseal_events")))
        return event

    def list_unseal_events(self, case_id: str, *,
                           reference_id: str | None = None
                           ) -> list[UnsealEvent]:
        rows = self._query(
            "SELECT * FROM golden_case_unseal_events "
            "WHERE case_id = ? AND (? IS NULL OR reference_id = ?) "
            "ORDER BY position", (case_id, reference_id, reference_id))
        return [UnsealEvent(
            event_id=r["event_id"], case_id=case_id,
            case_version=r["case_version"], reference_id=r["reference_id"],
            outcome=UnsealOutcome(r["outcome"]), actor=r["actor"],
            reason=r["reason"], previous_hidden=bool(r["previous_hidden"]),
            new_hidden=bool(r["new_hidden"]), diagnostics=r["diagnostics"],
            at=r["at"]) for r in rows]

    def _next_position(self, table: str, where_value: str | None = None
                       ) -> int:
        """Next append position in a table (optionally scoped by case_id)."""
        if where_value is None:
            rows = self._query(
                f"SELECT COALESCE(MAX(position), 0) + 1 AS next FROM {table}")
        else:
            rows = self._query(
                f"SELECT COALESCE(MAX(position), 0) + 1 AS next "
                f"FROM {table} WHERE case_id = ?", (where_value,))
        return rows[0]["next"]

    # -- research workflows (Stage 7.2) -------------------------------------

    def save_workflow(self, workflow: TaskWorkflow) -> TaskWorkflow:
        self._execute(
            "INSERT INTO task_workflows (workflow_id, study_id, template, "
            "title, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(workflow_id) DO UPDATE SET template=excluded.template, "
            "title=excluded.title, status=excluded.status, "
            "updated_at=excluded.updated_at",
            (workflow.workflow_id, workflow.study_id, workflow.template,
             workflow.title, workflow.status.value, workflow.created_at,
             workflow.updated_at))
        return workflow

    def get_workflow(self, workflow_id: str) -> TaskWorkflow | None:
        rows = self._query(
            "SELECT * FROM task_workflows WHERE workflow_id = ?",
            (workflow_id,))
        return self._workflow_from_row(rows[0]) if rows else None

    def list_workflows(self, study_id: str) -> list[TaskWorkflow]:
        rows = self._query(
            "SELECT * FROM task_workflows WHERE study_id = ? "
            "ORDER BY created_at, workflow_id", (study_id,))
        return [self._workflow_from_row(r) for r in rows]

    @staticmethod
    def _workflow_from_row(row) -> TaskWorkflow:
        return TaskWorkflow(
            workflow_id=row["workflow_id"], study_id=row["study_id"],
            template=row["template"], title=row["title"],
            status=TaskWorkflowStatus(row["status"]), created_at=row["created_at"],
            updated_at=row["updated_at"])

    def save_task(self, task: WorkflowTask) -> WorkflowTask:
        rows = self._query(
            "SELECT position FROM workflow_tasks WHERE task_id = ?", (task.task_id,))
        if rows:
            self._execute(
                "UPDATE workflow_tasks SET task_type=?, status=?, title=?, "
                "input_refs_json=?, depends_on_json=?, output_artifact_ids_json=?, "
                "attempt_count=?, error=?, updated_at=? WHERE task_id=?",
                (task.task_type.value, task.status.value, task.title,
                 dump_json(task.input_refs), dump_json(task.depends_on),
                 dump_json(task.output_artifact_ids), task.attempt_count,
                 task.error, task.updated_at, task.task_id))
            return task
        pos = self._query(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next FROM workflow_tasks "
            "WHERE workflow_id = ?", (task.workflow_id,))[0]["next"]
        self._execute(
            "INSERT INTO workflow_tasks (task_id, workflow_id, task_type, "
            "status, title, input_refs_json, depends_on_json, "
            "output_artifact_ids_json, attempt_count, error, created_at, "
            "updated_at, position) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task.task_id, task.workflow_id, task.task_type.value,
             task.status.value, task.title, dump_json(task.input_refs),
             dump_json(task.depends_on), dump_json(task.output_artifact_ids),
             task.attempt_count, task.error, task.created_at, task.updated_at,
             pos))
        return task

    def get_task(self, task_id: str) -> WorkflowTask | None:
        rows = self._query(
            "SELECT * FROM workflow_tasks WHERE task_id = ?", (task_id,))
        return self._task_from_row(rows[0]) if rows else None

    def list_tasks(self, workflow_id: str) -> list[WorkflowTask]:
        rows = self._query(
            "SELECT * FROM workflow_tasks WHERE workflow_id = ? "
            "ORDER BY position", (workflow_id,))
        return [self._task_from_row(r) for r in rows]

    @staticmethod
    def _task_from_row(row) -> WorkflowTask:
        return WorkflowTask(
            task_id=row["task_id"], workflow_id=row["workflow_id"],
            task_type=TaskType(row["task_type"]), status=TaskStatus(row["status"]),
            title=row["title"], input_refs=load_json(row["input_refs_json"]) or {},
            depends_on=list(load_json(row["depends_on_json"]) or []),
            output_artifact_ids=list(load_json(row["output_artifact_ids_json"]) or []),
            attempt_count=row["attempt_count"], error=row["error"],
            created_at=row["created_at"], updated_at=row["updated_at"])

    def get_workflow_detail(self, workflow_id: str) -> TaskWorkflowDetail | None:
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            return None
        return TaskWorkflowDetail(
            workflow=workflow, tasks=self.list_tasks(workflow_id))


# ---------------------------------------------------------------------------
# the registries' existing protocols, over this store (§4)
# ---------------------------------------------------------------------------


class SQLiteChemicalEntityRepository:
    """`ChemicalEntityRepository` (services.entity_registry) over SQLite.

    The protocol is the swap point 3.8.1 designed; this is the second
    implementation of it and not a new abstraction. `add` is an upsert of
    the entity's state: the registry mutates entities in place on merge and
    then tells the repository about it, and only an upsert survives that
    against a database.
    """

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def get(self, entity_id: str) -> ChemicalEntity | None:
        return self._store.get_entity(entity_id)

    def add(self, entity: ChemicalEntity) -> None:
        self._store.save_entity(entity)

    def find_by_identity_key(self, key: str) -> ChemicalEntity | None:
        return self._store.find_entity_by_identity_key(key)

    def find_by_external_id(self, namespace: str,
                            identifier: str) -> ChemicalEntity | None:
        return self._store.find_entity_by_external_id(namespace, identifier)

    def all(self) -> list[ChemicalEntity]:
        return self._store.list_entities()

    def index_external(self, entity: ChemicalEntity) -> None:
        self._store.save_entity(entity)


class SQLiteReactionRepository:
    """`ReactionRepository` (services.reaction_builder) over SQLite.

    Same story as the entity adapter: the protocol already existed, `add`
    is an upsert of the reaction's state, and no second abstraction is
    invented here.
    """

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def get(self, reaction_id: str) -> Reaction | None:
        return self._store.get_reaction(reaction_id)

    def add(self, reaction: Reaction) -> None:
        self._store.save_reaction(reaction)

    def find_by_scope_key(self, paper_scope: str,
                          reaction_key: str) -> Reaction | None:
        return self._store.find_reaction_by_scope_key(paper_scope, reaction_key)

    def find_by_paper(self, paper_id: str) -> list[Reaction]:
        return self._store.find_reactions_by_paper(paper_id)

    def find_by_doi(self, doi: str) -> list[Reaction]:
        return self._store.find_reactions_by_doi(doi)

    def find_by_component_entity(self, entity_id: str) -> list[Reaction]:
        return self._store.find_reactions_by_component_entity(entity_id)

    def find_by_product(self, entity_id: str) -> list[Reaction]:
        return self._store.find_reactions_by_product_entity(entity_id)

    def find_by_reactant(self, entity_id: str) -> list[Reaction]:
        return self._store.find_reactions_by_reactant_entity(entity_id)

    def find_between(self, reactant_ids: list[str],
                     product_ids: list[str]) -> list[Reaction]:
        return self._store.find_reactions_between(reactant_ids, product_ids)

    def all(self) -> list[Reaction]:
        return self._store.list_reactions()


class SQLiteWorkspaceRepository:
    """`WorkspaceRepository` (services.workspace) over SQLite (Stage F).

    Same story as the entity and reaction adapters: the protocol lives in
    the services layer, and this is the second implementation of it -- thin
    delegation to the store's workspace methods, no new abstraction.
    """

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def create_workspace(self, workspace: Workspace) -> Workspace:
        return self._store.save_workspace(workspace)

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        return self._store.get_workspace(workspace_id)

    def list_workspaces(self) -> list[Workspace]:
        return self._store.list_workspaces()

    def append_message(self, message: WorkspaceMessage) -> WorkspaceMessage:
        return self._store.append_message(message)

    def list_messages(self, workspace_id: str) -> list[WorkspaceMessage]:
        return self._store.list_messages(workspace_id)


__all__ = [
    "SCHEMA_VERSION",
    "SQLiteChemicalEntityRepository",
    "SQLiteReactionRepository",
    "SQLiteStore",
    "SQLiteWorkspaceRepository",
    "default_db_path",
    "dump_json",
    "load_json",
]

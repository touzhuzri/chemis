"""Persistence adapters for chemical research knowledge.

Phase 3.11. The repository protocols the registries were designed against
(`ChemicalEntityRepository`, `ReactionRepository`) live with their
registries; this package is their SQLite implementation plus the storage
of the knowledge objects that never had a protocol of their own (papers,
evidence, mentions, routes, sessions). See `crea.services.knowledge_base`
for the facade above it.
"""

from crea.repositories.errors import (
    ConstraintViolation,
    Duplicate,
    NotFound,
    RepositoryError,
    SchemaError,
    TransactionError,
    UNSUPPORTED_SCHEMA_VERSION,
)
from crea.repositories.sqlite import (
    SCHEMA_VERSION,
    SQLiteChemicalEntityRepository,
    SQLiteReactionRepository,
    SQLiteStore,
    default_db_path,
    dump_json,
)

__all__ = [
    "SCHEMA_VERSION",
    "ConstraintViolation",
    "Duplicate",
    "NotFound",
    "RepositoryError",
    "SQLiteChemicalEntityRepository",
    "SQLiteReactionRepository",
    "SQLiteStore",
    "SchemaError",
    "TransactionError",
    "UNSUPPORTED_SCHEMA_VERSION",
    "default_db_path",
    "dump_json",
]

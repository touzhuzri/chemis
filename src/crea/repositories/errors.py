"""Repository errors: storage failures, named as decisions.

Phase 3.11 (§59). A caller above the repository layer -- a service, the
agent -- must be able to tell "there is nothing there" from "what you tried
to write would break the knowledge graph" without reading SQLite's own
error strings. `sqlite3.IntegrityError` never escapes this package; every
failure is one of these.

The vocabulary is deliberately small and closed:

* `NotFound` -- a named object is not in the knowledge base.
* `Duplicate` -- a write that would mint a second identity collided.
* `ConstraintViolation` -- a foreign key or CHECK said no (and the
  transaction rolled back).
* `TransactionError` -- the unit of work could not commit as a whole.
* `SchemaError` -- the database is not a schema version this code speaks.
  `code` makes the refusal machine-readable (`UNSUPPORTED_SCHEMA_VERSION`
  is the one v1 knows how to raise).
"""

from __future__ import annotations


class RepositoryError(Exception):
    """Base class for every storage failure in this package."""


class NotFound(RepositoryError):
    """Named an object the knowledge base does not hold. Never a silent new one."""


class Duplicate(RepositoryError):
    """A write collided with an identity already stored."""


class ConstraintViolation(RepositoryError):
    """A foreign key or CHECK constraint refused the write."""


class TransactionError(RepositoryError):
    """A unit of work could not be completed; nothing from it was kept."""


class SchemaError(RepositoryError):
    """The database schema is not compatible with this code.

    Never "upgrade" a database in place to make a version problem go away:
    the refusal (`code="UNSUPPORTED_SCHEMA_VERSION"`) is the feature.
    """

    def __init__(self, message: str, *, code: str = "SCHEMA_ERROR") -> None:
        self.code = code
        super().__init__(message)


#: The code `SchemaError` carries when the stored schema version is one
#: this build does not implement. Named so tests and callers match on the
#: code, not on prose.
UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"


__all__ = [
    "ConstraintViolation",
    "Duplicate",
    "NotFound",
    "RepositoryError",
    "SchemaError",
    "TransactionError",
    "UNSUPPORTED_SCHEMA_VERSION",
]

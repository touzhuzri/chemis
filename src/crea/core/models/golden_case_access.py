"""M1.7.2 -- the access vocabulary for golden cases: two projections, one
audit trail, one content verdict.

M1.7.1 defined what a case *is*. This module defines what different
callers may *see*, as separate types rather than as one object with a
"hide secrets" flag:

* `GoldenCaseAgentView` -- everything a blind run may receive: the case's
  identity, its lifecycle status, and its `BlindInput`. It has no
  `reference_ids`, no `name`/`description`/`tags` (case prose can name
  the paper), no evaluation policy, and it refuses extra fields. A
  runner holding this type physically cannot read a reference: the data
  is not in the object, and no serialization of it can produce data that
  was never there.
* `GoldenCaseManagerView` -- the evaluation manager's full picture: the
  case, its references' metadata, its traces, its evaluations and its
  unseal history. Internal tooling only; this project has no user-auth
  layer, so the trust boundary is "code that deliberately asks for the
  manager view", and the agent path never asks.
* `UnsealRequest` / `UnsealEvent` -- unsealing is an explicit, audited,
  all-or-nothing operation. A request names a person and a reason; every
  attempt (unsealed, already unsealed, failed) is an append-only event
  carrying case id, case version, reference id, before/after hidden
  state. Reading a manager view unseals nothing; saving an evaluation
  unseals nothing.
* `ContentStatus` / `ReferenceContentResult` -- what reading a sealed
  artifact's bytes came back with. Only `OK` means verified content;
  missing hashes, mismatched hashes, unreadable or out-of-bounds locators
  all return their own status with `content=None`, so "unverified" can
  never wear the clothes of "valid reference material".

The honest boundary, restated: these models make the *agent path* structurally
unable to reach references, and make unsealing loud. They do not sandbox a
malicious Python caller holding the store -- that is an access-control
problem for a later, authenticated deployment, not a data-model problem.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from crea.core.models.golden_case import (
    BlindInput,
    GoldenCase,
    GoldenCaseStatus,
    GoldenCaseType,
    ReferenceMaterial,
    ResearchTrace,
    EvaluationResult,
)
from crea.core.provenance import utc_now_iso


class GoldenCaseAgentView(BaseModel):
    """The blind-run projection: the case's identity and its visible half.

    Built by projection, never by deletion: `build_agent_view` copies
    these fields out of a `GoldenCase` and the rest of the case is never
    referenced again. `extra="forbid"` closes the one hole a projection
    leaves open -- a caller trying to smuggle extra data through the
    constructor.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str
    case_type: GoldenCaseType
    case_version: str
    schema_version: str
    #: The lifecycle state, so a runner can refuse a draft or an archived
    #: case. It says nothing about the references.
    status: GoldenCaseStatus
    #: The entire visible half: question, entities, constraints, sandbox.
    blind_input: BlindInput

    @property
    def goal(self) -> str:
        return self.blind_input.goal


class GoldenCaseManagerView(BaseModel):
    """The evaluation manager's full picture of one case.

    Everything here is what a manager may inspect: the case object, each
    reference's *metadata* (still sealed or not -- hidden is a field, and
    unsealing is a separate audited act), the traces, the evaluations and
    the unseal history. Nothing on this type is reachable from the agent
    view, and nothing here auto-unseals.
    """

    case: GoldenCase
    references: list[ReferenceMaterial] = Field(default_factory=list)
    traces: list[ResearchTrace] = Field(default_factory=list)
    evaluations: list[EvaluationResult] = Field(default_factory=list)
    unseal_events: list["UnsealEvent"] = Field(default_factory=list)
    loaded_at: str = Field(default_factory=utc_now_iso)


class UnsealOutcome(str, Enum):
    """What one unseal attempt did. Attempts are never silent."""

    UNSEALED = "unsealed"
    ALREADY_UNSEALED = "already_unsealed"
    FAILED = "failed"


class UnsealRequest(BaseModel):
    """An explicit instruction to open references, from a person, with a
    reason. Both are required: an audit trail that names nobody and says
    nothing is not an audit trail."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    #: Empty means "every reference of this case" -- still explicit.
    reference_ids: list[str] = Field(default_factory=list)
    #: The person (or harness role) ordering the unseal.
    actor: str
    #: Why. "研究完成，进入专家对照评价阶段" is a reason; "" is not.
    reason: str

    @field_validator("case_id", "actor", "reason")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("UnsealRequest requires non-empty "
                             "case_id, actor and reason")
        return value


class UnsealEvent(BaseModel):
    """One audited unseal attempt, append-only.

    `case_version` is the version at attempt time: history never
    rewrites itself when the case later moves on. `outcome` says what
    happened; `previous_hidden`/`new_hidden` say what changed;
    `diagnostics` carries the failure detail when outcome is `failed`.
    """

    event_id: str
    case_id: str
    case_version: str
    reference_id: str
    outcome: UnsealOutcome
    actor: str
    reason: str
    previous_hidden: bool = True
    new_hidden: bool = True
    diagnostics: str = ""
    at: str = Field(default_factory=utc_now_iso)


class ContentStatus(str, Enum):
    """The verdict on reading one reference's stored content.

    Six honest outcomes. Only `OK` is verified content; every other state
    refuses to hand bytes back rather than letting unverified material
    pass as a reference.
    """

    OK = "ok"
    #: No stored artifact (or no locator naming one).
    NOT_FOUND = "not_found"
    #: The artifact exists but could not be read.
    UNREADABLE = "unreadable"
    #: The material declares no content hash to check against.
    HASH_MISSING = "hash_missing"
    #: The bytes do not hash to the declared value.
    HASH_MISMATCH = "hash_mismatch"
    #: The reference is still sealed; reading is not unsealing.
    NOT_UNSEALED = "not_unsealed"
    #: The locator uses a forbidden scheme or escapes the allowed roots.
    NOT_ALLOWED = "not_allowed"


class ReferenceContentResult(BaseModel):
    """What reading one reference's content produced.

    `content` is bytes only when `status is OK`. Every other status
    returns `content=None` with a readable `detail` -- a mismatch reports
    the computed hash so a manager can investigate, and never returns the
    mismatched bytes as if they were the reference.
    """

    reference_id: str
    status: ContentStatus
    content: bytes | None = None
    #: The declared hash, verbatim (e.g. "sha256:ab12...").
    declared_hash: str = ""
    #: The hash actually computed over the bytes read, when bytes were read.
    computed_hash: str = ""
    #: The declared locator, verbatim. Never the resolved absolute path:
    #: error detail must not leak local filesystem layout.
    locator: str = ""
    detail: str = ""


__all__ = [
    "ContentStatus",
    "GoldenCaseAgentView",
    "GoldenCaseManagerView",
    "ReferenceContentResult",
    "UnsealEvent",
    "UnsealOutcome",
    "UnsealRequest",
]

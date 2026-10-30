"""Exception hierarchy.

Every error the agent raises is one of these, so the CLI can map failures to
exit codes without pattern-matching on RDKit or httpx exceptions.
"""

from __future__ import annotations


class CreaError(Exception):
    """Base for every error raised by this package."""


class NormalizationError(CreaError):
    """Input could not be turned into a usable molecule."""

    def __init__(self, reason: str, input_repr: str = "") -> None:
        self.reason = reason
        self.input_repr = input_repr
        super().__init__(f"{reason} :: {input_repr!r}" if input_repr else reason)


class AlignmentError(CreaError):
    """MCS / atom-mapping could not be computed."""


class FragmentationError(CreaError):
    """Fragment decomposition failed."""


class ConformerError(CreaError):
    """3D conformer generation could not produce a conformer.

    Typed by `code` -- `INVALID_MOLECULE` (the input was not a structure) vs
    `CONFORMER_GENERATION_FAILED` (a valid structure could not be embedded) --
    so the API layer can map each to its own structured error without matching
    on English.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(message or code)


class MoleculeLibraryError(CreaError):
    """A molecule-library operation failed, typed by `code`.

    `INVALID_MOLECULE` (input is not a structure), `MOLECULE_NOT_FOUND` (no
    such record), `PROVIDER_ERROR` / `PROVIDER_TIMEOUT` /
    `PROVIDER_INVALID_RESPONSE` (a candidate source failed). Never a raw
    RDKit or HTTP exception.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(message or code)


class ReactionEvidenceError(CreaError):
    """A reaction-evidence operation failed, typed by `code`.

    `TEMPLATE_NOT_FOUND` (no such ReactionTemplate), `EVIDENCE_NOT_FOUND`,
    `INVALID_EVIDENCE` (the record does not validate). Never a raw exception.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(message or code)


class ReactionComparisonError(CreaError):
    """A reaction-comparison operation failed, typed by `code`.

    `INVALID_INPUT` (nothing to compare), `TEMPLATE_NOT_FOUND` (no such
    ReactionTemplate). Never a raw exception.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(message or code)


class ReactionGraphError(CreaError):
    """A reaction-graph operation failed, typed by `code`.

    `MOLECULE_NOT_FOUND`, `TEMPLATE_NOT_FOUND`, `LITERATURE_NOT_FOUND`. Never a
    raw exception.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(message or code)


class RouteExplorerError(CreaError):
    """A route-explorer operation failed, typed by `code`.

    `INVALID_ROUTE_DEPTH` (max_steps out of range). Never a raw exception.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(message or code)


class ModificationAnalysisError(CreaError):
    """An AI modification analysis failed, typed by `code`.

    `AI_PROVIDER_UNAVAILABLE` / `AI_TIMEOUT` / `AI_RATE_LIMITED` /
    `AI_OUTPUT_INVALID`. The deterministic chemistry tools are unaffected.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(message or code)


class RouteReasoningError(CreaError):
    """AI route reasoning failed, typed by `code`.

    `AI_PROVIDER_UNAVAILABLE` / `AI_TIMEOUT` / `AI_RATE_LIMITED` /
    `AI_OUTPUT_INVALID`. The deterministic Route Explorer is unaffected.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(message or code)


class ResearchStudyError(CreaError):
    """A research-session operation failed, typed by `code`.

    `STUDY_NOT_FOUND`, `REFERENCE_NOT_FOUND` (the referenced object is not in
    the knowledge base), `INVALID_STATUS_TRANSITION`, `INVALID_INPUT`. The
    session never creates the objects it references (§四十/§六十三).
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(message or code)


class ResearchWorkflowError(CreaError):
    """A research-workflow operation failed, typed by `code`.

    `WORKFLOW_NOT_FOUND`, `TASK_NOT_FOUND`, `WORKFLOW_CYCLE`,
    `WORKFLOW_LIMIT_REACHED`, `INVALID_WORKFLOW_STATE`, `TASK_INPUT_NOT_FOUND`,
    `TASK_NOT_READY`, `TASK_RETRY_LIMIT`, `INVALID_INPUT`.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(message or code)


class GoldenCaseError(CreaError):
    """A golden-case (evaluation infrastructure) operation failed, typed
    by `code`.

    `CASE_NOT_FOUND` / `CASE_EXISTS` / `CASE_VERSION_CONFLICT` /
    `CASE_INPUT_MISSING` (no BlindInput for a run), `REFERENCE_NOT_FOUND`,
    `TRACE_NOT_FOUND` / `TRACE_CASE_MISMATCH`, `EVALUATION_NOT_FOUND` /
    `EVALUATION_VERSION_MISMATCH` (the verdict does not judge the trace's
    case version), `UNSEAL_NOT_ALLOWED`, `CASE_RECORD_CORRUPT` (stored
    JSON no longer reconstructs a model), `INVALID_INPUT`. Never a raw
    sqlite3 or pydantic exception.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(message or code)


class ContextIntegrityError(CreaError):
    """An AI context would be structurally broken.

    Raised when a record references something that must be present but is not
    -- e.g. an evidence record naming a literature id with no matching paper
    (§34). A half-formed context is never returned; the caller is told.
    """

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        self.problems = list(problems or [])
        self.message = message
        super().__init__(message)


# --- Phase 2 placeholders -------------------------------------------------
# Raised only once those layers exist. Declared now so that error handling in
# the service layer is already total.


class ProviderUnavailable(CreaError):
    """A provider is not usable (missing key, disabled, or upstream error)."""


class AccessDenied(CreaError):
    """Content exists but may not legally be accessed in this context."""


# --- provider error taxonomy ----------------------------------------------
# Kept distinct so a caller can tell "we asked wrongly" from "we are being
# throttled" from "they broke". Collapsing them into one failure mode would
# make a 429 indistinguishable from a typo, and the first needs backoff while
# the second needs a fix.


class ProviderError(CreaError):
    """Base for anything that went wrong talking to a provider."""

    provider: str = ""
    http_status: int | None = None

    def __init__(self, message: str, provider: str = "", http_status: int | None = None,
                 retry_after: float | None = None):
        self.provider = provider
        self.http_status = http_status
        #: The server's own Retry-After, in seconds, when it sent one. A caller
        #: that decides to wait reads this rather than guessing a backoff.
        self.retry_after = retry_after
        super().__init__(message)


class InvalidRequest(ProviderError):
    """HTTP 400 -- our query was wrong."""


class AuthenticationOrAuthorizationError(ProviderError):
    """HTTP 401 / 403 -- our credential was rejected or lacks rights."""


class RateLimited(ProviderError):
    """HTTP 429 -- budget exhausted or request rate too high.

    Distinct from every other failure because it is the one case where the
    correct response is to wait rather than to change anything. OpenAlex
    documents 429 for both "over your daily budget" and "over 100 requests per
    second"; the message says which surface it came from when the body says so.
    """


class ResourceNotFound(ProviderError):
    """HTTP 404 -- the resource genuinely does not exist.

    Its own type on purpose. "This DOI is not in Crossref" is an answer, and
    folding it into UPSTREAM_ERROR would report a correct lookup as an outage.
    """


class UpstreamError(ProviderError):
    """HTTP 5xx."""


class InvalidUpstreamResponse(ProviderError):
    """The body was not the JSON we expected."""


class UpstreamSchemaError(ProviderError):
    """Valid JSON, but not the shape the contract describes."""


class EvidenceIntegrityError(CreaError):
    """A Claim does not resolve against the evidence it cites.

    Raised by the validator, never by the models. A Claim with an empty
    `evidence_refs` cannot be constructed at all -- that is a type-level
    guarantee -- but a Claim naming evidence nobody holds can, and has to be
    caught.
    """


class DocumentError(CreaError):
    """Something is wrong with a local file the user pointed us at.

    Typed by `kind` so a caller can react to "that file is not a PDF" without
    matching on English.
    """

    kind: object = None
    path: str = ""

    def __init__(self, message: str, kind=None, path: str = "") -> None:
        self.kind = kind
        self.path = path
        super().__init__(message)

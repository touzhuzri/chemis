"""Access and admissibility policy. Pure logic, no I/O, independently testable.

Two rules live here and they are the whole point of the module.

**A URL is not an authorisation.** `fulltext_url` being populated says where an
open-access copy is. It says nothing about whether we may retrieve, keep or
redistribute it. The retrieval gate consults `permission`, never the URL, and it
fails closed: anything that is not explicitly permitted is not permitted.

**A classification is not a licence.** OA status is a statement about the
open-access ecosystem. BRONZE in particular means "free to read on the
publisher's site", which is not a copyright licence and not a download
permission. With no explicit licence evidence, permission stays UNKNOWN --
neither MAY_RETRIEVE nor NOT_PERMITTED is inferred from an OA class alone.
"""

from __future__ import annotations

from crea.core.models.literature import (
    AbstractRecord,
    AccessAssessment,
    AccessEvidence,
    AccessStatus,
    FulltextPermission,
    OAStatus,
    PermissionBasis,
    RetrievalAttempt,
    RetrievalOutcome,
)

#: Bumped when the inference rules change meaning. Recorded on every
#: assessment so a decision can be told apart from one made under other rules.
ACCESS_POLICY_VERSION = "access-policy-v1"

#: SPDX ids treated as permitting automated retrieval of the full text. Anything
#: not listed here is UNKNOWN, not forbidden -- the distinction is that UNKNOWN
#: means we have not established permission, and the gate stops either way.
#:
#: `cc0-1.0` is the SPDX identifier for Creative Commons Zero v1.0 Universal,
#: confirmed against the SPDX licence list (spdx.org/licenses/CC0-1.0.html,
#: "Short identifier CC0-1.0", isValid). OpenAlex publishes the same licence
#: under its own vocabulary slug `public-domain`; the provider boundary maps
#: between the two, and the raw provider value is kept in `LicenseInfo.name`.
#: Nothing here is a self-invented identifier.
#:
#: The unversioned `cc-by` / `cc-by-sa` family names are present because some
#: providers (OpenAlex among them) name a licence family without stating which
#: version, and inventing one would be a guess. An unstated version stays
#: unstated.
PERMISSIVE_SPDX = frozenset({
    "cc0",
    "cc0-1.0",
    "cc-by",
    "cc-by-1.0",
    "cc-by-2.0",
    "cc-by-2.5",
    "cc-by-3.0",
    "cc-by-4.0",
    "cc-by-sa-1.0",
    "cc-by-sa-2.0",
    "cc-by-sa-2.5",
    "cc-by-sa-3.0",
    "cc-by-sa-4.0",
})

#: OA statuses whose work is open enough that an explicit, permissive licence on
#: the record would let us retrieve it. Membership grants NOTHING by itself --
#: `infer_permission` still requires `license_is_permissive`. Listed here only
#: so that a licence, where one exists, is not ignored.
#:
#: DIAMOND is a member for that reason alone. Diamond OA is a business model
#: (no fees to authors or readers), not a copyright licence, so a diamond
#: record with no licence still resolves to UNKNOWN.
_OA_WITH_LICENCE = frozenset({
    OAStatus.GOLD, OAStatus.GREEN, OAStatus.HYBRID, OAStatus.DIAMOND,
})


def license_is_permissive(license_info) -> bool:
    """True only on explicit, named evidence. A blank licence is never permissive."""
    if not license_info.is_known:
        return False
    if not license_info.spdx_id:
        return False
    return license_info.spdx_id.strip().lower() in PERMISSIVE_SPDX


def _evidence(kind: str, detail: str, assessment: AccessAssessment) -> AccessEvidence:
    return AccessEvidence(
        kind=kind, detail=detail,
        source=assessment.source or "policy",
        retrieved_at=assessment.checked_at or "")


def infer_permission(assessment: AccessAssessment) -> AccessAssessment:
    """Fill in `permission` and `permission_basis` from what is actually known.

    Returns a new assessment; the input is not mutated. Note that BRONZE and
    UNKNOWN both land on UNKNOWN permission: an OA classification is not a
    licence, and inferring either way from one would be an invention.

    Stage 4.3 also records `policy_version`, the human-readable `reasons`, and
    the structured `access_evidence` -- the *decision* is unchanged and stays
    fail-closed; only the explanation is added.
    """
    oa = assessment.oa_status
    lic = assessment.license
    evidence: list[AccessEvidence] = []
    reasons: list[str] = []

    def finish(update: dict, *, first_evidence: AccessEvidence,
               first_reason: str) -> AccessAssessment:
        return assessment.model_copy(update={
            "policy_version": ACCESS_POLICY_VERSION,
            "reasons": reasons + [first_reason],
            "access_evidence": evidence + [first_evidence],
            **update,
        })

    if oa is OAStatus.CLOSED:
        return finish(
            {"permission": FulltextPermission.NOT_PERMITTED,
             "permission_basis": PermissionBasis.OA_STATUS_CLOSED},
            first_evidence=_evidence("closed", "the work is known to be closed", assessment),
            first_reason="OA status is closed; retrieval is not permitted")

    if oa in _OA_WITH_LICENCE:
        if license_is_permissive(lic):
            return finish(
                {"permission": FulltextPermission.MAY_RETRIEVE,
                 "permission_basis": PermissionBasis.LICENSE,
                 "access_status": AccessStatus.AVAILABLE},
                first_evidence=_evidence(
                    "open_license",
                    f"permissive licence {lic.spdx_id!r} stated by {lic.stated_by or ['?']}",
                    assessment),
                first_reason=(
                    f"an explicit permissive licence ({lic.spdx_id}) applies and "
                    f"the OA status ({oa.value}) places it on a work we may retrieve"))
        return finish(
            {"permission": FulltextPermission.UNKNOWN,
             "permission_basis": PermissionBasis.OA_STATUS_ONLY},
            first_evidence=_evidence(
                "oa_status", f"OA status {oa.value} with no established licence", assessment),
            first_reason=(
                f"OA status is {oa.value} but no explicit, applicable licence was "
                f"established; permission stays unestablished"))

    # BRONZE, UNKNOWN, and any status added later without a policy entry.
    url_note = ("a full-text location is recorded, but a URL is not a permission"
                if assessment.fulltext_url else "no full-text location is recorded")
    return finish(
        {"permission": FulltextPermission.UNKNOWN,
         "permission_basis": PermissionBasis.OA_STATUS_ONLY
         if oa is not OAStatus.UNKNOWN
         else PermissionBasis.UNKNOWN},
        first_evidence=_evidence("url_only" if assessment.fulltext_url else "oa_status",
                                 url_note, assessment),
        first_reason=(
            f"OA status is {oa.value or 'unknown'} and no explicit, applicable "
            f"licence was established; {url_note}"))


def may_retrieve(assessment: AccessAssessment) -> bool:
    """The retrieval gate. Fail-closed: only an explicit MAY_RETRIEVE passes.

    Deliberately insensitive to `fulltext_url`. A resolvable OpenAlex
    `best_oa_location.url` is a location, not a permission, and a bronze OA
    article has one.
    """
    return assessment.permission is FulltextPermission.MAY_RETRIEVE


def gate_retrieval(assessment: AccessAssessment, paper_id: str, at: str) -> RetrievalAttempt:
    """Produce the outcome a retrieval attempt *would* have, without any I/O.

    The gate is expected to produce PERMISSION_DENIED as its normal result. It
    is not an error path, and it must not be the thing that discovers a network
    problem -- it runs before any network call exists.
    """
    if may_retrieve(assessment):
        return RetrievalAttempt(
            paper_id=paper_id,
            attempted_at=at,
            outcome=RetrievalOutcome.NOT_ATTEMPTED,
            note=(
                f"permission granted on basis "
                f"{assessment.permission_basis.value}; a transport is still required"
            ),
        )
    if not assessment.fulltext_url:
        return RetrievalAttempt(
            paper_id=paper_id,
            attempted_at=at,
            outcome=RetrievalOutcome.NO_URL,
            note="no full-text location recorded",
        )
    return RetrievalAttempt(
        paper_id=paper_id,
        attempted_at=at,
        outcome=RetrievalOutcome.PERMISSION_DENIED,
        note=(
            f"permission is {assessment.permission.value} "
            f"(basis {assessment.permission_basis.value}); "
            "a full-text URL being present does not grant retrieval"
        ),
    )


# ---------------------------------------------------------------------------
# abstract admissibility
# ---------------------------------------------------------------------------


def abstract_admissible_as_primary_evidence(record: AbstractRecord) -> bool:
    """May this abstract be leaned on as *primary* evidence?

    A reconstructed abstract is a positional reconstruction of an inverted
    index, not text anyone wrote in that order. It is entirely suitable for
    search, ranking and screening -- that is what it is for -- and it is not
    suitable as a primary source for a chemical fact.

    This is an admissibility question about *this* record, not a blanket rule
    that every Evidence must come from a document. Evidence is expected to
    distinguish its own strengths (see `EvidenceStrength`); a future phase may
    legitimately admit a metadata-derived claim as SECONDARY_METADATA_EVIDENCE.
    What remains true in every case is that a chemical fact ultimately needs a
    provenance chain and a `raw_quote` findable verbatim in its source.
    """
    return not record.is_reconstructed

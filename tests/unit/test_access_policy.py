"""Access policy. Pure logic, tested without any provider in the picture."""

import pytest

from crea.core.models.literature import (
    AbstractRecord,
    AbstractSource,
    AccessAssessment,
    AccessStatus,
    FulltextPermission,
    LicenseInfo,
    OAStatus,
    PermissionBasis,
    RetrievalOutcome,
)
from crea.core.policy.access import (
    abstract_admissible_as_primary_evidence,
    gate_retrieval,
    infer_permission,
    license_is_permissive,
    may_retrieve,
)


def cc_by() -> LicenseInfo:
    return LicenseInfo(spdx_id="cc-by", name="CC BY 4.0", is_known=True, applies_to="oa_copy")


def unknown_license() -> LicenseInfo:
    return LicenseInfo()


# --- the correction that matters most --------------------------------------


def test_bronze_is_a_classification_not_a_licence():
    a = AccessAssessment(
        oa_status=OAStatus.BRONZE,
        fulltext_url="https://example.org/pdf/3",
        license=unknown_license(),
    )
    out = infer_permission(a)
    # NOT may_retrieve: a classification is not a licence
    assert out.permission is not FulltextPermission.MAY_RETRIEVE
    # NOT not_permitted either: we have not established a prohibition, only an
    # absence of evidence, and pretending otherwise is its own kind of error
    assert out.permission is not FulltextPermission.NOT_PERMITTED
    assert out.permission is FulltextPermission.UNKNOWN
    assert out.permission_basis is PermissionBasis.OA_STATUS_ONLY
    assert may_retrieve(out) is False


# --- the rest of the mapping ------------------------------------------------


@pytest.mark.parametrize("oa", [OAStatus.GOLD, OAStatus.GREEN, OAStatus.HYBRID])
def test_licensed_oa_is_retrievable(oa):
    out = infer_permission(AccessAssessment(oa_status=oa, license=cc_by()))
    assert out.permission is FulltextPermission.MAY_RETRIEVE
    assert out.permission_basis is PermissionBasis.LICENSE
    assert out.access_status is AccessStatus.AVAILABLE


@pytest.mark.parametrize("oa", [OAStatus.GOLD, OAStatus.GREEN, OAStatus.HYBRID])
def test_unlicensed_oa_stays_unknown(oa):
    out = infer_permission(AccessAssessment(oa_status=oa, license=unknown_license()))
    assert out.permission is FulltextPermission.UNKNOWN
    assert out.permission_basis is PermissionBasis.OA_STATUS_ONLY


def test_closed_is_not_permitted():
    out = infer_permission(AccessAssessment(oa_status=OAStatus.CLOSED, license=cc_by()))
    assert out.permission is FulltextPermission.NOT_PERMITTED
    assert out.permission_basis is PermissionBasis.OA_STATUS_CLOSED


def test_unknown_oa_stays_unknown():
    out = infer_permission(AccessAssessment(oa_status=OAStatus.UNKNOWN))
    assert out.permission is FulltextPermission.UNKNOWN
    assert out.permission_basis is PermissionBasis.UNKNOWN


# --- fail-closed ------------------------------------------------------------


def test_a_blank_licence_is_never_permissive():
    lie = LicenseInfo(spdx_id="cc-by", is_known=False)
    assert license_is_permissive(lie) is False
    named = LicenseInfo(spdx_id="cc-by", is_known=True)
    assert license_is_permissive(named) is True
    unnamed = LicenseInfo(is_known=True)
    assert license_is_permissive(unnamed) is False


def test_an_unlisted_spdx_id_is_not_permissive():
    assert license_is_permissive(LicenseInfo(spdx_id="cc-by-nc-4.0", is_known=True)) is False


@pytest.mark.parametrize(
    "assessment",
    [
        AccessAssessment(oa_status=OAStatus.UNKNOWN),
        AccessAssessment(oa_status=OAStatus.BRONZE, fulltext_url="https://example.org/x.pdf"),
        AccessAssessment(oa_status=OAStatus.CLOSED, fulltext_url="https://example.org/x.pdf"),
        AccessAssessment(oa_status=OAStatus.GREEN, fulltext_url="https://example.org/x.pdf"),
    ],
)
def test_the_gate_is_insensitive_to_the_presence_of_a_url(assessment):
    assert assessment.fulltext_url is not None or assessment.oa_status is OAStatus.UNKNOWN
    assert may_retrieve(infer_permission(assessment)) is False


def test_gate_returns_permission_denied_without_any_io():
    a = infer_permission(AccessAssessment(
        oa_status=OAStatus.BRONZE,
        fulltext_url="https://example.org/pdf/3",
        license=unknown_license(),
    ))
    attempt = gate_retrieval(a, "paper-1", "2026-09-30T00:00:00Z")
    assert attempt.outcome is RetrievalOutcome.PERMISSION_DENIED
    assert "does not grant retrieval" in attempt.note


def test_gate_reports_no_url_when_there_is_none():
    a = infer_permission(AccessAssessment(oa_status=OAStatus.CLOSED))
    assert gate_retrieval(a, "p", "t").outcome is RetrievalOutcome.NO_URL


def test_gate_still_withholds_when_permission_is_granted():
    a = infer_permission(AccessAssessment(
        oa_status=OAStatus.GREEN, license=cc_by(), fulltext_url="https://example.org/x.pdf"
    ))
    attempt = gate_retrieval(a, "p", "t")
    assert attempt.outcome is RetrievalOutcome.NOT_ATTEMPTED
    assert "transport is still required" in attempt.note


def test_infer_permission_does_not_mutate_its_input():
    a = AccessAssessment(oa_status=OAStatus.BRONZE)
    infer_permission(a)
    assert a.permission is FulltextPermission.UNKNOWN
    assert a.permission_basis is PermissionBasis.UNKNOWN


# --- abstract admissibility -------------------------------------------------


def test_reconstructed_abstract_is_not_primary_evidence():
    rec = AbstractRecord(
        text="A glucokinase regulator was selected to identify mutations in NIDDK",
        source=AbstractSource.OPENALEX_RECONSTRUCTED,
        retrieved_at="2026-09-30T00:00:00Z",
    )
    assert rec.is_reconstructed is True
    assert abstract_admissible_as_primary_evidence(rec) is False


def test_publisher_supplied_abstract_is_admissible():
    for source in (AbstractSource.PUBLISHER, AbstractSource.CROSSREF, AbstractSource.USER_PROVIDED):
        rec = AbstractRecord(text="x", source=source, retrieved_at="t")
        assert rec.is_reconstructed is False
        assert abstract_admissible_as_primary_evidence(rec) is True


# --- DIAMOND (added after the 2026-09-30 live verification) ---------------
# OpenAlex returns oa_status "diamond" in the wild. It arrived with no rule
# attached, and the correct rule is the same one every other OA status already
# follows: the status is a description, the licence is the evidence.


def test_diamond_is_a_recognised_oa_status():
    assert OAStatus("diamond") is OAStatus.DIAMOND


def test_diamond_alone_grants_nothing():
    out = infer_permission(AccessAssessment(oa_status=OAStatus.DIAMOND, license=unknown_license()))
    assert out.permission is FulltextPermission.UNKNOWN
    assert out.permission is not FulltextPermission.MAY_RETRIEVE
    assert out.permission is not FulltextPermission.NOT_PERMITTED
    assert out.permission_basis is PermissionBasis.OA_STATUS_ONLY
    assert may_retrieve(out) is False


def test_diamond_with_an_explicit_licence_uses_the_licence_not_the_status():
    out = infer_permission(AccessAssessment(oa_status=OAStatus.DIAMOND, license=cc_by()))
    assert out.permission is FulltextPermission.MAY_RETRIEVE
    # the basis records *why*, and it is the licence
    assert out.permission_basis is PermissionBasis.LICENSE


def test_diamond_with_a_pdf_url_and_no_licence_still_denies():
    out = infer_permission(AccessAssessment(
        oa_status=OAStatus.DIAMOND,
        fulltext_url="https://example.org/pdf/3",
        license=unknown_license(),
    ))
    assert may_retrieve(out) is False
    assert gate_retrieval(out, "p", "t").outcome is RetrievalOutcome.PERMISSION_DENIED


def test_oa_status_and_permission_stay_independent():
    """Every OA status must resolve to a permission on its own evidence only."""
    seen = set()
    for status in OAStatus:
        out = infer_permission(AccessAssessment(oa_status=status))
        assert out.oa_status is status
        assert out.permission in set(FulltextPermission)
        seen.add((status, out.permission))
    # no OA status, on its own, ever reaches a permission
    assert all(p is not FulltextPermission.MAY_RETRIEVE for _s, p in seen)

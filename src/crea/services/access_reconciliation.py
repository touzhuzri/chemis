"""Access reconciliation: what evidence do we hold, and is it enough?

Three providers each report some part of the access picture, and none of them
reports all of it. The question this module answers is not "which provider
wins". It is:

    what independent evidence is in front of us, where do they disagree, and
    does the whole of it amount to permission to retrieve a full text?

The rules it applies are the frozen ones from
`docs/PHASE2_PROVIDER_CONTRACT.md` §6, and the direction of every one of them
is fail-closed:

* `MAY_RETRIEVE` requires an explicitly permissive licence, applicable to what
  we would retrieve. Nothing else qualifies.
* `UNKNOWN` is a refusal to act without evidence. It is never upgraded to
  `NOT_PERMITTED`, because we have not established a prohibition.
* `NOT_PERMITTED` requires evidence that retrieval is refused.

One asymmetry is deliberate and worth stating: **absence of a claim does not
overturn a claim.** If one provider reports a permissive licence and another
reports nothing, the licence stands. If one provider reports a URL and another
reports no URL, the URL does not become permission. Knowing less is not
knowing the opposite.
"""

from __future__ import annotations

from crea.core.models.literature import (
    AccessAssessment,
    AccessStatus,
    FulltextPermission,
    LicenseInfo,
    OAStatus,
)
from crea.core.models.merge import (
    AccessConflict,
    AccessReconciliation,
    ReconciliationInput,
)
from crea.core.policy.access import PERMISSIVE_SPDX

POLICY_VERSION = "access-v1"

#: Which provider's assessment to carry when several qualify. Unpaywall is the
#: access provider and is first; OpenAlex classifies OA status; Crossref makes
#: no access claim at all. Ties are broken here, never by argument order.
_SOURCE_PRIORITY = ("unpaywall", "openalex", "crossref", "local_document")

#: `content-version` values that describe a *narrow grant* rather than a
#: licence over the work. A text-and-data-mining grant is not permission to
#: retain a full text, whatever URL it points at.
RESTRICTED_CONTENT_VERSIONS = frozenset({"tdm", "am", "similar-check"})


def _is_permissive(item: ReconciliationInput) -> bool:
    """An explicit, permissive licence -- and nothing weaker.

    A record whose licence is scoped to text and data mining is not permissive
    no matter what its URL says. `spdx_id` is already empty for those, and this
    check is the belt to that braces.
    """
    if not item.license_spdx:
        return False
    if str(item.license_content_version).lower() in RESTRICTED_CONTENT_VERSIONS:
        return False
    return item.license_spdx.strip().lower() in PERMISSIVE_SPDX


def _known_oa(evidence: list[ReconciliationInput]) -> bool:
    return any(
        e.oa_status not in ("unknown", "", "closed") for e in evidence
    )


def reconcile(
    evidence: list[ReconciliationInput],
    assessment_of: dict[str, AccessAssessment] | None = None,
) -> AccessReconciliation:
    """Weigh several providers' access claims into one assessment.

    `assessment_of` supplies each provider's own `AccessAssessment` so the
    selected record can be carried through verbatim rather than reconstructed.
    """
    ordered = sorted(
        evidence,
        key=lambda e: (_SOURCE_PRIORITY.index(e.source)
                       if e.source in _SOURCE_PRIORITY else len(_SOURCE_PRIORITY),
                       e.source),
    )
    result = AccessReconciliation(evidence=list(ordered),
                                  policy_version=POLICY_VERSION)

    if not ordered:
        result.selected_assessment = AccessAssessment().model_dump(mode="json")
        result.permission_basis = "no access evidence was available"
        result.decisive = "none"
        return result

    # --- conflicts, recorded before anything is decided --------------------
    statuses = {e.source: e.oa_status for e in ordered}
    if len(set(statuses.values())) > 1:
        result.conflicts.append(AccessConflict(
            field="oa_status",
            description="providers disagree on the open-access status",
            values=statuses,
        ))

    licences = {e.source: (e.license_spdx or e.license_name or "UNKNOWN")
                for e in ordered}
    if len(set(licences.values())) > 1:
        result.conflicts.append(AccessConflict(
            field="license",
            description="providers disagree on the licence",
            values=licences,
        ))

    locations = {e.source: ("yes" if e.fulltext_url else "no") for e in ordered}
    if len(set(locations.values())) > 1:
        result.conflicts.append(AccessConflict(
            field="fulltext_url",
            description="providers disagree on whether a full-text location is known",
            values=locations,
        ))

    has_permissive = [e for e in ordered if _is_permissive(e)]
    has_url = [e for e in ordered if e.fulltext_url]
    all_closed = bool(ordered) and all(
        e.oa_status == OAStatus.CLOSED.value for e in ordered)

    # --- the decision -------------------------------------------------------
    if has_permissive:
        selected = has_permissive[0]
        result.selected_source = selected.source
        result.decisive = (
            f"explicit permissive licence {selected.license_spdx} reported by "
            f"{selected.source}"
        )
        if not _known_oa(ordered) and not selected.fulltext_url:
            # A permissive licence on a record that also claims to be closed is
            # worth surfacing rather than resolving silently.
            result.conflicts.append(AccessConflict(
                field="licence_vs_status",
                description=("a permissive licence was reported on a record no "
                             "provider calls open access"),
                values={selected.source: selected.license_spdx or "UNKNOWN"},
            ))
        basis = "explicit permissive licence on the record"
    elif all_closed:
        selected = ordered[0]
        result.selected_source = selected.source
        result.decisive = f"every provider reports the work as closed"
        basis = "every provider reports the work as closed"
    else:
        selected = (has_url[0] if has_url else ordered[0])
        result.selected_source = selected.source
        result.decisive = (
            f"no permissive licence in any provider's evidence "
            f"({len(ordered)} source(s) considered)"
        )
        basis = "no permissive licence was reported by any source"

    result.permission_basis = basis

    assessment = (assessment_of or {}).get(result.selected_source)
    if assessment is None:
        assessment = AccessAssessment(
            oa_status=OAStatus(selected.oa_status)
            if selected.oa_status in {s.value for s in OAStatus}
            else OAStatus.UNKNOWN,
            fulltext_url=selected.fulltext_url,
            url_type=selected.url_type,
            license=LicenseInfo(
                spdx_id=selected.license_spdx,
                name=selected.license_name,
                url=selected.license_url,
                content_version=selected.license_content_version,
                applies_to="oa_copy",
                is_known=bool(selected.license_spdx),
                stated_by=[selected.source],
            ),
            notes=list(selected.notes),
        )

    # The permission is policy's answer to the reconciled evidence, and only
    # the permission -- the fields stay as the selected provider reported them.
    from crea.core.policy.access import infer_permission

    assessment = infer_permission(assessment)
    if not has_permissive and not all_closed:
        # Belt and braces: this layer never relaxes the policy, so anything
        # short of a permissive licence lands on UNKNOWN whatever the selected
        # assessment happened to carry.
        assessment = assessment.model_copy(update={
            "permission": FulltextPermission.UNKNOWN,
            "access_status": (AccessStatus.AVAILABLE
                              if has_url else AccessStatus.UNKNOWN),
        })
    result.selected_assessment = assessment.model_dump(mode="json")
    return result


def evidence_from_paper(paper) -> ReconciliationInput:
    """Build one reconciliation input from a provider-parsed `Paper`.

    The raw upstream status string is carried alongside the mapped one, so an
    unmodelled value stays visible after the fact.
    """
    # Read from the structured field. Notes are for humans; a value that only
    # lives in a note is one wording change away from a silent bug.
    raw = paper.access.oa_status_raw
    return ReconciliationInput(
        source=paper.sources[0] if paper.sources else "unknown",
        source_id=paper.source_ids.get(paper.sources[0], "") if paper.sources else "",
        oa_status=paper.access.oa_status.value,
        oa_status_raw=raw or paper.access.oa_status.value,
        license_spdx=paper.access.license.spdx_id,
        license_name=paper.access.license.name,
        license_url=paper.access.license.url,
        license_content_version=paper.access.license.content_version,
        fulltext_url=paper.access.fulltext_url,
        url_type=paper.access.url_type,
        notes=list(paper.access.notes),
    )

"""Cross-provider merge: field-level, provenance-preserving.

Two rules shape this module.

**Identity is decided before anything else, and only by DOI.** If two records
normalise to the same DOI they are candidates for the same work; if either has
no DOI, this layer does not guess. Title similarity already exists in
`services.dedup`, with its own screen, its own four gates and its own audit
trail. A second, looser identity algorithm inside the merge would be a way for
two records to silently become one without anyone deciding so.

**A field is resolved, not overwritten.** Every resolution records the selected
value, every source that offered it, everything else on the table, and whether
the sources disagreed. A merge that leaves `title = "..."` and no explanation is
not a merge; it is a deletion with extra steps.

The input order never affects the output. Priority comes from the policy table,
never from the order records happen to arrive in.

Access *assessment* is deliberately not decided here. This layer selects which
reported access record to carry and records the disagreement; turning evidence
into a permission is `core.policy`'s job, and reconciling several providers'
evidence is the access-reconciliation layer's.
"""

from __future__ import annotations

from crea.core.ids import normalize_doi
from crea.core.models.literature import Paper
from crea.core.models.merge import (
    FieldAlternative,
    FieldResolution,
    FieldState,
    IdentityStatus,
    MergePolicy,
    MergeResult,
    ProviderRecord,
)
from crea.core.provenance import utc_now_iso

#: Per-field source priority. There is no global ordering, on purpose: Crossref
#: is the DOI registration agency and is the better record of a title, a
#: journal and a publisher; neither source is authoritative about the other.
#: A single global rule would encode one provider's trustworthiness where it
#: has none.
DEFAULT_POLICY = MergePolicy(
    version="merge-v1",
    field_priority={
        "title": ("crossref", "openalex", "unpaywall"),
        "journal": ("crossref", "openalex"),
        "year": ("crossref", "openalex"),
        "publication_date": ("crossref", "openalex"),
        "publication_date_type": ("crossref", "openalex"),
        "authors": ("crossref", "openalex"),
        "access": ("unpaywall", "openalex", "crossref"),
        "url": ("crossref", "openalex"),
        "publisher": ("crossref",),
        "type": ("crossref",),
        "volume": ("crossref",),
        "issue": ("crossref",),
        "pages": ("crossref",),
        "issn": ("crossref", "openalex"),
        "upstream_notes": ("crossref", "openalex", "unpaywall"),
        "identifiers": ("crossref", "openalex", "unpaywall"),
    },
)

_EMPTY = (None, "", [], {})


def _render(value) -> str:
    """Human-readable rendering, for display only. Storage copies raw values."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_render(v) for v in value)
    name = getattr(value, "name", None)
    if name is not None and hasattr(value, "family"):
        return str(name) or " ".join(
            p for p in (value.given, value.family) if p) or str(value)
    return str(value)


def _upstream(paper: Paper, attribute: str):
    return getattr(paper.upstream, attribute, None)


def _value(paper: Paper, field_name: str, attribute: str | None, upstream: bool):
    return _upstream(paper, field_name) if upstream else getattr(paper, attribute, None)


def _ordered(papers: list[Paper], policy: MergePolicy, field_name: str) -> list[Paper]:
    """Deterministic order: policy rank, then source name, then paper id.

    Never the caller's list order. Two calls that pass the same records in
    opposite orders must select the same value, or the merge result is a
    function of the call site rather than of the evidence.
    """
    unranked = len(policy.field_priority) + 1

    def key(paper: Paper):
        source = paper.sources[0] if paper.sources else ""
        rank = policy.rank(field_name, source)
        return (rank if rank is not None else unranked, source, paper.paper_id)

    return sorted(papers, key=key)


def _offers(papers: list[Paper], policy: MergePolicy, field_name: str,
            attribute: str | None, upstream: bool) -> dict[str, list[str]]:
    """value -> every source that offered it, in policy order."""
    out: dict[str, list[str]] = {}
    for paper in _ordered(papers, policy, field_name):
        value = _value(paper, field_name, attribute, upstream)
        if value in _EMPTY:
            continue
        source = paper.sources[0] if paper.sources else "unknown"
        rendered = _render(value)
        out.setdefault(rendered, [])
        if source not in out[rendered]:
            out[rendered].append(source)
    return out


def _resolve(papers: list[Paper], policy: MergePolicy, field_name: str,
             attribute: str | None, upstream: bool = False) -> FieldResolution:
    """Resolve one field by preference. Never raises; absence is a state."""
    offered = _offers(papers, policy, field_name, attribute, upstream)
    if not offered:
        return FieldResolution(field=field_name, state=FieldState.MISSING)

    chosen: Paper | None = None
    for paper in _ordered(papers, policy, field_name):
        if _value(paper, field_name, attribute, upstream) not in _EMPTY:
            chosen = paper
            break
    if chosen is None:  # pragma: no cover - _offers already proved otherwise
        return FieldResolution(field=field_name, state=FieldState.MISSING)

    selected = _render(_value(chosen, field_name, attribute, upstream))
    same_sources = offered[selected]
    others = [FieldAlternative(value=v, source=s)
              for v, sources in offered.items() if v != selected for s in sources[:1]]

    return FieldResolution(
        field=field_name,
        state=FieldState.CONFLICT if others else FieldState.AGREED,
        selected=selected,
        selected_from=sorted(same_sources),
        alternatives=others,
        conflict="; ".join(f"{a.source}: {a.value}" for a in others),
    )


def _resolve_union(papers: list[Paper], policy: MergePolicy, field_name: str,
                   attribute: str) -> FieldResolution:
    """A field we deliberately combine rather than choose between."""
    values: list[str] = []
    sources: list[str] = []
    for paper in _ordered(papers, policy, field_name):
        value = getattr(paper, attribute, None)
        if value in _EMPTY:
            continue
        source = paper.sources[0] if paper.sources else "unknown"
        if source not in sources:
            sources.append(source)
        for item in (value if isinstance(value, (list, tuple)) else [value]):
            text = str(item)
            if text not in values:
                values.append(text)

    if not values:
        return FieldResolution(field=field_name, state=FieldState.MISSING)
    if len(sources) <= 1:
        return FieldResolution(field=field_name, state=FieldState.AGREED,
                               selected=", ".join(values), selected_from=sources)
    return FieldResolution(field=field_name, state=FieldState.UNIONED,
                           selected=", ".join(values), selected_from=sorted(sources))


#: (resolution name, Paper attribute or None when it lives on UpstreamFacts)
_PREFERRED_FIELDS = (
    ("title", "title"),
    ("journal", "journal"),
    ("year", "year"),
    ("url", "url"),
    ("volume", "volume"),
    ("issue", "issue"),
    ("pages", "pages"),
    ("publisher", None),
    ("type", None),
    ("publication_date", None),
    ("publication_date_type", None),
    ("access", None),
)


def _merge_abstracts(papers: list[Paper], policy: MergePolicy) -> list:
    """Every distinct abstract, each keeping its own source and its own tag.

    Two abstracts are never concatenated. A Crossref abstract is a publisher
    deposit; an OpenAlex one is a positional reconstruction of an inverted
    index. They are different objects, and averaging them would destroy the
    only property that distinguishes them.
    """
    out = []
    seen: set[tuple[str, str]] = set()
    for paper in _ordered(papers, policy, "abstract"):
        for record in paper.abstracts:
            key = (record.source.value, record.text)
            if key in seen:
                continue
            seen.add(key)
            out.append(record)
    return out


def classify_identity(papers: list[Paper]) -> tuple[IdentityStatus, str, str]:
    """Decide whether these records are the same work. DOI only."""
    dois = [normalize_doi(p.identifiers.doi) for p in papers]
    present = [d for d in dois if d]

    if not present:
        return IdentityStatus.NO_DOI, "no record carries a DOI", ""
    if len(papers) == 1:
        return (IdentityStatus.DOI_MATCH if present else IdentityStatus.NO_DOI,
                f"single record, doi={present[0]}", "")
    if len(set(present)) > 1:
        return (IdentityStatus.DOI_CONFLICT,
                "records carry different DOIs and are not the same work",
                "; ".join(sorted(set(present))))
    if len(present) != len(papers):
        missing = sorted(
            p.sources[0] if p.sources else "unknown"
            for p, d in zip(papers, dois) if not d)
        return (IdentityStatus.IDENTITY_UNKNOWN,
                f"doi={present[0]} but no DOI from: {', '.join(missing)}", "")
    return (IdentityStatus.DOI_MATCH,
            f"all records normalise to doi={present[0]}", "")


def merge_papers(
    papers: list[Paper],
    policy: MergePolicy = DEFAULT_POLICY,
) -> MergeResult:
    """Merge records of one work. Caller order is irrelevant to the result."""
    version = policy.version
    if not papers:
        return MergeResult(policy_version=version)

    papers = sorted(papers, key=lambda p: (p.sources[0] if p.sources else "",
                                           p.paper_id))
    identity, basis, identity_conflict = classify_identity(papers)

    provider_records = [
        ProviderRecord(
            source=p.sources[0] if p.sources else "unknown",
            source_id=p.source_ids.get(p.sources[0], "") if p.sources else "",
            paper_id=p.paper_id,
            retrieved_at=p.retrieved_at,
            raw_hashes=dict(p.raw_hashes),
            doi=normalize_doi(p.identifiers.doi),
        )
        for p in papers
    ]
    author_lists = {p.sources[0] if p.sources else "unknown": p.authors
                    for p in papers}

    if identity is not IdentityStatus.DOI_MATCH or len(papers) < 2:
        # Not an identity match. Nothing is combined: the records are handed
        # back as they came, with their identity honestly described.
        return MergeResult(
            identity=identity,
            identity_basis=basis,
            identity_conflict=identity_conflict,
            paper=papers[0],
            field_resolutions=[],
            provider_records=provider_records,
            author_lists=author_lists,
            conflicts=[identity_conflict] if identity_conflict else [],
            policy_version=version,
            merged=False,
        )

    resolutions: list[FieldResolution] = []
    conflicts: list[str] = []

    doi_res = FieldResolution(
        field="doi",
        state=FieldState.AGREED,
        selected=normalize_doi(papers[0].identifiers.doi),
        selected_from=sorted({p.sources[0] for p in papers if p.sources}),
    )
    resolutions.append(doi_res)

    base = _ordered(papers, policy, "title")[-1]
    merged = base.model_copy(deep=True)
    merged.identifiers.doi = doi_res.selected
    merged.paper_id = doi_res.selected or merged.paper_id

    for field_name, attribute in _PREFERRED_FIELDS:
        if field_name == "access":
            # Which reported assessment to carry, and the fact that they
            # differ. Deciding a permission is not this layer's job.
            res, assessment = _resolve_access(papers, policy)
            resolutions.append(res)
            if res.conflict:
                conflicts.append(f"access: {res.conflict}")
            if assessment is not None:
                merged.access = assessment
            continue

        upstream = attribute is None
        res = _resolve(papers, policy, field_name, attribute, upstream)
        resolutions.append(res)
        if res.state is FieldState.MISSING:
            continue
        if res.conflict:
            conflicts.append(f"{field_name}: {res.conflict}")

        chosen = next(
            (p for p in _ordered(papers, policy, field_name)
             if _value(p, field_name, attribute, upstream) not in _EMPTY), None)
        if chosen is None:
            continue
        if upstream:
            setattr(merged.upstream, field_name,
                    _upstream(chosen, field_name))
        else:
            setattr(merged, attribute, _value(chosen, field_name, attribute, upstream))

    # Per-source observations are combined, not chosen between. A note such as
    # "the deposit date was not used as the publication date" belongs to the
    # record that made it, and dropping it because another source was
    # preferred would lose the reason the value looks the way it does.
    note_lines: list[str] = []
    note_sources: list[str] = []
    for paper in _ordered(papers, policy, "upstream_notes"):
        source = paper.sources[0] if paper.sources else "unknown"
        if source not in note_sources:
            note_sources.append(source)
        for note in paper.upstream.notes:
            if note not in note_lines:
                note_lines.append(note)
    merged.upstream.notes = note_lines
    resolutions.append(FieldResolution(
        field="upstream_notes",
        state=FieldState.UNIONED if note_lines else FieldState.MISSING,
        selected=f"{len(note_lines)} note(s) retained",
        selected_from=sorted(note_sources),
    ))

    # Raw publication dates and the policy version that chose among them are
    # provenance about a decision, not competing values. They travel with the
    # record, and the values that were not selected stay visible.
    raw_dates: dict[str, str] = {}
    for paper in _ordered(papers, policy, "publication_date"):
        raw_dates.update(paper.upstream.publication_dates_raw)
    merged.upstream.publication_dates_raw = raw_dates
    merged.upstream.date_policy_version = next(
        (p.upstream.date_policy_version for p in _ordered(papers, policy, "publication_date")
         if p.upstream.date_policy_version), None)
    resolutions.append(FieldResolution(
        field="publication_dates_raw",
        state=FieldState.UNIONED if raw_dates else FieldState.MISSING,
        selected=f"{len(raw_dates)} raw date(s) retained",
        selected_from=sorted({p.sources[0] for p in papers
                              if p.sources and p.upstream.publication_dates_raw}),
    ))

    issn_res = _resolve_union(papers, policy, "issn", "issn")
    resolutions.append(issn_res)
    if issn_res.selected:
        merged.upstream.issn = [x.strip() for x in issn_res.selected.split(", ")
                                if x.strip()]

    author_lists = {p.sources[0] if p.sources else "unknown": p.authors
                    for p in papers}
    authors_res = _resolve(papers, policy, "authors", "authors")
    resolutions.append(authors_res)
    if authors_res.conflict:
        conflicts.append(
            f"authors: {authors_res.conflict} (source-specific lists preserved)")
    merged.authors = next(
        (p.authors for p in _ordered(papers, policy, "authors") if p.authors), [])

    abstract_res = FieldResolution(
        field="abstract",
        state=FieldState.MISSING if not any(p.abstracts for p in papers)
        else FieldState.UNIONED,
        selected="",
        selected_from=sorted({r.source.value for p in papers for r in p.abstracts}),
    )
    abstracts = _merge_abstracts(papers, policy)
    if len(abstracts) > 1:
        abstract_res.alternatives = [
            FieldAlternative(
                value=f"{r.source.value}{' (reconstructed)' if r.is_reconstructed else ''}",
                source=r.source.value,
                note="kept as a separate record; abstracts are never combined",
            )
            for r in abstracts
        ]
        abstract_res.conflict = "multiple distinct abstracts retained"
        if len({r.source.value for r in abstracts}) > 1:
            abstract_res.conflict += (
                "; one is a reconstruction and is not primary evidence")
    resolutions.append(abstract_res)
    merged.abstracts = abstracts

    id_res = _resolve_union(papers, policy, "identifiers", "identifiers")
    resolutions.append(id_res)
    for field in ("openalex_id", "pmid", "pmcid", "mag_id", "isbn"):
        setattr(merged.identifiers, field, next(
            (getattr(p.identifiers, field) for p in papers
             if getattr(p.identifiers, field)), None))

    merged.sources = sorted({s for p in papers for s in p.sources})
    merged.source_ids = {p.sources[0]: p.source_ids.get(p.sources[0], "")
                         for p in papers if p.sources}
    merged.raw_hashes = {k: v for p in papers for k, v in p.raw_hashes.items()}
    merged.merge_conflicts = sorted(set(conflicts))
    merged.retrieved_at = utc_now_iso()

    return MergeResult(
        identity=identity,
        identity_basis=basis,
        identity_conflict=identity_conflict,
        paper=merged,
        field_resolutions=resolutions,
        provider_records=provider_records,
        author_lists=author_lists,
        conflicts=merged.merge_conflicts,
        policy_version=version,
        merged=True,
    )


def _resolve_access(papers: list[Paper], policy: MergePolicy
                    ) -> tuple[FieldResolution, object | None]:
    """Pick which reported access assessment to carry, and record the rest.

    The permission itself is left exactly as the provider's policy pass left
    it. This layer selects a *record*; `core.policy` interprets it, and the
    reconciliation layer weighs several providers together.
    """
    ordered = _ordered(papers, policy, "access")
    reported = [(p, p.access) for p in ordered if p.access is not None]
    if not reported:
        return FieldResolution(field="access", state=FieldState.MISSING), None

    chosen_paper, chosen = reported[0]
    source = chosen_paper.sources[0] if chosen_paper.sources else "unknown"

    descriptions = {}
    for paper, access in reported:
        s = paper.sources[0] if paper.sources else "unknown"
        descriptions[s] = (
            f"oa={access.oa_status.value} "
            f"license={access.license.spdx_id or access.license.name} "
            f"url={'yes' if access.fulltext_url else 'none'}"
        )
    distinct = sorted(set(descriptions.values()))
    if len(distinct) > 1:
        conflict = "; ".join(f"{s}: {d}" for s, d in sorted(descriptions.items()))
    else:
        conflict = ""

    res = FieldResolution(
        field="access",
        state=FieldState.CONFLICT if conflict else FieldState.AGREED,
        selected=descriptions[source],
        selected_from=[source],
        conflict=conflict,
    )
    return res, chosen


def merge_groups(
    groups: dict[str, list[Paper]],
    policy: MergePolicy = DEFAULT_POLICY,
) -> list[MergeResult]:
    """Merge each identity group.

    Grouping is the caller's business; combining is ours. A group of one still
    produces a result, so "one record per DOI" and "three records per DOI"
    answer the same shape of question.
    """
    return [merge_papers(papers, policy=policy)
            for _key, papers in sorted(groups.items())]

"""DOI and title deduplication as an explicit state machine.

The shape of the decision is the design. Fuzzy matching never merges on a
similarity number alone: a pair that survives the screen becomes a
`DUPLICATE_CANDIDATE`, the individual checks are evaluated, and the outcome is
recorded per gate. When the evidence is thin the answer is KEEP_SEPARATE,
because two papers wrongly merged is a worse failure than two papers wrongly
left apart -- the first fabricates a record, the second merely wastes a click.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from crea.core.ids import content_hash
from crea.core.ids import normalize_doi as _normalize_doi
from crea.core.models.dedup import (
    DedupDecision,
    DedupPass,
    DedupResult,
    DedupReason,
    DedupStats,
    DedupThresholds,
    DedupVerdict,
    GateStatus,
)
from crea.core.models.literature import Paper

#: Merge precedence across sources. Crossref is the registration agency and is
#: therefore the metadata authority; a discovery index fills the gaps. Every
#: disagreement is appended to `merge_conflicts` rather than silently resolved.
SOURCE_PRECEDENCE: tuple[str, ...] = ("crossref", "openalex", "unpaywall", "local_document")

_WS = re.compile(r"\s+")
_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+")
_BRACKETED = re.compile(r"[\[\(].*?[\]\)]\s*$")


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------


def normalize_doi(value: str | None) -> str | None:
    """Delegate to the canonical normaliser in `core.ids`.

    The implementation lives there because providers need the identical
    answer, and a DOI that normalises one way at the provider and another way
    in the dedup key is one work split into two.
    """
    return _normalize_doi(value)


def normalize_title(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = _LEADING_ARTICLE.sub("", text)
    text = _BRACKETED.sub("", text)
    return _WS.sub(" ", text).strip()


def normalize_journal(value: str | None) -> str:
    if not value:
        return ""
    return _WS.sub(" ", unicodedata.normalize("NFKD", str(value)).lower()).strip()


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _author_key(author) -> str:
    name = f"{author.family} {author.given}".strip().lower()
    return name or (author.name or "").strip().lower()


def authors_overlap(a: Paper, b: Paper) -> float | None:
    """Jaccard overlap of author keys, or None if either list is empty.

    Auxiliary corroboration only. A shared author across a paper series says
    nothing about identity, so this is never sufficient on its own.
    """
    ka = {_author_key(x) for x in a.authors if _author_key(x)}
    kb = {_author_key(x) for x in b.authors if _author_key(x)}
    if not ka or not kb:
        return None
    return len(ka & kb) / len(ka | kb)


def journal_match(a: Paper, b: Paper) -> bool | None:
    ja, jb = normalize_journal(a.journal), normalize_journal(b.journal)
    if not ja or not jb:
        return None
    return ja == jb


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------


def _title_gate(a: Paper, b: Paper, t: DedupThresholds) -> tuple[GateStatus, float]:
    sim = title_similarity(a.title, b.title)
    return (GateStatus.PASS if sim >= t.title_similarity else GateStatus.FAIL), sim


def _year_gate(a: Paper, b: Paper, t: DedupThresholds) -> tuple[GateStatus, int | None]:
    if a.year is None or b.year is None:
        return GateStatus.UNKNOWN, None
    diff = abs(a.year - b.year)
    return (GateStatus.PASS if diff <= t.year_window else GateStatus.FAIL), diff


def _journal_gate(a: Paper, b: Paper) -> tuple[GateStatus, bool | None]:
    match = journal_match(a, b)
    if match is None:
        return GateStatus.UNKNOWN, None
    return (GateStatus.PASS if match else GateStatus.FAIL), match


def _author_gate(a: Paper, b: Paper) -> tuple[GateStatus, float | None]:
    overlap = authors_overlap(a, b)
    if overlap is None:
        return GateStatus.UNKNOWN, None
    return (GateStatus.PASS if overlap > 0.0 else GateStatus.FAIL), overlap


def screen_pair(
    a: Paper,
    b: Paper,
    thresholds: DedupThresholds | None = None,
) -> DedupDecision:
    """Evaluate the fuzzy pass over one pair. Never merges; only reports."""
    t = thresholds or DedupThresholds()

    title_check, sim = _title_gate(a, b, t)
    year_check, year_diff = _year_gate(a, b, t)
    journal_check, jmatch = _journal_gate(a, b)
    author_check, overlap = _author_gate(a, b)

    if title_check is GateStatus.FAIL:
        decision, reason = DedupVerdict.KEEP_SEPARATE, DedupReason.BELOW_SIMILARITY_THRESHOLD
    elif year_check is GateStatus.FAIL:
        decision, reason = DedupVerdict.KEEP_SEPARATE, DedupReason.OUTSIDE_YEAR_WINDOW
    elif year_check is GateStatus.UNKNOWN:
        # the year window is a necessary condition, so it cannot be assumed
        decision, reason = DedupVerdict.KEEP_SEPARATE, DedupReason.INSUFFICIENT_EVIDENCE
    elif journal_check is GateStatus.FAIL:
        decision, reason = DedupVerdict.KEEP_SEPARATE, DedupReason.JOURNAL_MISMATCH
    elif author_check is GateStatus.FAIL:
        decision, reason = DedupVerdict.KEEP_SEPARATE, DedupReason.AUTHORS_DISJOINT
    elif journal_check is GateStatus.PASS or author_check is GateStatus.PASS:
        # title and year agree and something independent corroborates them
        decision, reason = DedupVerdict.MERGE, DedupReason.ALL_GATES_PASSED
    else:
        # title and year agree, and nothing independent survives. Same title in
        # the same year across a literature with no journal or author on record
        # is a coincidence far more often than a duplicate.
        decision, reason = DedupVerdict.KEEP_SEPARATE, DedupReason.INSUFFICIENT_EVIDENCE

    return DedupDecision(
        decision_id=content_hash(f"{a.paper_id}|{b.paper_id}", 12),
        record_a=a.paper_id,
        record_b=b.paper_id,
        pass_name=DedupPass.TITLE_FUZZY,
        decision=decision,
        reason=reason,
        title_check=title_check,
        year_check=year_check,
        journal_check=journal_check,
        author_check=author_check,
        normalized_title_similarity=None if sim == 0.0 else round(sim, 4),
        year_difference=year_diff,
        journal_match=jmatch,
        authors_overlap=None if overlap is None else round(overlap, 4),
        detail="",
        threshold_version=t.version,
    )


# ---------------------------------------------------------------------------
# merging
# ---------------------------------------------------------------------------


def _pick(papers: list[Paper], attribute: str):
    """First non-empty value by source precedence, recording disagreements.

    Distinctness is measured on `repr`, because the fields that can disagree
    here include lists of authors and identifiers, which are not hashable.
    """
    best = None
    for source in SOURCE_PRECEDENCE:
        for paper in papers:
            if source in paper.sources:
                value = getattr(paper, attribute, None)
                if value not in (None, "", [], {}):
                    best = value
                    break
        if best is not None:
            break

    variants: list[str] = []
    for paper in papers:
        value = getattr(paper, attribute, None)
        if value in (None, "", [], {}):
            continue
        rendered = repr(value)
        if rendered not in variants:
            variants.append(rendered)
    return best, (variants if len(variants) > 1 else None)


def merge_papers(papers: list[Paper]) -> Paper:
    """Fold duplicate records into one. Conflicts are recorded, never dropped."""
    papers = list(papers)
    if len(papers) == 1:
        return papers[0]

    base = min(papers, key=lambda p: (
        SOURCE_PRECEDENCE.index(p.sources[0]) if p.sources and p.sources[0] in SOURCE_PRECEDENCE
        else len(SOURCE_PRECEDENCE)
    ))
    merged = base.model_copy(deep=True)
    conflicts: list[str] = list(base.merge_conflicts)

    for attribute in ("title", "journal", "year", "volume", "issue", "pages", "url"):
        value, disagreement = _pick(papers, attribute)
        if value is not None:
            setattr(merged, attribute, value)
        if disagreement:
            conflicts.append(
                f"{attribute}: sources disagree ({len(disagreement)} variants)"
            )

    authors, author_disagreement = _pick(papers, "authors")
    if authors:
        merged.authors = authors
    if author_disagreement:
        conflicts.append(f"authors: sources disagree ({len(author_disagreement)} variants)")

    seen_abstracts = {(a.source, a.text) for a in merged.abstracts}
    for paper in papers:
        for record in paper.abstracts:
            if (record.source, record.text) not in seen_abstracts:
                seen_abstracts.add((record.source, record.text))
                merged.abstracts.append(record)

    # identifiers are a union: they describe the same work from different angles
    for paper in papers:
        for field in merged.identifiers.model_fields:
            if getattr(merged.identifiers, field) in (None, ""):
                setattr(merged.identifiers, field, getattr(paper.identifiers, field))

    sources: list[str] = []
    source_ids: dict[str, str] = {}
    raw_hashes: dict[str, str] = {}
    for paper in papers:
        for s in paper.sources:
            if s not in sources:
                sources.append(s)
        source_ids.update(paper.source_ids)
        raw_hashes.update(paper.raw_hashes)

    # access is reported by the access provider and is never merged from others
    assessment = next(
        (p.access for p in papers if p.access.source == "unpaywall"),
        next((p.access for p in papers if p.access.oa_status.value != "unknown"), base.access),
    )

    merged.sources = sources
    merged.source_ids = source_ids
    merged.raw_hashes = raw_hashes
    merged.merge_conflicts = conflicts
    merged.access = assessment
    merged.paper_id = (
        merged.identifiers.doi
        or content_hash(f"{merged.title}|{merged.year}", 16)
    )
    return merged


# ---------------------------------------------------------------------------
# the state machine
# ---------------------------------------------------------------------------


def deduplicate(
    papers: list[Paper],
    thresholds: DedupThresholds | None = None,
) -> DedupResult:
    """pass 1 (DOI) -> pass 2 (title+year) -> pass 3 (fuzzy candidate screen)."""
    t = thresholds or DedupThresholds()
    stats = DedupStats(input_count=len(papers))
    buckets: list[list[Paper]] = []
    decisions: list[DedupDecision] = []

    # -- pass 1: exact normalised DOI ------------------------------------
    by_doi: dict[str, list[Paper]] = {}
    leftovers: list[Paper] = []
    for paper in papers:
        doi = normalize_doi(paper.identifiers.doi)
        if doi:
            by_doi.setdefault(doi, []).append(paper)
        else:
            leftovers.append(paper)

    for doi, group in by_doi.items():
        if len(group) > 1:
            for other in group[1:]:
                decisions.append(DedupDecision(
                    decision_id=content_hash(f"{group[0].paper_id}|{other.paper_id}", 12),
                    record_a=group[0].paper_id,
                    record_b=other.paper_id,
                    pass_name=DedupPass.DOI_EXACT,
                    decision=DedupVerdict.MERGE,
                    reason=DedupReason.EXACT_DOI,
                    title_check=GateStatus.PASS,
                    detail=f"identical normalised DOI {doi}",
                    threshold_version=t.version,
                ))
            stats.exact_doi_merges += len(group) - 1
        buckets.append(group)

    # -- pass 2: exact normalised title + year, among the DOI-less --------
    by_title: dict[tuple[str, int], list[Paper]] = {}
    remaining: list[Paper] = []
    for paper in leftovers:
        key = (normalize_title(paper.title), paper.year) if paper.year else None
        if key and key[0]:
            by_title.setdefault(key, []).append(paper)  # type: ignore[arg-type]
        else:
            remaining.append(paper)

    for key, group in by_title.items():
        if len(group) > 1:
            for other in group[1:]:
                decisions.append(DedupDecision(
                    decision_id=content_hash(f"{group[0].paper_id}|{other.paper_id}", 12),
                    record_a=group[0].paper_id,
                    record_b=other.paper_id,
                    pass_name=DedupPass.TITLE_YEAR_EXACT,
                    decision=DedupVerdict.MERGE,
                    reason=DedupReason.EXACT_TITLE_AND_YEAR,
                    title_check=GateStatus.PASS,
                    year_check=GateStatus.PASS,
                    detail=f"identical normalised title and year {key}",
                    threshold_version=t.version,
                ))
            stats.title_year_merges += len(group) - 1
        buckets.append(group)
    buckets.extend([[p] for p in remaining])

    # -- pass 3: fuzzy screen, best-first --------------------------------
    # Merged pairs replace their two members rather than being appended beside
    # them, so the surviving bucket list is rebuilt from: multi-member buckets
    # from the earlier passes, then merged pairs, then the screened singletons
    # that were never paired.
    settled = [b for b in buckets if len(b) != 1]
    screened = [b for b in buckets if len(b) == 1]

    candidates: list[tuple[float, int, int]] = []
    for i, a in enumerate(screened):
        for j, b in enumerate(screened):
            if i < j:
                sim = title_similarity(a[0].title, b[0].title)
                if sim >= t.title_similarity:
                    candidates.append((sim, i, j))
    candidates.sort(reverse=True)

    used: set[int] = set()
    merged_pairs: list[list[Paper]] = []
    for _sim, i, j in candidates:
        if i in used or j in used:
            continue
        decision = screen_pair(screened[i][0], screened[j][0], t)
        decisions.append(decision)
        if decision.decision is DedupVerdict.MERGE:
            stats.fuzzy_merges += 1
            merged_pairs.append(screened[i] + screened[j])
            used.update({i, j})
        else:
            stats.kept_separate_on_insufficient_evidence += 1

    buckets = (
        settled
        + merged_pairs
        + [group for k, group in enumerate(screened) if k not in used]
    )

    merged = [merge_papers(group) for group in buckets]
    stats.output_count = len(merged)
    stats.decisions = decisions
    return DedupResult(
        papers=merged,
        groups=[[p.paper_id for p in group] for group in buckets],
        stats=stats,
    )

"""LiteratureService: deterministic orchestration.

```
    request
       │
       ├── OpenAlex search  ──┐
       │                      ├──→  DOI grouping  →  field-level merge
       └── Crossref search  ──┘
                                 │
                    (opt-in)     ▼
                            Unpaywall lookup
                                 │
                            access reconciliation
                                 │
                                 ▼
                            result
```

This is orchestration and nothing else. It does no chemistry, implements no
provider, decides no permission, calls no model and ranks nothing. Those belong
to `chem/`, `sources/`, `core/policy/access.py` and
`services/access_reconciliation.py` respectively, and reaching past them from
here would make the boundary untestable.

Three decisions this service makes, and only three:

1. **Which providers to ask**, and what to do when one of them fails.
2. **Which records describe the same work** -- by DOI, delegated to
   `services.literature_merge`.
3. **Whether to spend access calls**, which is opt-in and bounded.
"""

from __future__ import annotations

import time
from collections import OrderedDict

from crea.core.errors import CreaError, ProviderError
from crea.core.ids import normalize_doi
from crea.core.models.literature import Paper
from crea.core.models.merge import ReconciliationInput
from crea.core.models.service import (
    AccessCall,
    DoiLookupRequest,
    DoiLookupResult,
    LiteratureSearchRequest,
    LiteratureSearchResult,
    ProviderRun,
    ProviderRunStatus,
    SearchOutcome,
)
from crea.core.provenance import utc_now_iso
from crea.providers.base import LiteratureQuery, PaperRef
from crea.services.access_reconciliation import evidence_from_paper, reconcile
from crea.services.literature_merge import DEFAULT_POLICY, merge_papers
from crea.services.literature_retry import RetryPolicy, call_with_retry


class LiteratureService:
    """Coordinates the providers. Stateless; providers are injected."""

    def __init__(self, providers: dict, access_provider=None,
                 merge_policy=DEFAULT_POLICY,
                 retry_policy: RetryPolicy | None = None) -> None:
        self._providers = dict(providers)
        self._access_provider = access_provider
        self._policy = merge_policy
        #: Bounded retry for transient provider failures. Default off, which
        #: preserves the provider contract exactly; a composition root opts in.
        self._retry = retry_policy or RetryPolicy()

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def search(self, request: LiteratureSearchRequest) -> LiteratureSearchResult:
        result = LiteratureSearchResult(
            request=request,
            outcome=SearchOutcome.SUCCESS,
            merge_policy_version=self._policy.version,
            generated_at=utc_now_iso(),
        )

        if not request.query.strip():
            result.outcome = SearchOutcome.NO_RESULTS
            result.warnings.append("empty query; no provider was asked")
            return result

        collected: list[Paper] = []
        ran = 0
        failed = 0

        for name in request.providers:
            provider = self._providers.get(name)
            if provider is None:
                result.provider_runs.append(ProviderRun(
                    name=name, status=ProviderRunStatus.SKIPPED,
                    error=f"no provider registered as {name!r}"))
                continue
            run, papers = self._run_search(provider, request, result.warnings)
            result.provider_runs.append(run)
            if run.status is ProviderRunStatus.OK:
                ran += 1
                collected.extend(papers)
            else:
                failed += 1

        if ran == 0:
            result.outcome = SearchOutcome.ALL_PROVIDERS_FAILED
            result.warnings.append(
                "every requested provider failed or was unavailable; this is "
                "not an empty result")
            return result

        if failed:
            result.outcome = SearchOutcome.PARTIAL_SUCCESS
            result.warnings.append(
                f"{failed} of {ran + failed} provider(s) did not answer; "
                f"results cover the ones that did")

        groups = self._group_by_doi(collected)
        merged, merges, unresolved = self._merge(groups)

        result.papers = merged
        result.merges = merges
        result.merged_count = sum(1 for m in merges if m.merged)
        result.unresolved_count = unresolved

        if not merged:
            result.outcome = SearchOutcome.NO_RESULTS
            return result

        self._enrich_access(result, request)
        return result

    def _run_search(self, provider, request: LiteratureSearchRequest,
                    warnings: list[str] | None = None):
        started = time.perf_counter()
        try:
            outcome = call_with_retry(
                lambda: _sync(provider.search(
                    LiteratureQuery(text=request.query, limit=request.limit))),
                self._retry)
            papers = list(getattr(outcome, "papers", outcome) or [])
            warnings.extend(getattr(outcome, "warnings", []) or [])
        except CreaError as exc:
            return ProviderRun(
                name=provider.name, status=ProviderRunStatus.ERROR,
                availability=provider.status().availability.value,
                error_type=type(exc).__name__, error=str(exc),
                duration_ms=int((time.perf_counter() - started) * 1000),
            ), []
        except Exception as exc:  # noqa: BLE001 - an adapter bug must not kill the search
            return ProviderRun(
                name=provider.name, status=ProviderRunStatus.ERROR,
                error_type=type(exc).__name__, error=str(exc),
                duration_ms=int((time.perf_counter() - started) * 1000),
            ), []
        return ProviderRun(
            name=provider.name, status=ProviderRunStatus.OK,
            availability=provider.status().availability.value,
            records_found=len(papers),
            duration_ms=int((time.perf_counter() - started) * 1000),
        ), papers

    # ------------------------------------------------------------------
    # identity and merge
    # ------------------------------------------------------------------

    @staticmethod
    def _group_by_doi(papers: list[Paper]) -> "OrderedDict[str, list[Paper]]":
        """Group by normalised DOI; records with no DOI keep their own group.

        Insertion order is discovery order, which is what makes the output
        stable without us inventing a score.
        """
        groups: OrderedDict[str, list[Paper]] = OrderedDict()
        for paper in papers:
            key = normalize_doi(paper.identifiers.doi) or f"_nodoci:{paper.paper_id}"
            groups.setdefault(key, []).append(paper)
        return groups

    def _merge(self, groups):
        merged: list[Paper] = []
        merges = []
        unresolved = 0
        for key, group in groups.items():
            result = merge_papers(group, policy=self._policy)
            merges.append(result)
            if result.paper is not None:
                merged.append(result.paper)
            if result.identity.value != "doi_match":
                unresolved += 1
        return merged, merges, unresolved

    # ------------------------------------------------------------------
    # access enrichment -- opt-in and bounded
    # ------------------------------------------------------------------

    def _enrich_access(self, result: LiteratureSearchResult,
                       request: LiteratureSearchRequest) -> None:
        if not request.include_access:
            result.access_skipped_reason = (
                "include_access=False; Unpaywall was not called for any record"
            )
            return

        provider = self._access_provider
        if provider is None:
            result.access_skipped_reason = "no access provider was configured"
            result.warnings.append("include_access=True but no access provider exists")
            return

        if not provider.status().usable:
            result.access_skipped_reason = (
                f"access provider unavailable: {provider.status().reason}")
            result.warnings.append(result.access_skipped_reason)
            return

        budget = max(0, request.max_access_calls)
        if budget == 0:
            result.access_skipped_reason = "max_access_calls=0"
            return

        used = 0
        for paper in result.papers:
            if used >= budget:
                result.warnings.append(
                    f"access enrichment stopped at {budget} call(s); "
                    f"{len(result.papers) - used} record(s) were left without it")
                result.access_skipped_reason = f"budget of {budget} call(s) reached"
                break
            doi = normalize_doi(paper.identifiers.doi)
            if not doi:
                # Unpaywall resolves a DOI. There is nothing to give it, and
                # inventing a query would be a different, unverified API.
                result.access_calls.append(AccessCall(
                    doi="", outcome="skipped", error="no DOI to look up"))
                continue
            used += 1
            call, reconciliation = self._one_access(provider, doi)
            result.access_calls.append(call)
            if reconciliation is not None:
                result.access.append(reconciliation)

    @staticmethod
    def _one_access(provider, doi: str):
        try:
            evidence = _sync(provider.get_access_assessment(doi))
        except ProviderError as exc:
            return AccessCall(doi=doi, outcome="error", error=str(exc)), None
        except Exception as exc:  # noqa: BLE001
            return AccessCall(doi=doi, outcome="error",
                              error=f"{type(exc).__name__}: {exc}"), None
        if evidence is None:
            return AccessCall(doi=doi, outcome="unavailable"), None
        item = ReconciliationInput(
            source=provider.name,
            source_id=doi,
            oa_status=evidence.oa_status.value,
            oa_status_raw=evidence.oa_status_raw or evidence.oa_status.value,
            license_spdx=evidence.license.spdx_id,
            license_name=evidence.license.name,
            license_url=evidence.license.url,
            license_content_version=evidence.license.content_version,
            fulltext_url=evidence.fulltext_url,
            url_type=evidence.url_type,
            notes=list(evidence.notes),
        )
        return AccessCall(doi=doi, outcome="ok"), reconcile([item],
                                                            {provider.name: evidence})

    # ------------------------------------------------------------------
    # DOI lookup
    # ------------------------------------------------------------------

    def lookup_by_doi(self, doi: str, include_access: bool = False) -> DoiLookupResult:
        """Resolve one DOI. The plain-argument form callers actually want."""
        return self.lookup(DoiLookupRequest(doi=doi, include_access=include_access))

    def lookup(self, request: DoiLookupRequest) -> DoiLookupResult:
        """Resolve one DOI across providers, merge, and optionally check access.

        A record nobody has is a normal answer here, not an error: a DOI lookup
        that finds nothing should say so, not raise.
        """
        doi = normalize_doi(request.doi)
        result = DoiLookupResult(request=request, generated_at=utc_now_iso())
        if not doi:
            result.warnings.append(f"{request.doi!r} does not normalise to a DOI")
            return result

        collected: list[Paper] = []
        for name in request.providers:
            provider = self._providers.get(name)
            if provider is None:
                result.provider_runs.append(ProviderRun(
                    name=name, status=ProviderRunStatus.SKIPPED,
                    error=f"no provider registered as {name!r}"))
                continue
            started = time.perf_counter()
            try:
                paper = call_with_retry(
                    lambda: _sync(provider.get_metadata(PaperRef(doi=doi))),
                    self._retry)
            except CreaError as exc:
                result.provider_runs.append(ProviderRun(
                    name=provider.name, status=ProviderRunStatus.ERROR,
                    error_type=type(exc).__name__, error=str(exc),
                    duration_ms=int((time.perf_counter() - started) * 1000)))
                continue
            except Exception as exc:  # noqa: BLE001
                result.provider_runs.append(ProviderRun(
                    name=provider.name, status=ProviderRunStatus.ERROR,
                    error_type=type(exc).__name__, error=str(exc),
                    duration_ms=int((time.perf_counter() - started) * 1000)))
                continue
            result.provider_runs.append(ProviderRun(
                name=provider.name,
                status=ProviderRunStatus.OK if paper is not None
                else ProviderRunStatus.SKIPPED,
                records_found=1 if paper is not None else 0,
                duration_ms=int((time.perf_counter() - started) * 1000)))
            if paper is not None:
                collected.append(paper)

        if not collected:
            result.warnings.append(
                f"no provider has a record for {doi}; this is an answer, not a failure")
            return result

        merge = merge_papers(collected, policy=self._policy)
        result.merge = merge
        result.paper = merge.paper
        result.found = True
        result.identity = merge.identity.value
        result.identity_basis = merge.identity_basis

        if request.include_access and self._access_provider is not None \
                and self._access_provider.status().usable:
            call, reconciliation = self._one_access(self._access_provider, doi)
            if reconciliation is not None:
                result.access.append(reconciliation)
            if call.error:
                result.warnings.append(f"access lookup failed: {call.error}")
        elif request.include_access:
            result.warnings.append("include_access=True but access is unavailable")
        return result


def _sync(coro):
    """Run a provider coroutine.

    The service is synchronous on purpose: orchestration that has to be awaited
    correctly is orchestration nobody will call from a script or a CLI.
    """
    import asyncio

    return asyncio.run(coro)

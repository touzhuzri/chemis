"""Fixture loading and the stand-in provider parsers.

**Every fixture says where it came from, and the two kinds are not mixed.**

``origin = SYNTHETIC``
    Hand-authored to a plausible shape, so that a parse path can be exercised
    before the real client exists. A synthetic payload must never pose as a
    recorded response: a test asserting its own imagination is worse than no
    test, because it passes.

``origin = LIVE_CAPTURE``
    Bytes taken from the real endpoint by a generator script named in the
    `.meta.json`, with the exact URL and the moment of capture recorded. Used
    once an endpoint has been verified, so the parse is tested against the
    shape the source actually sends.

The Phase 2 literature fixtures are `SYNTHETIC` and their meta files say so; the
Phase 3.6 PubChem fixtures are `LIVE_CAPTURE` and theirs name
`tests/fixtures/providers/_gen_pubchem.py`. Both are *parsed* here rather than
pre-converted to a model, on purpose: if a fixture were a pre-built `Paper` or
`ChemicalIdentityCandidate`, an upstream schema change would produce a quietly
different value instead of a parse failure.
"""

from __future__ import annotations

import json
import pathlib

from crea.core.ids import content_hash
from crea.core.models.literature import (
    AbstractRecord,
    AbstractSource,
    AccessAssessment,
    Author,
    Identifiers,
    LicenseInfo,
    OAStatus,
    Paper,
)
from crea.providers.base import Availability, PaperRef, ProviderStatus
from crea.sources.base import BaseProvider, ProviderResult, RateLimit
from crea.sources.crossref import parse_work as _cr_parse_work
from crea.sources.openalex import parse_work as _oa_parse_work
from crea.sources.openalex import reconstruct_abstract
from crea.sources.unpaywall import parse_response as _up_parse_response

FIXTURE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "providers"


def load(provider: str, name: str) -> object:
    path = FIXTURE_ROOT / provider / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def meta(provider: str, name: str) -> dict:
    path = FIXTURE_ROOT / provider / f"{name}.meta.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def payload_hash(payload: object) -> str:
    return content_hash(json.dumps(payload, sort_keys=True, ensure_ascii=False), 16)


# ---------------------------------------------------------------------------
# parse helpers -- the shapes a real client would implement
# ---------------------------------------------------------------------------


def parse_openalex_work(work: dict, retrieved_at: str) -> Paper:
    """Delegate to the production parser.

    The schema was verified against live OpenAlex responses, so a second
    implementation of the same mapping in the test suite is a liability: it
    would be the copy that drifts. The test double's job is to supply a
    payload, not to re-implement the reader.
    """
    return _oa_parse_work(work, provider_name="openalex", retrieved_at=retrieved_at)


def parse_crossref_message(payload: dict, retrieved_at: str) -> Paper:
    """Delegate to the production parser.

    The schema was read off a live Crossref response, so a second mapping in
    the test suite is a liability: it would be the copy that drifts.
    """
    message = (payload or {}).get("message") or {}
    return _cr_parse_work(message, provider_name="crossref", retrieved_at=retrieved_at)


def parse_unpaywall(payload: dict, retrieved_at: str) -> AccessAssessment:
    """Delegate to the production parser."""
    _paper, assessment = _up_parse_response(
        payload, provider_name="unpaywall", retrieved_at=retrieved_at)
    return assessment


# ---------------------------------------------------------------------------
# the stand-in provider
# ---------------------------------------------------------------------------


class FixtureProvider(BaseProvider):
    """A provider whose payload source is a fixture file.

    It exercises everything except the endpoint: parsing, provenance stamping,
    access reporting, and the retrieval gate.
    """

    def __init__(self, name: str, fixture: str, retrieved_at: str, **kwargs) -> None:
        super().__init__(name=name, transport=None, **kwargs)  # type: ignore[arg-type]
        self.fixture = fixture
        self.retrieved_at = retrieved_at

    def status(self) -> ProviderStatus:
        if self.requires_key and not self.missing_key_reason:
            return ProviderStatus(self.name, Availability.MISSING_KEY, "no credential configured")
        return super().status()

    async def search(self, query) -> ProviderResult:
        provider, case = self.fixture.split("/", 1)
        payload = load(provider, case)
        papers: list[Paper] = []
        if self.name == "openalex":
            works = (payload.get("results") if isinstance(payload, dict) else payload) or []
            for work in works:
                paper = parse_openalex_work(work, self.retrieved_at)
                # the provider states oa_status and the location; policy decides
                # the permission. This ordering is the discipline, not a detail.
                paper.access = self.report_access(paper.access)
                papers.append(self.stamp(paper))
        return ProviderResult(
            provider=self.name,
            papers=papers,
            raw_hashes={self.name: payload_hash(payload)},
            retrieved_at=self.retrieved_at,
        )

    async def get_access_assessment(self, doi: str):
        provider, case = self.fixture.split("/", 1)
        payload = load(provider, case)
        if isinstance(payload, dict) and payload.get("error"):
            return None
        return self.report_access(parse_unpaywall(payload, self.retrieved_at))

    async def get_metadata(self, ref: PaperRef):
        provider, case = self.fixture.split("/", 1)
        payload = load(provider, case)
        if isinstance(payload, dict) and payload.get("error"):
            return None
        return self.stamp(parse_crossref_message(payload, self.retrieved_at))

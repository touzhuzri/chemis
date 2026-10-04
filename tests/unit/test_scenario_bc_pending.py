"""T-G part 2 / T-H -- Scenario B and C. Deferred to Phase 2 by decision.

These tests are written down, not written up. Phase 1 was scoped to Scenario A
only, and the user's instruction was explicit: do not implement OpenAlex
search, full-text parsing, evidence extraction, or retrosynthesis in Phase 1.

They are collected as explicit skips rather than omitted, so that the coverage
gap is visible in the test report instead of being invisible in a file that
does not exist.
"""

import pytest

pytestmark = pytest.mark.phase2


SCENARIO_B = [
    ("G-01", "OpenAlex response maps onto the Paper model"),
    ("G-02", "a repeated query is served from cache with an unchanged retrieved_at"),
    ("G-03", "DOI variants (https://doi.org/X, doi:X, lowercase) collapse to one record"),
    ("G-04", "records without a DOI merge on normalized title + year + first author"),
    ("G-05", "Unpaywall is_oa=true yields access_type='open' with a non-empty oa_url"),
    ("G-06", "Unpaywall is_oa=false yields access_type='closed'"),
    ("G-07", "a 404/withdrawn DOI yields access_type='unknown' rather than raising"),
    ("G-08", "HTTP 429 triggers backoff and then succeeds"),
    ("G-09", "a Crossref timeout degrades the result to partial instead of failing"),
    ("G-10", "an absent UNPAYWALL_EMAIL degrades retrieval and says so to the user"),
]

SCENARIO_C = [
    ("H-01", "an OA PDF yields non-empty text with page spans and a stable doc hash"),
    ("H-02", "sections locate the Experimental block inside the full text"),
    ("H-03", "the located span actually contains condition keywords"),
    ("H-04", "a closed PDF is refused before extraction is ever called"),
    ("H-05", "every raw_quote is findable verbatim in the source text"),
    ("H-06", "a tampered yield with an unchanged quote fails validation and is excluded"),
    ("H-07", "a review with no experimental section yields no Evidence, not invented Evidence"),
    ("H-08", "a scanned PDF without a text layer is reported unsupported, not silently empty"),
    ("H-09", "field-level spans parse back to the value they claim"),
]


@pytest.mark.parametrize("case_id,description", SCENARIO_B, ids=[c[0] for c in SCENARIO_B])
def test_scenario_b_pending(case_id, description):
    pytest.skip(f"Phase 2: {case_id} {description}")


@pytest.mark.parametrize("case_id,description", SCENARIO_C, ids=[c[0] for c in SCENARIO_C])
def test_scenario_c_pending(case_id, description):
    pytest.skip(f"Phase 2: {case_id} {description}")

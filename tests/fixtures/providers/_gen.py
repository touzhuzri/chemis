"""Generate the synthetic provider fixtures. Run once; the files are the artefact.

Two things are tracked separately, because they have different truth values:

  values  -- SYNTHETIC. Hand-authored. No value here came from a real API.
  schema  -- LIVE_VERIFIED, where the field names and shapes were read off a
             live response on 2026-09-30 (see docs/PHASE2_PROVIDER_CONTRACT.md).
             Verified means the *shape* is real, not that the *data* is.

A fixture is never a recorded response and never claims to be one.
"""

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent


def write(provider, name, payload, endpoint, note,
          schema="LIVE_VERIFIED", schema_unverified=()):
    directory = ROOT / provider
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    meta = {
        "provider": provider,
        "fixture": name,
        "values": "SYNTHETIC",
        "is_recorded_response": False,
        # Present and null on purpose: these fixtures are not recordings, and
        # the field says so rather than being absent. Any future recorded
        # response must fill all four before it may be committed.
        "recorded_at": None,
        "license_status": "N/A -- synthetic values, no upstream content",
        "schema": schema,
        "schema_verified_at": "2026-09-30",
        "schema_source": f"docs/PHASE2_PROVIDER_CONTRACT.md ({provider})",
        "schema_unverified": list(schema_unverified),
        "endpoint": endpoint,
        "contains_full_text": False,
        "credentials_redacted": True,
        "note": note,
    }
    (directory / f"{name}.meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# --- OpenAlex-shaped search response ---------------------------------------
# Field names below follow the live response verified 2026-09-30: the location
# object uses landing_page_url / pdf_url, and content_urls carries
# content.openalex.org links. The earlier draft of this fixture used Unpaywall's
# url / url_for_pdf, which OpenAlex does not have.
write(
    "openalex",
    "search_diyne",
    {
        "_synthetic_values": True,
        "meta": {"count": 3, "page": 1, "per_page": 25, "cost_usd": 0.0001},
        "results": [
            {
                "id": "https://openalex.org/W0000000001",
                "doi": "https://doi.org/10.0000/synthetic.2026.0001",
                "display_name": "Synthesis of 1,4-diphenyl-1,3-diyne by oxidative coupling",
                "title": "Synthesis of 1,4-diphenyl-1,3-diyne by oxidative coupling",
                "publication_year": 2021,
                "publication_date": "2021-04-17",
                "type": "article",
                "ids": {
                    "openalex": "https://openalex.org/W0000000001",
                    "doi": "https://doi.org/10.0000/synthetic.2026.0001",
                },
                "authorships": [
                    {"author": {"display_name": "A Synthetic", "orcid": None}},
                    {"author": {"display_name": "B Fictional", "orcid": None}},
                ],
                "abstract_inverted_index": {
                    "A": [0], "glucokinase": [1], "regulator": [2],
                    "was": [3], "selected": [4], "to": [5], "identify": [6],
                    "mutations": [7], "in": [8], "NIDDK": [9], "that": [10],
                },
                "open_access": {
                    "is_oa": True,
                    "oa_status": "gold",
                    "oa_url": "https://example.org/landing/1",
                    "any_repository_has_fulltext": True,
                },
                "best_oa_location": {
                    "id": "doi:10.0000/synthetic.2026.0001",
                    "is_oa": True,
                    "landing_page_url": "https://example.org/landing/1",
                    "pdf_url": "https://example.org/pdf/1",
                    "source": {"id": "https://openalex.org/S0000000001",
                               "display_name": "Journal of Synthetic Chemistry",
                               "issn_l": "0000-0000", "issn": ["0000-0000"],
                               "is_oa": True, "is_in_doaj": False, "is_core": True},
                    "license": "cc-by",
                    "version": "publishedVersion",
                    "is_accepted": True,
                    "is_published": True,
                    "is_best": True,
                },
                "has_fulltext": True,
                "has_content": {"grobid_xml": True, "pdf": True},
                "content_urls": {
                    "pdf": "https://content.openalex.org/works/W0000000001.pdf",
                    "grobid_xml": "https://content.openalex.org/works/W0000000001.grobid-xml",
                },
                "primary_location": {"source": {"display_name": "Journal of Synthetic Chemistry"}},
                "is_retracted": False,
            },
            {
                "id": "https://openalex.org/W0000000002",
                "doi": None,
                "display_name": "Sonogashira coupling in diyne synthesis: a review",
                "title": "Sonogashira coupling in diyne synthesis: a review",
                "publication_year": 2019,
                "publication_date": "2019-01-01",
                "type": "article",
                "ids": {"openalex": "https://openalex.org/W0000000002"},
                "authorships": [{"author": {"display_name": "C Placeholder", "orcid": None}}],
                "abstract_inverted_index": None,
                "open_access": {"is_oa": False, "oa_status": "closed", "oa_url": None,
                                "any_repository_has_fulltext": False},
                "best_oa_location": None,
                "has_fulltext": False,
                "has_content": {"grobid_xml": False, "pdf": False},
                "content_urls": None,
                "primary_location": {"source": {"display_name": "Reviews in Fake Chemistry"}},
                "is_retracted": False,
            },
            {
                "id": "https://openalex.org/W0000000003",
                "doi": "https://doi.org/10.0000/synthetic.2022.0003",
                "display_name": "A diamond open access note on diyne chemistry",
                "title": "A diamond open access note on diyne chemistry",
                "publication_year": 2022,
                "publication_date": "2022-06-01",
                "type": "article",
                "ids": {"openalex": "https://openalex.org/W0000000003",
                        "doi": "https://doi.org/10.0000/synthetic.2022.0003"},
                "authorships": [{"author": {"display_name": "D Diamond", "orcid": None}}],
                "abstract_inverted_index": None,
                "open_access": {"is_oa": True, "oa_status": "diamond",
                                "oa_url": "https://example.org/landing/3",
                                "any_repository_has_fulltext": True},
                "best_oa_location": {
                    "id": "doi:10.0000/synthetic.2022.0003",
                    "is_oa": True,
                    "landing_page_url": "https://example.org/landing/3",
                    "pdf_url": None,
                    "source": {"id": "https://openalex.org/S0000000003",
                               "display_name": "Diamond Journal of Chemistry",
                               "issn_l": "0000-0001", "issn": ["0000-0001"],
                               "is_oa": True, "is_in_doaj": True, "is_core": False},
                    "license": None,
                    "version": "publishedVersion",
                    "is_accepted": True, "is_published": True, "is_best": True,
                },
                "has_fulltext": False,
                "has_content": {"grobid_xml": False, "pdf": False},
                "content_urls": None,
                "primary_location": {"source": {"display_name": "Diamond Journal of Chemistry"}},
                "is_retracted": False,
            },
        ],
    },
    "https://api.openalex.org/works  [VERIFIED 2026-09-30]",
    "Search results for the Phase 2 end-to-end query. The second record has no "
    "DOI, which is what makes the title/year dedup pass load-bearing. The third "
    "is a diamond OA record with no licence -- the case that must stay at "
    "UNKNOWN permission.",
    schema_unverified=[
        "abstract_inverted_index content for a record that actually has an abstract "
        "(the live probe returned null for every record sampled)",
    ],
)

# --- Unpaywall-shaped access responses -------------------------------------
write(
    "unpaywall", "is_oa_green",
    {
        "_synthetic_values": True,
        "doi": "10.0000/synthetic.2026.0001",
        "title": "Synthesis of 1,4-diphenyl-1,3-diyne by oxidative coupling",
        "year": 2021,
        "is_oa": True,
        "oa_status": "green",
        "journal_name": "Journal of Synthetic Chemistry",
        "journal_issn_l": "0000-0000",
        "journal_is_oa": True,
        "journal_is_in_doaj": True,
        "publisher": "Example Synthetic Press",
        "genre": "journal-article",
        "has_repository_copy": True,
        "best_oa_location": {
            "endpoint_id": None,
            "evidence": "deprecated",
            "host_type": "repository",
            "is_best": True,
            "license": "cc-by",
            "oa_date": "2021-05-01",
            "pmh_id": None,
            "repository_institution": "Example Repository",
            "updated": "deprecated",
            "url": "https://example.org/pdf/1",
            "url_for_landing_page": "https://doi.org/10.0000/synthetic.2026.0001",
            "url_for_pdf": "https://example.org/pdf/1",
            "version": "publishedVersion",
        },
        "oa_locations": [],
        "oa_locations_embargoed": [],
        "has_repository_copy": True,
        "journal_is_oa": True,
        "journal_is_in_doaj": True,
        "genre": "journal-article",
        "data_standard": 2,
    },
    "https://api.unpaywall.org/v2/{doi}?email=...  [VERIFIED 2026-09-30]",
    "Green OA with an explicit CC-BY licence: the only fixture that should reach "
    "MAY_RETRIEVE, and only because of the licence, not the URL.",
    schema_unverified=["oa_status value set beyond closed/gold observed live"],
)

write(
    "unpaywall", "is_oa_closed",
    {
        "_synthetic_values": True,
        "doi": "10.0000/synthetic.2019.0002",
        "title": "Sonogashira coupling in diyne synthesis: a review",
        "year": 2019,
        "is_oa": False,
        "oa_status": "closed",
        "best_oa_location": None,
        "oa_locations": [],
        "oa_locations_embargoed": [],
        "has_repository_copy": False,
        "journal_is_oa": False,
        "journal_is_in_doaj": False,
    },
    "https://api.unpaywall.org/v2/{doi}?email=...  [VERIFIED 2026-09-30]",
    "Closed. No location at all, so the gate short-circuits on the URL as well as "
    "on the permission.",
)

write(
    "unpaywall", "is_oa_bronze",
    {
        "_synthetic_values": True,
        "doi": "10.0000/synthetic.2020.0003",
        "title": "A bronze open access article",
        "year": 2020,
        "is_oa": True,
        "oa_status": "bronze",
        "best_oa_location": {
            "endpoint_id": None,
            "evidence": "deprecated",
            "host_type": "publisher",
            "is_best": True,
            "license": None,
            "oa_date": "2020-01-01",
            "pmh_id": None,
            "repository_institution": None,
            "updated": "deprecated",
            "url": "https://example.org/pdf/3",
            "url_for_landing_page": "https://example.org/landing/3",
            "url_for_pdf": "https://example.org/pdf/3",
            "version": "publishedVersion",
        },
        "oa_locations": [],
        "oa_locations_embargoed": [],
        "has_repository_copy": False,
        "journal_is_oa": False,
        "journal_is_in_doaj": False,
    },
    "https://api.unpaywall.org/v2/{doi}?email=...  [VERIFIED 2026-09-30]",
    "The important one. Bronze is free to read and carries a resolvable PDF URL, "
    "yet no licence -- so permission must stay UNKNOWN, not OA_NO_DOWNLOAD and "
    "not MAY_RETRIEVE. The live gold record probed on 2026-09-30 also carried "
    "license: null, so this is not a bronze peculiarity.",
    schema_unverified=["prose definition of bronze (unpaywall.org is a JS shell)"],
)

write(
    "unpaywall", "doi_not_found",
    {"_synthetic_values": True, "error": "DOI not found"},
    "https://api.unpaywall.org/v2/{doi}?email=...  [VERIFIED 2026-09-30]",
    "A 404 must yield UNKNOWN, never an exception and never OPEN. The live API "
    "returns an HTML body for 404, not JSON, which a client must handle.",
)

# --- Crossref-shaped metadata ----------------------------------------------
write(
    "crossref", "doi_metadata",
    {
        "_synthetic_values": True,
        "status": "ok",
        "message-type": "work",
        "message-version": "1.0.0",
        "message": {
            "DOI": "10.0000/synthetic.2026.0001",
            "title": ["Synthesis of 1,4-diphenylbuta-1,3-diyne via oxidative coupling"],
            "container-title": ["Journal of Synthetic Chemistry"],
            "author": [
                {"given": "A", "family": "Synthetic"},
                {"given": "B", "family": "Fictional"},
            ],
            "issued": {"date-parts": [[2021, 4, 17]]},
            "published": {"date-parts": [[2021, 4, 17]]},
            "volume": "12",
            "issue": "3",
            "page": "1145-1152",
            "type": "journal-article",
            "URL": "https://doi.org/10.0000/synthetic.2026.0001",
            "resource": {"primary": {"URL": "https://example.org/article/1"}},
            "license": [
                {"start": {"date-parts": [[2021, 4, 17]]},
                 "content-version": "tdm",
                 "delay-in-days": 0,
                 "URL": "https://example.org/tdm-terms"}
            ],
            "link": [
                {"URL": "https://example.org/article/1.pdf",
                 "content-type": "application/pdf",
                 "content-version": "vor",
                 "intended-application": "text-mining"}
            ],
            "abstract": "<jats:p>A synthetic abstract supplied as JATS.</jats:p>",
        },
    },
    "https://api.crossref.org/works/{doi}  [VERIFIED 2026-09-30]",
    "Metadata authority record. Its title differs from the OpenAlex one, which "
    "is what makes the merge-conflict recording observable. Note the two fields "
    "that carry their own scope in the field name: link[].intended-application "
    "is 'text-mining' and license[].content-version is 'tdm'. Neither is a "
    "general reuse grant.",
    schema_unverified=[
        "abstract JATS XML content -- absent from every record sampled live",
        "the full set of values link[].intended-application and "
        "license[].content-version can take",
    ],
)


# --- Crossref-shaped work list (search + cursor pagination) -----------------
# Field shapes read off a live Crossref response on 2026-09-30:
# `message` wraps everything; lists carry `items`, `total-results` and
# `next-cursor`. The two `link[]` entries below carry the fields the live
# record showed, including the one that matters -- `intended-application`.
def _work(n, doi_suffix, title):
    return {
        "DOI": f"10.0000/synthetic.2026.{doi_suffix}",
        "title": [title],
        "container-title": ["Journal of Synthetic Chemistry"],
        "short-container-title": ["J. Synth. Chem."],
        "author": [
            {"given": "A", "family": "Synthetic", "sequence": "first",
             "affiliation": [{"name": "Example Institute"}]},
            {"given": "B", "family": "Fictional", "sequence": "additional",
             "affiliation": []},
        ],
        "issued": {"date-parts": [[2021, 4, 17]]},
        "published-print": {"date-parts": [[2021, 4, 17]]},
        "created": {"date-time": "2021-05-02T10:00:00Z",
                    "date-parts": [[2021, 5, 2]]},
        "volume": "12", "issue": "3", "page": "1145-1152",
        "type": "journal-article",
        "publisher": "Example Synthetic Press",
        "URL": f"https://doi.org/10.0000/synthetic.2026.{doi_suffix}",
        "resource": {"primary": {"URL": f"https://example.org/article/{n}"}},
        "ISSN": ["0000-0000", "0000-000X"],
        "is-referenced-by-count": n,
        "license": [
            {"start": {"date-parts": [[2021, 4, 17]]},
             "content-version": "tdm", "delay-in-days": 0,
             "URL": "https://example.org/tdm-terms"}
        ],
        "link": [
            {"URL": f"https://example.org/article/{n}.pdf",
             "content-type": "application/pdf", "content-version": "vor",
             "intended-application": "text-mining"},
            {"URL": f"https://example.org/article/{n}.xml",
             "content-type": "application/xml", "content-version": "vor",
             "intended-application": "text-mining"},
        ],
    }


def _list_page(items, next_cursor):
    return {
        "status": "ok",
        "message-type": "work-list",
        "message": {
            "items": items,
            "total-results": len(items) + 1,
            "items-per-page": len(items),
            "next-cursor": next_cursor,
            "query": {"search-queries": []},
        },
    }


write("crossref", "work_list_page1",
      _list_page([_work(1, "0011", "1,4-diphenyl-1,3-diyne by oxidative coupling")],
                 "CURSOR-PAGE-2"),
      "https://api.crossref.org/v1/works?query.bibliographic=...&rows=1  [VERIFIED 2026-09-30]",
      "First page of a work list, carrying a next-cursor. Values synthetic; "
      "the envelope shape is what a live response showed.")

write("crossref", "work_list_page2",
      _list_page([_work(2, "0012", "Sonogashira coupling for internal diynes")],
                 None),
      "https://api.crossref.org/v1/works?cursor=CURSOR-PAGE-2&rows=1  [VERIFIED 2026-09-30]",
      "Final page: no next-cursor, which is the documented stop condition.")

write("crossref", "work_list_empty",
      _list_page([], "CURSOR-PAGE-3"),
      "https://api.crossref.org/v1/works?cursor=...  [VERIFIED 2026-09-30]",
      "An empty page that still advertises a cursor -- the pagination loop must "
      "stop on the empty item list rather than following it.")

write("crossref", "work_no_title",
      {"status": "ok", "message-type": "work",
       "message": {"DOI": "10.0000/synthetic.2026.0099",
                   "title": [], "container-title": [],
                   "publisher": "Example Synthetic Press",
                   "type": "journal-article",
                   "issued": {"date-parts": [[2020]]},
                   "created": {"date-parts": [[2020, 9, 1]]},
                   "created": {"date-parts": [[2020, 9, 1]]},
                   "license": [], "link": []}},
      "https://api.crossref.org/v1/works/10.0000/...  [VERIFIED 2026-09-30]",
      "A deposit with empty title and container arrays, a year-only issued "
      "date and a much later created date. Title must become None, not ''; "
      "created must not stand in for publication.")

write("crossref", "work_date_conflict",
      {"status": "ok", "message-type": "work",
       "message": {"DOI": "10.0000/synthetic.2026.0098",
                   "title": ["A work whose dates disagree"],
                   "container-title": ["Journal of Synthetic Chemistry"],
                   "issued": {"date-parts": [[2019, 1, 1]]},
                   "published-print": {"date-parts": [[2021, 6, 1]]},
                   "published-online": {"date-parts": [[2020, 12, 1]]},
                   "created": {"date-parts": [[2021, 7, 1]]}}},
      "https://api.crossref.org/v1/works/10.0000/...  [VERIFIED 2026-09-30]",
      "Three publication dates that disagree. The conflict must be recorded, "
      "not averaged into a date nobody published.")

write("crossref", "not_found",
      {"status": "failed", "message-type": "unknown",
       "message": "DOI does not exist"},
      "https://api.crossref.org/v1/works/10.0000/definitely-missing  [VERIFIED 2026-09-30]",
      "The body Crossref returns alongside a 404.")


write(
    "unpaywall", "is_oa_unknown_status",
    {
        "_synthetic_values": True,
        "doi": "10.0000/synthetic.2027.0101",
        "title": "A record with a status our enum does not model",
        "year": 2027,
        "is_oa": True,
        "oa_status": "platinum",
        "best_oa_location": {
            "endpoint_id": None, "evidence": "deprecated", "host_type": "publisher",
            "is_best": True, "license": "cc0", "oa_date": "2027-01-01",
            "pmh_id": None, "repository_institution": None, "updated": "deprecated",
            "url": "https://example.org/pdf/101",
            "url_for_landing_page": "https://doi.org/10.0000/synthetic.2027.0101",
            "url_for_pdf": "https://example.org/pdf/101",
            "version": "publishedVersion",
        },
        "oa_locations": [],
    },
    "https://api.unpaywall.org/v2/{doi}?email=...  [PARTIALLY VERIFIED 2026-09-30]",
    "A status string outside the modelled set. It must degrade to UNKNOWN and "
    "the raw value must survive verbatim -- silently coercing it to gold would "
    "hand the record a policy path it never qualified for.",
    schema="LIVE_VERIFIED_FOR_SHAPE_ONLY",
    schema_unverified=[
        "the value 'platinum' is synthetic; the point is that an unmodelled "
        "value must not be mapped onto a modelled one",
        "the full set of oa_status values Unpaywall can return",
    ],
)

print("fixtures written to", ROOT)

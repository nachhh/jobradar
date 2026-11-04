"""Himalayas (himalayas.app): remote jobs worldwide, through its public search API.

Endpoint: GET /jobs/api/search?country=AR&page=N (20 jobs per page; search has no cursor).
Verified on 2026-11-04: 4,599 jobs open to Argentina, including "worldwide" ones (an empty
locationRestrictions list); about 230 pages.

Scope (decision for step s7): jobs open to Argentina, and only tech roles. Search has no category
filter, so every page is fetched and non-tech jobs are dropped here, by their parentCategories.
Dropping them at extract (like GetOnBoard's server-side categories) keeps raw_postings and, later,
Phase 2's LLM spend on the jobs that matter.

Auth: an optional free API key, sent as "Authorization: Bearer <key>". Without one, requests
share a public allowance with every keyless caller. Data refreshes once a day, so the pipeline
fetches Himalayas at most once a day.

Terms: credit Himalayas, link back, and send people to the job's `applicationLink`.
"""

import logging
from collections import Counter
from typing import Any

from jobradar.config import get_settings
from jobradar.ingest.http import Fetcher
from jobradar.ingest.posting import PostingIn, salary_period, utc_from_timestamp
from jobradar.ingest.sources.base import ExtractError, Payload, RawRecord, dedupe, make_record
from jobradar.ingest.text import html_to_text
from jobradar.models import Remote, Source

logger = logging.getLogger(__name__)

SEARCH_URL = "https://himalayas.app/jobs/api/search"
COUNTRY = "AR"  # ISO 3166 code; "Argentina" returns the same results
PAGE_SIZE = 20  # fixed by the API
MAX_PAGES = 400  # guard against a runaway loop (4,599 jobs = 230 pages on 2026-11-04)

# parentCategories that count as tech. The live run logs the categories it drops, to tune this.
TECH_CATEGORIES = frozenset({"Developer", "Data Science", "DevOps", "IT", "QA", "Engineering"})


def extract(fetcher: Fetcher) -> list[RawRecord]:
    key = get_settings().himalayas_api_key
    headers = {"Authorization": f"Bearer {key.get_secret_value()}"} if key else None
    records: list[RawRecord] = []
    dropped: Counter[str] = Counter()
    page = 1
    while True:
        body = fetcher.get_json(SEARCH_URL, params={"country": COUNTRY, "page": page}, headers=headers)
        if not isinstance(body, dict) or not isinstance(body.get("jobs"), list):
            raise ExtractError(f"himalayas page {page}: unexpected response shape")
        for job in body["jobs"]:
            if is_tech(job):
                records.append(make_record(Source.HIMALAYAS, job.get("guid"), job))
            else:
                dropped.update(job.get("parentCategories") or ["(none)"])

        total = body.get("totalCount", 0)
        if page * PAGE_SIZE >= total or not body["jobs"]:
            break
        if page >= MAX_PAGES:
            raise ExtractError(f"himalayas: more than {MAX_PAGES} pages")
        page += 1

    logger.info(
        "himalayas: kept %d tech jobs; dropped by category: %s", len(records), dict(dropped.most_common())
    )
    return dedupe(records)


def is_tech(job: dict[str, Any]) -> bool:
    return bool(TECH_CATEGORIES.intersection(job.get("parentCategories") or []))


# --- Parse (stage 2: transform) ---------------------------------------------------------------


def parse(payload: Payload) -> PostingIn:
    """One Himalayas job → PostingIn. All Himalayas jobs are remote; the countries are a list."""
    countries = payload.get("locationRestrictions") or []
    has_salary = payload.get("minSalary") is not None or payload.get("maxSalary") is not None
    return PostingIn(
        source=Source.HIMALAYAS,
        src_posting_id=payload["guid"],
        url=payload["applicationLink"],  # the terms ask to send people here
        title=payload["title"],
        company=payload.get("companyName"),
        # An empty list means open worldwide (Himalayas' convention).
        location_text=", ".join(countries) if countries else "Worldwide",
        remote=Remote.REMOTE,
        seniority=", ".join(payload.get("seniority") or []),
        salary_min=payload.get("minSalary"),
        salary_max=payload.get("maxSalary"),
        salary_currency=payload.get("currency") if has_salary else None,
        salary_period=salary_period(payload.get("salaryPeriod")) if has_salary else None,
        posted_at=utc_from_timestamp(payload["pubDate"]) if payload.get("pubDate") else None,
        description=html_to_text(payload["description"]),
        # Categories are slugs ("Remote-Web-Developer"): spaces read better and match better.
        tags=tuple(c.replace("-", " ") for c in payload.get("categories") or ()),
    )

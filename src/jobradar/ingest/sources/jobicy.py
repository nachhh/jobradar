"""Jobicy (jobicy.com): remote jobs, through its public API.

Endpoint: GET /api/v2/remote-jobs?industry=…&geo=latam&count=100, paged with `cursor` while the
response says hasMore. Verified on 2026-11-04: geo=latam returns jobs open to LATAM plus
"Anywhere" ones; engineering had 29 and data-science 39, one page each.

Scope (decision for step s7): the tech industries, open to LATAM, filtered server-side.
Terms: poll at most once an hour, credit Jobicy, and link to the original Jobicy job page.
"""

from datetime import datetime

from jobradar.ingest.http import Fetcher
from jobradar.ingest.posting import PostingIn, salary_period
from jobradar.ingest.sources.base import ExtractError, Payload, RawRecord, dedupe, make_record
from jobradar.ingest.text import html_to_text
from jobradar.models import Remote, Source

URL = "https://jobicy.com/api/v2/remote-jobs"
# Slugs from ?get=industries: Software Engineering, Data Science & Analytics, QA & Testing.
INDUSTRIES = ("engineering", "data-science", "qa-testing")
GEO = "latam"
PER_PAGE = 100  # the maximum
MAX_PAGES_PER_INDUSTRY = 20


def extract(fetcher: Fetcher) -> list[RawRecord]:
    records: list[RawRecord] = []
    for industry in INDUSTRIES:
        params: dict[str, str | int] = {"industry": industry, "geo": GEO, "count": PER_PAGE}
        for _ in range(MAX_PAGES_PER_INDUSTRY):
            body = fetcher.get_json(URL, params=params)
            if not isinstance(body, dict) or not isinstance(body.get("jobs"), list):
                raise ExtractError(f"jobicy {industry}: unexpected response shape")
            records.extend(make_record(Source.JOBICY, job.get("id"), job) for job in body["jobs"])
            if not body.get("hasMore") or not body.get("nextCursor"):
                break
            # The cursor continues the same query; the docs require the filters to stay unchanged.
            params = params | {"cursor": body["nextCursor"]}
        else:  # for-else: runs only if the loop never hit `break`
            raise ExtractError(f"jobicy {industry}: more than {MAX_PAGES_PER_INDUSTRY} pages")
    # One job can be listed under two industries.
    return dedupe(records)


# --- Parse (stage 2: transform) ---------------------------------------------------------------


def parse(payload: Payload) -> PostingIn:
    """One Jobicy job → PostingIn. All Jobicy jobs are remote; jobGeo says where from."""
    has_salary = payload.get("salaryMin") is not None or payload.get("salaryMax") is not None
    level = payload.get("jobLevel")
    return PostingIn(
        source=Source.JOBICY,
        src_posting_id=str(payload["id"]),
        url=payload["url"],
        title=payload["jobTitle"],
        company=payload.get("companyName"),
        location_text=payload.get("jobGeo"),
        remote=Remote.REMOTE,
        seniority=None if level == "Any" else level,  # "Any" says nothing about seniority
        salary_min=payload.get("salaryMin"),
        salary_max=payload.get("salaryMax"),
        salary_currency=payload.get("salaryCurrency") if has_salary else None,
        salary_period=salary_period(payload.get("salaryPeriod")) if has_salary else None,
        # ISO 8601 with an offset ("2026-11-04T03:37:22+00:00"), so already time-zone-aware.
        posted_at=datetime.fromisoformat(payload["pubDate"]) if payload.get("pubDate") else None,
        description=html_to_text(payload["jobDescription"]),
        tags=tuple(payload.get("jobIndustry") or ()),
    )

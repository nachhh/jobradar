"""Remotive (remotive.com): remote jobs, through its public API.

Terms of use: at most 4 requests a day, jobs are delayed 24 h, and anything shown to a user must
link back to the posting URL and credit Remotive. So this extractor makes exactly ONE request per
run: all categories at once, no paging. Twice-a-day runs then use 2 of the 4 daily requests, and
the pipeline skips Remotive if it succeeded less than 6 hours ago (Phase 1 spec).

Response shape (documented at remotive.com/api-documentation):
    {"job-count": N, "jobs": [{"id", "url", "title", "company_name", "category", "tags",
     "job_type", "publication_date", "candidate_required_location", "salary", "description"}, …]}

Verified on 2026-11-04: the free API returns only 10 jobs in total ("total-job-count": 10), mixed
categories, despite the docs' "default: all"; the full list is a paid API. Each payload also has a
`company_logo_url` field, unused here.

The test fixture is hand-written from the documentation (so tests spend no daily request). The
first live run confirmed it has the real shape; it stays hand-written because it's built to cover
cases (an empty salary, a "S.A." company) that 10 live jobs may not.
"""

from datetime import UTC, datetime

from jobradar.ingest.http import Fetcher
from jobradar.ingest.posting import PostingIn
from jobradar.ingest.sources.base import ExtractError, Payload, RawRecord, dedupe, make_record
from jobradar.ingest.text import html_to_text
from jobradar.models import Remote, Source

URL = "https://remotive.com/api/remote-jobs"


def extract(fetcher: Fetcher) -> list[RawRecord]:
    body = fetcher.get_json(URL)
    if not isinstance(body, dict) or not isinstance(body.get("jobs"), list):
        raise ExtractError("remotive: unexpected response shape")
    return dedupe(make_record(Source.REMOTIVE, job.get("id"), job) for job in body["jobs"])


# --- Parse (stage 2: transform) ---------------------------------------------------------------


def parse(payload: Payload) -> PostingIn:
    """One Remotive job → PostingIn. Every Remotive job is remote by definition.

    The salary is free text ("$110k - $140k", "USD 4,000 - 5,500 / month"), kept as salary_text;
    turning it into numbers is left to Phase 2, like HN's free text.
    """
    return PostingIn(
        source=Source.REMOTIVE,
        src_posting_id=str(payload["id"]),
        url=payload["url"],
        title=payload["title"],
        company=payload.get("company_name"),
        location_text=payload.get("candidate_required_location"),
        remote=Remote.REMOTE,
        salary_text=payload.get("salary"),
        posted_at=_utc(payload.get("publication_date")),
        description=html_to_text(payload["description"]),
        tags=tuple(payload.get("tags") or ()),
    )


def _utc(value: str | None) -> datetime | None:
    """Remotive dates have no time zone ("2026-10-30T10:15:22"); they are taken as UTC."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

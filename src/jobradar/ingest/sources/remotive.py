"""Remotive (remotive.com): remote jobs, through its public API.

Terms of use: at most 4 requests a day, jobs are delayed 24 h, and anything shown to a user must
link back to the posting URL and credit Remotive. So this extractor makes exactly ONE request per
run: all categories at once, no paging. Twice-a-day runs then use 2 of the 4 daily requests, and
the pipeline skips Remotive if it succeeded less than 6 hours ago (Phase 1 spec).

Response shape (documented at remotive.com/api-documentation):
    {"job-count": N, "jobs": [{"id", "url", "title", "company_name", "category", "tags",
     "job_type", "publication_date", "candidate_required_location", "salary", "description"}, …]}

The test fixture is written from that documentation, not recorded, so that no daily request is
spent on tests; it will be replaced with payloads from the first real run (kept in raw_postings).
"""

from jobradar.ingest.http import Fetcher
from jobradar.ingest.sources.base import ExtractError, RawRecord, dedupe, make_record
from jobradar.models import Source

URL = "https://remotive.com/api/remote-jobs"


def extract(fetcher: Fetcher) -> list[RawRecord]:
    body = fetcher.get_json(URL)
    if not isinstance(body, dict) or not isinstance(body.get("jobs"), list):
        raise ExtractError("remotive: unexpected response shape")
    return dedupe(make_record(Source.REMOTIVE, job.get("id"), job) for job in body["jobs"])

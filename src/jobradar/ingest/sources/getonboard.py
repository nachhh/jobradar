"""GetOnBoard (getonbrd.com): LATAM tech jobs, through its public JSON:API.

Endpoint: GET /api/v0/categories/{category}/jobs?page=N&per_page=100&expand[]=…
Verified on 2026-11-03: 602 postings over the four categories, 9 requests (~15 s).

The response follows the JSON:API convention: {"data": [...], "meta": {"page", "total_pages"}},
each item {"id", "type", "attributes", "links"}. Related objects (company, seniority, tags,
modality) come back as bare IDs unless listed in `expand[]`, which inlines them; that saves one
request per company and keeps everything the parser needs in the posting's own payload.
"""

from jobradar.ingest.http import Fetcher
from jobradar.ingest.sources.base import ExtractError, RawRecord, dedupe, make_record
from jobradar.models import Source

BASE_URL = "https://www.getonbrd.com/api/v0"

# Whole categories rather than keyword searches (spec decision): one broad corpus for any profile.
CATEGORIES = ("programming", "data-science-analytics", "machine-learning-ai", "sysadmin-devops-qa")

PER_PAGE = 100  # the largest page size the API accepts; fewer pages = fewer requests
EXPAND = ["company", "seniority", "tags", "modality"]

# A guard against an API that keeps reporting more pages: stop instead of looping forever.
MAX_PAGES_PER_CATEGORY = 50

# Fields that change without the posting changing (see base.payload_hash).
VOLATILE = (
    "attributes.applications_count",
    "attributes.company.data.attributes.response_time_in_days",
)


def extract(fetcher: Fetcher) -> list[RawRecord]:
    records: list[RawRecord] = []
    for category in CATEGORIES:
        records.extend(_extract_category(fetcher, category))
    return dedupe(records)


def _extract_category(fetcher: Fetcher, category: str) -> list[RawRecord]:
    records: list[RawRecord] = []
    page = 1
    while True:
        body = fetcher.get_json(
            f"{BASE_URL}/categories/{category}/jobs",
            params={"page": page, "per_page": PER_PAGE, "expand[]": EXPAND},
        )
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ExtractError(f"getonboard {category} page {page}: unexpected response shape")
        for item in body["data"]:
            records.append(make_record(Source.GETONBOARD, item.get("id"), item, VOLATILE))

        total_pages = body.get("meta", {}).get("total_pages", 1)
        # An empty page also ends the loop, in case total_pages is wrong.
        if page >= total_pages or not body["data"]:
            return records
        if page >= MAX_PAGES_PER_CATEGORY:
            raise ExtractError(f"getonboard {category}: more than {MAX_PAGES_PER_CATEGORY} pages")
        page += 1

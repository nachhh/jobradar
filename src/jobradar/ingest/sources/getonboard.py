"""GetOnBoard (getonbrd.com): LATAM tech jobs, through its public JSON:API.

Endpoint: GET /api/v0/categories/{category}/jobs?page=N&per_page=100&expand[]=…
Verified on 2026-11-03: 602 postings over the four categories, 9 requests (~15 s).

The response follows the JSON:API convention: {"data": [...], "meta": {"page", "total_pages"}},
each item {"id", "type", "attributes", "links"}. Related objects (company, seniority, tags,
modality) come back as bare IDs unless listed in `expand[]`, which inlines them; that saves one
request per company and keeps everything the parser needs in the posting's own payload.
"""

from typing import Any

from jobradar.ingest.http import Fetcher
from jobradar.ingest.posting import PostingIn, utc_from_timestamp
from jobradar.ingest.sources.base import ExtractError, Payload, RawRecord, dedupe, make_record
from jobradar.ingest.text import html_to_text
from jobradar.models import Remote, SalaryPeriod, Source

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


# --- Parse (stage 2: transform) ---------------------------------------------------------------

# GetOnBoard's remote_modality → our Remote values. "remote_local" is remote within a country or
# region (e.g. "remote, Chile only"); for the remote/hybrid/onsite question it's still remote.
REMOTE_MODALITY = {
    "fully_remote": Remote.REMOTE,
    "remote_local": Remote.REMOTE,
    "hybrid": Remote.HYBRID,
    "no_remote": Remote.ONSITE,
}

# The description is split into HTML sections, each with an optional headline (sometimes ""). They
# are joined in this order, with these labels when the posting gives none.
SECTIONS = (
    ("functions", "Responsibilities"),
    ("description", "Requirements"),
    ("desirable", "Nice to have"),
    ("benefits", "Benefits"),
    ("projects", "About the company"),
)


def parse(payload: Payload) -> PostingIn:
    """One GetOnBoard job (as stored in raw_postings) → PostingIn.

    Missing required keys raise KeyError on purpose: the transform turns it into a rejection
    with the reason, which is how an API change becomes visible.
    """
    attrs = payload["attributes"]
    has_salary = attrs.get("min_salary") is not None or attrs.get("max_salary") is not None
    return PostingIn(
        source=Source.GETONBOARD,
        src_posting_id=payload["id"],
        url=payload["links"]["public_url"],
        title=attrs["title"],
        company=_related(attrs, "company"),
        location_text=", ".join(attrs.get("countries") or []),
        remote=REMOTE_MODALITY.get(attrs.get("remote_modality") or "", Remote.UNKNOWN),
        seniority=_related(attrs, "seniority"),
        salary_min=attrs.get("min_salary"),
        salary_max=attrs.get("max_salary"),
        # GetOnBoard publishes salaries in USD per month; the unit is only stated when there is one.
        salary_currency="USD" if has_salary else None,
        salary_period=SalaryPeriod.MONTH if has_salary else None,
        posted_at=utc_from_timestamp(attrs["published_at"]) if attrs.get("published_at") else None,
        description=_description(attrs),
        tags=tuple(tag["attributes"]["name"] for tag in attrs.get("tags", {}).get("data", [])),
    )


def _related(attrs: dict[str, Any], name: str) -> str | None:
    """The `name` attribute of an expanded relation: {"data": {"attributes": {"name": …}}}."""
    data = (attrs.get(name) or {}).get("data") or {}
    return (data.get("attributes") or {}).get("name")


def _description(attrs: dict[str, Any]) -> str:
    """All non-empty sections as plain text, each under its headline."""
    blocks = []
    for key, default_label in SECTIONS:
        body = html_to_text(attrs.get(key))
        if body:
            headline = (attrs.get(f"{key}_headline") or default_label).strip()
            blocks.append(f"{headline}\n{body}")
    return "\n\n".join(blocks)

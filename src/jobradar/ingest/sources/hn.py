"""Hacker News "Ask HN: Who is hiring?" threads, through the Algolia HN API.

Two requests per thread, both free and unauthenticated:
1. search_by_date?tags=story,author_whoishiring lists the stories posted by the `whoishiring` bot.
   It posts three each month (Who is hiring / Who wants to be hired / Freelancer), so they're
   filtered by title.
2. items/{story_id} returns the story with its whole comment tree. Top-level comments are the
   job postings; replies under them are conversation and are dropped.

Verified on 2026-11-03: the October 2026 thread has 259 top-level comments (~470 KB response).
"""

from typing import Any

from jobradar.ingest.http import Fetcher
from jobradar.ingest.sources.base import ExtractError, Payload, RawRecord, dedupe, make_record
from jobradar.models import Source

BASE_URL = "https://hn.algolia.com/api/v1"
HIRING_TITLE_PREFIX = "Ask HN: Who is hiring?"
THREADS = 2  # the last two monthly threads (spec decision)

# An HN posting goes inactive when its comment is older than this, or when it was deleted from a
# thread fetched in the run (spec, "Closed postings"). Applied by the pipeline, not the extractor.
MAX_AGE_DAYS = 45


def extract(fetcher: Fetcher) -> list[RawRecord]:
    records: list[RawRecord] = []
    for story_id in _latest_hiring_story_ids(fetcher):
        story = fetcher.get_json(f"{BASE_URL}/items/{story_id}")
        for comment in story.get("children", []):
            payload = _posting_payload(comment)
            if payload is not None:
                records.append(make_record(Source.HN, comment.get("id"), payload))
    return dedupe(records)


def _latest_hiring_story_ids(fetcher: Fetcher) -> list[int]:
    # 10 hits cover more than three months of whoishiring posts (three stories a month).
    body = fetcher.get_json(
        f"{BASE_URL}/search_by_date", params={"tags": "story,author_whoishiring", "hitsPerPage": 10}
    )
    hiring = [hit for hit in body.get("hits", []) if hit.get("title", "").startswith(HIRING_TITLE_PREFIX)]
    if not hiring:
        raise ExtractError("hn: no 'Who is hiring?' story found")
    # search_by_date already returns newest first; sorting makes that an explicit guarantee.
    hiring.sort(key=lambda hit: hit["created_at_i"], reverse=True)
    return [int(hit["objectID"]) for hit in hiring[:THREADS]]


def _posting_payload(comment: dict[str, Any]) -> Payload | None:
    """The comment without its replies, or None for a deleted or flagged comment.

    Deleted comments have no text and no author. Replies are removed because they aren't part of
    the posting, and a new reply would otherwise change the hash and look like a new version.
    """
    if not comment.get("text") or not comment.get("author"):
        return None
    return {key: value for key, value in comment.items() if key != "children"}

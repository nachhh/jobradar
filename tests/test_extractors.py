"""Unit tests for the three extractors, run against the saved responses in tests/fixtures/.

Each test declares the API routes with respx, serving fixture files, then checks what the
extractor returns: IDs, deduplication, dropped records, request counts. No network involved.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from jobradar.ingest.http import Fetcher, make_client
from jobradar.ingest.sources import getonboard, hn, remotive
from jobradar.ingest.sources.base import ExtractError, dedupe, make_record, payload_hash
from jobradar.models import Source

FIXTURES = Path(__file__).parent / "fixtures"


def load(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text())


@pytest.fixture
def fetcher() -> Fetcher:
    # No waiting in tests: no polite interval, and backoff sleeps do nothing.
    return Fetcher(make_client(), min_interval=0.0, sleep=lambda _seconds: None)


# --- shared helpers ---------------------------------------------------------------------------


def test_hash_ignores_key_order() -> None:
    assert payload_hash({"a": 1, "b": {"c": 2, "d": 3}}) == payload_hash({"b": {"d": 3, "c": 2}, "a": 1})


def test_hash_ignores_volatile_fields_but_payload_keeps_them() -> None:
    before = {"id": "x", "attributes": {"title": "DE", "applications_count": 5}}
    after = {"id": "x", "attributes": {"title": "DE", "applications_count": 9}}
    volatile = ["attributes.applications_count"]

    assert payload_hash(before, volatile) == payload_hash(after, volatile)
    assert payload_hash(before) != payload_hash(after)  # without the exclusion they differ
    assert before["attributes"]["applications_count"] == 5  # the original was not modified


def test_hash_tolerates_missing_volatile_paths() -> None:
    assert payload_hash({"id": 1}, ["attributes.company.data"]) == payload_hash({"id": 1})


def test_record_without_id_is_an_error() -> None:
    with pytest.raises(ExtractError, match="without an id"):
        make_record(Source.REMOTIVE, None, {"title": "no id"})


def test_dedupe_keeps_first() -> None:
    first = make_record(Source.HN, 1, {"v": 1})
    records = [first, make_record(Source.HN, 2, {"v": 2}), make_record(Source.HN, 1, {"v": 3})]
    assert dedupe(records) == [first, records[1]]


# --- GetOnBoard -------------------------------------------------------------------------------


def test_getonboard_pages_and_dedupes(respx_mock: respx.MockRouter, fetcher: Fetcher) -> None:
    base = f"{getonboard.BASE_URL}/categories"
    # `params` matches when the request's query contains these values (other params may be present).
    respx_mock.get(f"{base}/programming/jobs", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json=load("getonboard/jobs-page-1.json"))
    )
    respx_mock.get(f"{base}/programming/jobs", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json=load("getonboard/jobs-page-2.json"))
    )
    for category in getonboard.CATEGORIES[1:]:
        respx_mock.get(f"{base}/{category}/jobs").mock(
            return_value=httpx.Response(200, json=load("getonboard/jobs-empty.json"))
        )

    records = getonboard.extract(fetcher)

    # 3 on page 1 + 2 on page 2, one of which repeats page 1 → 4 distinct postings.
    assert [r.src_posting_id for r in records] == [
        "founding-ai-engineer-sapling-remote",
        "ai-engineer-rag-intelligent-agents-dynamic-devs-santiago-61bd",
        "ai-sales-specialist-agendapro-santiago",
        "ia-developer-improving-remote",
    ]
    # Expanded relations are inlined in the payload the transform will parse.
    company = records[0].payload["attributes"]["company"]["data"]["attributes"]
    assert company["name"]
    # 2 pages + one request for each of the 3 empty categories.
    assert fetcher.requests_made == 5


def test_getonboard_rejects_unexpected_shape(respx_mock: respx.MockRouter, fetcher: Fetcher) -> None:
    respx_mock.get(url__startswith=getonboard.BASE_URL).mock(
        return_value=httpx.Response(200, json={"error": "changed"})
    )
    with pytest.raises(ExtractError, match="unexpected response shape"):
        getonboard.extract(fetcher)


# --- Hacker News ------------------------------------------------------------------------------


def _mock_hn(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{hn.BASE_URL}/search_by_date").mock(
        return_value=httpx.Response(200, json=load("hn/stories.json"))
    )
    for story_id in (50338728, 49922569):
        respx_mock.get(f"{hn.BASE_URL}/items/{story_id}").mock(
            return_value=httpx.Response(200, json=load(f"hn/item-{story_id}.json"))
        )


def test_hn_reads_the_last_two_hiring_threads(respx_mock: respx.MockRouter, fetcher: Fetcher) -> None:
    _mock_hn(respx_mock)

    records = hn.extract(fetcher)

    # November: 3 postings + 1 deleted comment (skipped); October: 2 postings. The "Who wants to be
    # hired?" and September stories in the fixture are not fetched (unmatched routes would fail).
    assert len(records) == 5
    assert "50399999" not in {r.src_posting_id for r in records}
    assert {r.payload["story_id"] for r in records} == {50338728, 49922569}


def test_hn_drops_replies_from_the_payload(respx_mock: respx.MockRouter, fetcher: Fetcher) -> None:
    _mock_hn(respx_mock)
    records = {r.src_posting_id: r for r in hn.extract(fetcher)}

    # 50338732 has two replies in the recorded thread; they must not be part of the posting.
    assert "children" not in records["50338732"].payload
    assert records["50338732"].payload["text"].startswith("Simple Analytics | DevOps Engineer")


# --- Remotive ---------------------------------------------------------------------------------


def test_remotive_makes_one_request(respx_mock: respx.MockRouter, fetcher: Fetcher) -> None:
    route = respx_mock.get(remotive.URL).mock(
        return_value=httpx.Response(200, json=load("remotive/remote-jobs.json"))
    )

    records = remotive.extract(fetcher)

    assert [r.src_posting_id for r in records] == ["2084113", "2084250", "2084301"]  # int IDs → str
    assert route.call_count == 1  # the daily limit is 4: never page or retry-by-category

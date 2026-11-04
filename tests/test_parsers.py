"""Unit tests for the text helpers and the per-source parsers, using the saved fixtures."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jobradar.ingest.sources import getonboard, hn, remotive
from jobradar.ingest.text import dedup_key, html_to_text, normalize_name
from jobradar.models import Remote, Source

FIXTURES = Path(__file__).parent / "fixtures"


def load(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text())


def getonboard_job(job_id: str) -> dict[str, Any]:
    jobs = load("getonboard/jobs-page-1.json")["data"] + load("getonboard/jobs-page-2.json")["data"]
    return next(job for job in jobs if job["id"] == job_id)


# --- text helpers -----------------------------------------------------------------------------


def test_html_to_text_keeps_structure_and_decodes_entities() -> None:
    html = "<p>Hi &amp; welcome<p>We use:<ul><li>Python</li><li>SQL &#x2F; dbt</li></ul><br>Bye&nbsp;now"
    assert html_to_text(html) == "Hi & welcome\nWe use:\n- Python\n- SQL / dbt\nBye now"


def test_html_to_text_handles_empty() -> None:
    assert html_to_text(None) == ""
    assert html_to_text("<p></p>") == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Datafy S.A.", "datafy"),
        ("ACME, Inc.", "acme"),
        ("Ingeniería Ñandú Ltda.", "ingenieria nandu"),
        ("Inc", "inc"),  # a name that is only a suffix is kept, not emptied
    ],
)
def test_normalize_company(raw: str, expected: str) -> None:
    assert normalize_name(raw, drop_legal_suffixes=True) == expected


def test_dedup_key_needs_company_and_title() -> None:
    assert dedup_key("Acme Inc.", "Senior Data Engineer!") == "acme|senior data engineer"
    assert dedup_key(None, "Senior Data Engineer") is None
    assert dedup_key("Acme", "") is None


# --- GetOnBoard -------------------------------------------------------------------------------


def test_getonboard_maps_structured_fields() -> None:
    posting = getonboard.parse(getonboard_job("founding-ai-engineer-sapling-remote"))

    assert posting.source == Source.GETONBOARD
    assert posting.url == "https://www.getonbrd.com/jobs/founding-ai-engineer-sapling-remote"
    assert (posting.title, posting.company, posting.seniority) == (
        "Founding AI Engineer",
        "Sapling",
        "Senior",
    )
    assert (posting.salary_min, posting.salary_max) == (4500, 7000)
    assert posting.remote == Remote.REMOTE
    assert posting.posted_at is not None and posting.posted_at.tzinfo is not None
    assert "Large Language Models" in posting.tags
    assert "<" not in posting.description  # HTML stripped


@pytest.mark.parametrize(
    ("job_id", "remote"),
    [
        ("ai-engineer-rag-intelligent-agents-dynamic-devs-santiago-61bd", Remote.HYBRID),
        ("ai-sales-specialist-agendapro-santiago", Remote.ONSITE),
        ("ia-developer-improving-remote", Remote.REMOTE),  # remote_local
    ],
)
def test_getonboard_remote_modality(job_id: str, remote: Remote) -> None:
    assert getonboard.parse(getonboard_job(job_id)).remote == remote


def test_getonboard_description_uses_default_headline_when_empty() -> None:
    job = getonboard_job("founding-ai-engineer-sapling-remote")
    job["attributes"]["benefits_headline"] = ""
    assert "\nBenefits\n" in getonboard.parse(job).description


def test_getonboard_rejects_inverted_salary() -> None:
    job = getonboard_job("founding-ai-engineer-sapling-remote")
    job["attributes"]["min_salary"], job["attributes"]["max_salary"] = 7000, 4500
    with pytest.raises(ValidationError, match="salary_min 7000 > salary_max 4500"):
        getonboard.parse(job)


def test_getonboard_missing_title_is_an_error() -> None:
    job = getonboard_job("founding-ai-engineer-sapling-remote")
    del job["attributes"]["title"]
    with pytest.raises(KeyError):
        getonboard.parse(job)


# --- Hacker News ------------------------------------------------------------------------------


def test_hn_keeps_structured_fields_only() -> None:
    comment = load("hn/item-50338728.json")["children"][0]
    posting = hn.parse(comment)

    assert posting.src_posting_id == "50338732"
    assert posting.url == "https://news.ycombinator.com/item?id=50338732"
    # Decision D1: nothing is read out of the free text in Phase 1.
    assert (posting.title, posting.company, posting.location_text) == (None, None, None)
    assert posting.remote == Remote.UNKNOWN
    assert posting.description.startswith("Simple Analytics | DevOps Engineer")
    assert "&#x2F;" not in posting.description


# --- Remotive ---------------------------------------------------------------------------------


def test_remotive_maps_fields_and_assumes_utc() -> None:
    jobs = load("remotive/remote-jobs.json")["jobs"]
    first, second = remotive.parse(jobs[0]), remotive.parse(jobs[1])

    assert (first.title, first.company, first.remote) == (
        "Senior Data Engineer",
        "Acme Analytics",
        Remote.REMOTE,
    )
    assert first.salary_text == "$110k - $140k"
    assert first.posted_at is not None and first.posted_at.utcoffset() is not None
    assert second.salary_text is None  # "" in the payload means not given


def test_content_hash_changes_with_content_only() -> None:
    job = load("remotive/remote-jobs.json")["jobs"][0]
    same = remotive.parse(dict(job, company_logo="https://elsewhere/logo.png"))  # a field we don't keep
    edited = remotive.parse(dict(job, title="Staff Data Engineer"))

    assert remotive.parse(job).content_hash() == same.content_hash()
    assert remotive.parse(job).content_hash() != edited.content_hash()

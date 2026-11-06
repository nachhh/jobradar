"""Integration tests for the enrich pipeline, with the fake provider (no network, no cost).

They use `clean_db` rather than `db_conn`: the pipeline opens and commits its own transactions
(one per posting), which a rolled-back test connection couldn't contain.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import Engine, insert, select, update

from jobradar.enrich import pipeline
from jobradar.enrich.pipeline import NothingToEnrichError, run_enrich
from jobradar.llm.base import FatalProviderError, ProviderError
from jobradar.llm.fake import FakeProvider
from jobradar.models import LlmCall, PipelineRun, Posting, PostingEnrichment, RunStatus, Source

pytestmark = pytest.mark.integration


def answer(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "company": "Acme",
        "title": "Data Engineer",
        "seniority": "senior",
        "role_family": "data_engineering",
        "employment_type": "full_time",
        "remote": "remote",
        "latam_hiring": "yes",
        "latam_evidence": "open to LATAM",
        "location_restrictions": [],
        "skills_required": ["python"],
        "skills_nice": [],
        "years_experience_min": None,
        "languages": [],
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "salary_period": None,
        "salary_basis": "unknown",
        "summary": "Builds pipelines.",
    }
    return base | overrides


def add_postings(engine: Engine, count: int, **overrides: Any) -> list[int]:
    """Insert `count` active postings (with a run to own them) and return their ids."""
    with engine.begin() as conn:
        run_id = conn.execute(
            insert(PipelineRun).values(pipeline="ingest", trigger="manual").returning(PipelineRun.run_id)
        ).scalar_one()
        ids = []
        for i in range(count):
            row = {
                "source": Source.GETONBOARD,
                "src_posting_id": f"{uuid.uuid4()}",
                "url": f"https://example.com/{i}",
                "title": "Data Engineer",
                "company": "Acme",
                "description": f"Posting {i}. We are open to LATAM.",
                "content_hash": f"hash-{i}",
                "last_run_id": run_id,
            } | overrides
            ids.append(conn.execute(insert(Posting).values(row).returning(Posting.id)).scalar_one())
        return ids


def respond_always(**overrides: Any) -> Callable[[str], dict[str, Any]]:
    return lambda _text: answer(**overrides)


def enrichments(engine: Engine) -> dict[int, Any]:
    with engine.connect() as conn:
        return {r.posting_id: r for r in conn.execute(select(PostingEnrichment)).all()}


def call_statuses(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return list(conn.execute(select(LlmCall.status).order_by(LlmCall.id)).scalars())


def test_enriches_stale_postings_then_has_nothing_to_do(clean_db: Engine) -> None:
    ids = add_postings(clean_db, 3)
    add_postings(clean_db, 1, is_active=False)  # inactive: never enriched
    provider = FakeProvider(respond_always())

    result = run_enrich(clean_db, provider)

    assert result.status == RunStatus.SUCCEEDED
    assert (result.counts.stale, result.counts.ok, result.counts.calls) == (3, 3, 3)
    rows = enrichments(clean_db)
    assert sorted(rows) == ids
    assert rows[ids[0]].status == "ok"
    assert rows[ids[0]].source_content_hash == "hash-0"
    assert rows[ids[0]].dedup_key == "acme|data engineer"

    # Nothing changed: a second run makes no call at all.
    with pytest.raises(NothingToEnrichError):
        run_enrich(clean_db, provider)
    assert len(provider.calls) == 3


def test_changed_content_or_prompt_makes_postings_stale(
    clean_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = add_postings(clean_db, 2)
    provider = FakeProvider(respond_always())
    run_enrich(clean_db, provider)

    with clean_db.begin() as conn:
        conn.execute(update(Posting).where(Posting.id == ids[0]).values(content_hash="edited"))
    assert run_enrich(clean_db, provider).counts.stale == 1

    # A new prompt version makes every enrichment stale.
    monkeypatch.setattr(pipeline, "PROMPT_VERSION", pipeline.PROMPT_VERSION + "-next")
    assert run_enrich(clean_db, provider).counts.stale == 2


def test_invalid_answer_is_retried_once(clean_db: Engine) -> None:
    (posting_id,) = add_postings(clean_db, 1)
    answers = iter([answer(seniority="rockstar"), answer()])  # invalid, then valid
    provider = FakeProvider(lambda _text: next(answers))

    result = run_enrich(clean_db, provider)

    assert (result.counts.calls, result.counts.ok) == (2, 1)
    assert "previous answer was invalid" in provider.calls[1][1]  # the retry says what was wrong
    assert call_statuses(clean_db) == ["invalid_output", "ok"]
    assert enrichments(clean_db)[posting_id].status == "ok"


def test_failed_answers_are_skipped_after_max_attempts(clean_db: Engine) -> None:
    (posting_id,) = add_postings(clean_db, 1)
    provider = FakeProvider(respond_always(seniority="rockstar"))  # always invalid

    for attempt in range(1, pipeline.MAX_ATTEMPTS + 1):
        result = run_enrich(clean_db, provider)
        assert result.status == RunStatus.FAILED
        row = enrichments(clean_db)[posting_id]
        assert (row.status, row.attempts, row.summary) == ("failed", attempt, None)

    # Three failed attempts on the same content and prompt: skipped from now on.
    with pytest.raises(NothingToEnrichError):
        run_enrich(clean_db, provider)


def test_api_error_is_not_an_attempt(clean_db: Engine) -> None:
    (posting_id,) = add_postings(clean_db, 1)

    def outage(_text: str) -> dict[str, Any]:
        raise ProviderError("503 overloaded", model="fake-model")

    result = run_enrich(clean_db, FakeProvider(outage))

    assert result.counts.errors == 1
    assert posting_id not in enrichments(clean_db)  # nothing written: still stale
    assert call_statuses(clean_db) == ["error"]  # but the call is on record
    assert run_enrich(clean_db, FakeProvider(respond_always())).counts.ok == 1


def test_fatal_error_stops_the_run(clean_db: Engine) -> None:
    add_postings(clean_db, 20)

    def bad_key(_text: str) -> dict[str, Any]:
        raise FatalProviderError("401 invalid x-api-key", model="fake-model")

    result = run_enrich(clean_db, FakeProvider(bad_key), concurrency=1)

    assert result.status == RunStatus.FAILED
    assert result.counts.calls == 1  # stopped at the first one instead of failing all 20


def test_ungrounded_answer_keeps_fields_but_not_the_latam_claim(clean_db: Engine) -> None:
    (posting_id,) = add_postings(clean_db, 1)
    provider = FakeProvider(respond_always(latam_evidence="We hire across Latin America"))  # not in the text

    result = run_enrich(clean_db, provider)

    assert (result.counts.ok, result.counts.ungrounded) == (1, 1)
    row = enrichments(clean_db)[posting_id]
    assert (row.status, row.latam_hiring, row.seniority) == ("ok", "unknown", "senior")
    assert call_statuses(clean_db) == ["ungrounded"]


def test_max_calls_leaves_the_rest_stale(clean_db: Engine) -> None:
    add_postings(clean_db, 5)
    result = run_enrich(clean_db, FakeProvider(respond_always()), max_calls=2, concurrency=1)

    assert (result.counts.calls, result.counts.ok) == (2, 2)
    assert result.status == RunStatus.PARTIAL
    assert run_enrich(clean_db, FakeProvider(respond_always())).counts.stale == 3


def test_limit_applies_per_source_when_sources_are_given(clean_db: Engine) -> None:
    add_postings(clean_db, 3)
    add_postings(clean_db, 3, source=Source.HN)
    add_postings(clean_db, 3, source=Source.JOBICY)

    stale = pipeline.find_stale(clean_db, limit=2, sources=[Source.GETONBOARD, Source.HN])

    assert sorted(r["source"] for r in stale) == ["getonboard", "getonboard", "hn", "hn"]


def test_hn_posting_gets_a_dedup_key_from_the_llm(clean_db: Engine) -> None:
    """HN postings have no company or title in the source; the enrichment's key enables linking."""
    (posting_id,) = add_postings(clean_db, 1, source=Source.HN, company=None, title=None)
    run_enrich(clean_db, FakeProvider(respond_always(company="Globex Inc.", title="ML Engineer")))
    assert enrichments(clean_db)[posting_id].dedup_key == "globex|ml engineer"


def test_failure_after_success_clears_the_old_fields(clean_db: Engine) -> None:
    (posting_id,) = add_postings(clean_db, 1)
    run_enrich(clean_db, FakeProvider(respond_always()))
    with clean_db.begin() as conn:
        conn.execute(update(Posting).where(Posting.id == posting_id).values(content_hash="edited"))

    run_enrich(clean_db, FakeProvider(respond_always(seniority="rockstar")))

    row = enrichments(clean_db)[posting_id]
    assert (row.status, row.attempts, row.seniority, row.skills_required) == ("failed", 1, None, [])

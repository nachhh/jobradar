"""Integration tests for the pipeline: the guarantees listed in the Phase 1 spec, "Tests".

Extractors are replaced by fakes that serve the fixture payloads and count their calls, so these
tests exercise the ledger, transactions, lock and recovery without any HTTP.
"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, func, select, text, update
from typer.testing import CliRunner

from jobradar import cli
from jobradar.ingest import pipeline
from jobradar.ingest.http import Fetcher
from jobradar.ingest.posting import PostingIn
from jobradar.ingest.sources import PARSERS
from jobradar.ingest.sources.base import Payload, RawRecord, make_record
from jobradar.models import PipelineRunSource, Posting, RawPosting, RejectedPosting, RunStatus, Source

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures"


def load(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text())


class FakeExtractor:
    """Stands in for a source's extract(): returns fixed payloads and counts how often it ran."""

    def __init__(self, source: Source, payloads: list[Payload]) -> None:
        self.source = source
        self.payloads = payloads
        self.calls = 0

    def __call__(self, fetcher: Fetcher) -> list[RawRecord]:
        self.calls += 1
        return [make_record(self.source, p["id"], p) for p in self.payloads]


def fakes() -> dict[Source, FakeExtractor]:
    november = [
        {k: v for k, v in c.items() if k != "children"} for c in load("hn/item-50338728.json")["children"]
    ]
    return {
        Source.GETONBOARD: FakeExtractor(Source.GETONBOARD, load("getonboard/jobs-page-1.json")["data"]),
        Source.REMOTIVE: FakeExtractor(Source.REMOTIVE, load("remotive/remote-jobs.json")["jobs"]),
        Source.HN: FakeExtractor(Source.HN, [c for c in november if c["text"]]),
    }


def no_http() -> Fetcher:
    return Fetcher(httpx.Client())  # never used by the fakes; closed by the pipeline


def ingest(engine: Engine, extractors: dict[Source, FakeExtractor], **kwargs: Any) -> pipeline.RunResult:
    return pipeline.run_ingest(engine, extractors=extractors, make_fetcher=no_http, **kwargs)


def source_rows(engine: Engine, run_id: uuid.UUID) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = conn.execute(select(PipelineRunSource).where(PipelineRunSource.run_id == run_id)).all()
    return {row.source: row for row in rows}


def broken_transform(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("parser bug")


def count(engine: Engine, model: type, *where: Any) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(model).where(*where)).scalar_one()


# --- loading and idempotency ------------------------------------------------------------------


def test_run_loads_every_source_and_rerun_is_unchanged(clean_db: Engine) -> None:
    first = ingest(clean_db, fakes())

    assert first.status == RunStatus.SUCCEEDED
    rows = source_rows(clean_db, first.run_id)
    assert {s: (r.fetched, r.inserted, r.stage) for s, r in rows.items()} == {
        "getonboard": (3, 3, "transformed"),
        "remotive": (3, 3, "transformed"),
        "hn": (3, 3, "transformed"),
    }

    second = ingest(clean_db, fakes(), force=True)  # force: Remotive ran seconds ago

    rows = source_rows(clean_db, second.run_id)
    assert {s: (r.inserted, r.updated, r.unchanged) for s, r in rows.items()} == {
        "getonboard": (0, 0, 3),
        "remotive": (0, 0, 3),
        "hn": (0, 0, 3),
    }
    assert count(clean_db, RawPosting) == 9  # no new versions
    assert count(clean_db, Posting) == 9


def test_remotive_is_skipped_within_six_hours_unless_forced(clean_db: Engine) -> None:
    ingest(clean_db, fakes())
    extractors = fakes()

    result = ingest(clean_db, extractors)
    assert result.skipped == (Source.REMOTIVE,)
    assert extractors[Source.REMOTIVE].calls == 0

    with pytest.raises(pipeline.NothingToRunError):
        ingest(clean_db, fakes(), sources=[Source.REMOTIVE])

    forced = ingest(clean_db, extractors, sources=[Source.REMOTIVE], force=True)
    assert forced.status == RunStatus.SUCCEEDED
    assert extractors[Source.REMOTIVE].calls == 1


# --- failures and recovery --------------------------------------------------------------------


def test_crash_during_load_leaves_no_rows_for_that_source(
    clean_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The raw rows are inserted, then the ledger update fails: the whole stage must roll back."""
    real_mark = pipeline._mark_extracted

    def crash_for_hn(conn: Any, run_id: uuid.UUID, source: Source, *, fetched: int) -> None:
        if source == Source.HN:
            raise RuntimeError("simulated crash after the raw INSERT")
        real_mark(conn, run_id, source, fetched=fetched)

    monkeypatch.setattr(pipeline, "_mark_extracted", crash_for_hn)
    result = ingest(clean_db, fakes())

    assert result.status == RunStatus.PARTIAL
    hn = source_rows(clean_db, result.run_id)["hn"]
    assert (hn.status, hn.stage, hn.error_type) == ("failed", None, "RuntimeError")
    assert count(clean_db, RawPosting, RawPosting.source == Source.HN) == 0
    assert count(clean_db, RawPosting, RawPosting.source == Source.GETONBOARD) == 3


def test_failed_transform_keeps_raw_and_resume_finishes_without_fetching(
    clean_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_transform = pipeline.transform_source
    monkeypatch.setattr(pipeline, "transform_source", broken_transform)
    extractors = fakes()
    result = ingest(clean_db, extractors)

    assert result.status == RunStatus.FAILED
    assert all(r.stage == "extracted" for r in source_rows(clean_db, result.run_id).values())
    assert count(clean_db, RawPosting) == 9  # what was fetched is safe
    assert count(clean_db, Posting) == 0

    monkeypatch.setattr(pipeline, "transform_source", real_transform)  # "the bug is fixed"
    resumed = pipeline.resume_ingest(clean_db, result.run_id, extractors=extractors, make_fetcher=no_http)

    assert resumed.status == RunStatus.SUCCEEDED
    assert all(e.calls == 1 for e in extractors.values())  # no source was fetched again
    assert count(clean_db, Posting) == 9


def test_resume_refuses_a_run_that_is_not_the_latest(
    clean_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "transform_source", broken_transform)
    old = ingest(clean_db, fakes())
    ingest(clean_db, fakes(), force=True)

    with pytest.raises(pipeline.ResumeError, match="not the latest"):
        pipeline.resume_ingest(clean_db, old.run_id, make_fetcher=no_http)


# --- data quality checks ----------------------------------------------------------------------


def test_empty_extract_fails_the_source_and_closes_nothing(clean_db: Engine) -> None:
    ingest(clean_db, fakes(), sources=[Source.GETONBOARD])
    empty = fakes()
    empty[Source.GETONBOARD].payloads = []

    result = ingest(clean_db, empty, sources=[Source.GETONBOARD])

    row = source_rows(clean_db, result.run_id)["getonboard"]
    assert (result.status, row.error_type) == (RunStatus.FAILED, "EmptyExtractError")
    assert count(clean_db, Posting, Posting.is_active) == 3  # nothing was closed


def test_high_reject_rate_fails_and_leaves_postings_untouched(clean_db: Engine) -> None:
    broken = fakes()
    for job in broken[Source.REMOTIVE].payloads[:2]:
        del job["title"]  # 2 of 3 rejected = 67% > 20%

    result = ingest(clean_db, broken, sources=[Source.REMOTIVE])

    row = source_rows(clean_db, result.run_id)["remotive"]
    assert (row.status, row.stage, row.error_type) == ("failed", "extracted", "RejectRateExceeded")
    assert count(clean_db, Posting) == 0  # even the valid one waits for the fix
    assert count(clean_db, RejectedPosting) == 2  # the evidence is kept


# --- concurrency ------------------------------------------------------------------------------


def test_a_second_concurrent_run_is_refused(clean_db: Engine) -> None:
    with clean_db.connect() as other_session:
        other_session.execute(text("SELECT pg_advisory_lock(:key)"), {"key": pipeline.LOCK_ID})
        with pytest.raises(pipeline.RunLockedError):
            ingest(clean_db, fakes())
        other_session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": pipeline.LOCK_ID})

    assert ingest(clean_db, fakes()).status == RunStatus.SUCCEEDED  # free again


# --- rebuild ----------------------------------------------------------------------------------


def test_rebuild_reparses_raw_without_fetching(clean_db: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    extractors = fakes()
    ingest(clean_db, extractors)
    original: Callable[[Payload], PostingIn] = PARSERS[Source.REMOTIVE]

    def fixed_parser(payload: Payload) -> PostingIn:  # "a parser fix" that changes every title
        return original(payload).model_copy(update={"title": "Fixed title"})

    monkeypatch.setitem(PARSERS, Source.REMOTIVE, fixed_parser)
    result = pipeline.rebuild(clean_db)

    rows = source_rows(clean_db, result.run_id)
    assert (rows["remotive"].updated, rows["getonboard"].unchanged) == (3, 3)
    assert all(e.calls == 1 for e in extractors.values())
    with clean_db.connect() as conn:
        titles = conn.execute(select(Posting.title).where(Posting.source == Source.REMOTIVE)).scalars().all()
    assert set(titles) == {"Fixed title"}


# --- CLI --------------------------------------------------------------------------------------


def test_runs_command_shows_the_ledger(clean_db: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    result = ingest(clean_db, fakes())
    with clean_db.begin() as conn:  # make one source look failed, to check the error line
        conn.execute(
            update(PipelineRunSource)
            .where(PipelineRunSource.source == Source.HN)
            .values(status="failed", error_type="HTTPStatusError", error_message="503")
        )
    monkeypatch.setattr(cli, "get_engine", lambda: clean_db)

    output = CliRunner().invoke(cli.app, ["runs", "--limit", "1"]).output

    assert str(result.run_id) in output
    assert "getonboard succeeded transformed fetched    3  inserted    3" in output
    assert "HTTPStatusError: 503" in output


# --- linking ----------------------------------------------------------------------------------


def test_run_links_the_same_job_across_sources(clean_db: Engine) -> None:
    extractors = fakes()
    # Make one Remotive job the same as GetOnBoard's "Founding AI Engineer" at Sapling.
    job = extractors[Source.REMOTIVE].payloads[0]
    job["company_name"], job["title"] = "Sapling Inc.", "Founding AI Engineer"

    ingest(clean_db, extractors)

    with clean_db.connect() as conn:
        linked = conn.execute(select(Posting.source).where(Posting.canonical_id.is_not(None))).scalars().all()
        groups = conn.execute(text("SELECT count(*) FROM current_postings")).scalar_one()
    assert linked == ["remotive"]  # GetOnBoard's copy was seen first (it ran first): canonical
    assert groups == 8  # 9 postings, one job posted twice

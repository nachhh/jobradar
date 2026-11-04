"""Integration tests for stage 2 (transform), against the jobradar_test database.

Each test plays one or more "runs": it creates a run, lands payloads in raw_postings the way the
extract does (new version → new row; known version → only last_run_id moves), then transforms.
"""

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from jobradar.ingest.sources.base import make_record
from jobradar.ingest.transform import TransformCounts, transform_source
from jobradar.models import PipelineRun, Posting, RawPosting, RejectedPosting, Source

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def load(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text())


def remotive_jobs() -> list[dict[str, Any]]:
    return load("remotive/remote-jobs.json")["jobs"]


def hn_comments(story_id: int) -> list[dict[str, Any]]:
    """The fixture thread's live comments, without replies (as the extractor stores them)."""
    children = load(f"hn/item-{story_id}.json")["children"]
    return [{k: v for k, v in c.items() if k != "children"} for c in children if c["text"]]


def new_run(conn: Connection) -> uuid.UUID:
    stmt = insert(PipelineRun).values(pipeline="ingest", trigger="manual").returning(PipelineRun.run_id)
    return conn.execute(stmt).scalar_one()


def land_raw(conn: Connection, run_id: uuid.UUID, source: Source, payloads: list[dict[str, Any]]) -> None:
    """What stage 1 does, minus HTTP and COPY (step 4): upsert each version, stamping the run."""
    rows = [
        {
            "source": source,
            "src_posting_id": r.src_posting_id,
            "payload": r.payload,
            "payload_hash": r.payload_hash,
            "first_run_id": run_id,
            "last_run_id": run_id,
        }
        for r in (make_record(source, p["id"], p) for p in payloads)
    ]
    stmt = pg_insert(RawPosting)
    conn.execute(
        stmt.on_conflict_do_update(
            constraint="uq_raw_postings_source_src_posting_id_payload_hash",
            set_={"last_run_id": stmt.excluded.last_run_id, "last_fetched_at": stmt.excluded.last_fetched_at},
        ),
        rows,
    )


def run_once(
    conn: Connection, source: Source, payloads: list[dict[str, Any]], now: datetime = NOW
) -> TransformCounts:
    run_id = new_run(conn)
    land_raw(conn, run_id, source, payloads)
    return transform_source(conn, run_id, source, now=now)


def active_ids(conn: Connection, source: Source) -> set[str]:
    rows = conn.execute(select(Posting.src_posting_id).where(Posting.source == source, Posting.is_active))
    return set(rows.scalars())


# --- idempotency ------------------------------------------------------------------------------


def test_first_run_inserts_everything(db_conn: Connection) -> None:
    counts = run_once(db_conn, Source.REMOTIVE, remotive_jobs())
    assert (counts.inserted, counts.updated, counts.unchanged, counts.rejected) == (3, 0, 0, 0)

    acme = db_conn.execute(select(Posting).where(Posting.src_posting_id == "2084113")).one()
    assert acme.dedup_key == "acme analytics|senior data engineer"
    assert acme.last_seen_at == NOW


def test_second_identical_run_changes_nothing(db_conn: Connection) -> None:
    run_once(db_conn, Source.REMOTIVE, remotive_jobs())
    later = NOW + timedelta(hours=12)
    counts = run_once(db_conn, Source.REMOTIVE, remotive_jobs(), now=later)

    assert (counts.inserted, counts.updated, counts.unchanged) == (0, 0, 3)
    # One raw row per posting still: the re-fetch only moved last_run_id.
    raw_rows = db_conn.execute(select(RawPosting.id).where(RawPosting.source == Source.REMOTIVE)).all()
    assert len(raw_rows) == 3
    # Unchanged postings still record that they were seen.
    seen = db_conn.execute(select(Posting.last_seen_at).where(Posting.source == Source.REMOTIVE)).scalars()
    assert set(seen) == {later}


def test_changed_payload_updates_posting_and_adds_raw_version(db_conn: Connection) -> None:
    run_once(db_conn, Source.REMOTIVE, remotive_jobs())
    edited = remotive_jobs()
    edited[0]["title"] = "Staff Data Engineer"
    counts = run_once(db_conn, Source.REMOTIVE, edited)

    assert (counts.inserted, counts.updated, counts.unchanged) == (0, 1, 2)
    title = db_conn.execute(select(Posting.title).where(Posting.src_posting_id == "2084113")).scalar_one()
    assert title == "Staff Data Engineer"
    versions = db_conn.execute(select(RawPosting.id).where(RawPosting.src_posting_id == "2084113")).all()
    assert len(versions) == 2


# --- quarantine -------------------------------------------------------------------------------


def test_invalid_record_is_rejected_and_the_rest_load(db_conn: Connection) -> None:
    jobs = remotive_jobs()
    del jobs[1]["title"]  # KeyError in the parser
    jobs[2]["url"] = "not-a-url"  # ValidationError in PostingIn

    counts = run_once(db_conn, Source.REMOTIVE, jobs)

    assert (counts.inserted, counts.rejected) == (1, 2)
    reasons = db_conn.execute(
        select(RejectedPosting.reason).order_by(RejectedPosting.src_posting_id)
    ).scalars()
    assert [r.split(":")[0] for r in reasons] == ["KeyError", "ValidationError"]


# --- closed postings --------------------------------------------------------------------------


def test_posting_missing_from_extract_is_deactivated_then_reactivated(db_conn: Connection) -> None:
    jobs = remotive_jobs()
    run_once(db_conn, Source.REMOTIVE, jobs)

    counts = run_once(db_conn, Source.REMOTIVE, jobs[:2])  # the third one closed
    assert counts.deactivated == 1
    assert active_ids(db_conn, Source.REMOTIVE) == {"2084113", "2084250"}

    run_once(db_conn, Source.REMOTIVE, jobs)  # ...and came back
    assert active_ids(db_conn, Source.REMOTIVE) == {"2084113", "2084250", "2084301"}


def test_hn_old_comments_expire(db_conn: Connection) -> None:
    comments = hn_comments(50338728)  # posted 2026-11-02
    run_once(db_conn, Source.HN, comments)

    counts = run_once(db_conn, Source.HN, comments, now=NOW + timedelta(days=46))
    assert counts.deactivated == len(comments)
    assert active_ids(db_conn, Source.HN) == set()


def test_hn_deleted_comment_in_fetched_thread_closes(db_conn: Connection) -> None:
    november, october = hn_comments(50338728), hn_comments(49922569)
    run_once(db_conn, Source.HN, november + october)

    # Next run: November's first comment was deleted; October's thread fell out of the window.
    still_listed = november[1:]
    counts = run_once(db_conn, Source.HN, still_listed)

    assert counts.deactivated == 1
    expected = {str(c["id"]) for c in still_listed + october}  # October: out of range, not missing
    assert active_ids(db_conn, Source.HN) == expected


def test_rejected_new_version_keeps_posting_active(db_conn: Connection) -> None:
    jobs = remotive_jobs()
    run_once(db_conn, Source.REMOTIVE, jobs)

    broken = copy.deepcopy(jobs)
    broken[0]["url"] = "not-a-url"
    counts = run_once(db_conn, Source.REMOTIVE, broken)

    assert (counts.rejected, counts.deactivated) == (1, 0)
    assert "2084113" in active_ids(db_conn, Source.REMOTIVE)  # still listed; old clean row kept

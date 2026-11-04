"""Stage 2 of the pipeline: raw rows seen in a run → validated, upserted rows in `postings`.

transform_source() works on one source inside the caller's transaction: it never commits. The
pipeline (step 4) wraps it in a transaction per source and records the counts in the run ledger,
so a crash halfway leaves `postings` exactly as it was before that source started.

The steps, all set-based (a handful of SQL statements per source, not one per posting):
1. Read the source's raw rows seen in this run, and parse each into PostingIn. Failures go to
   rejected_postings with the reason instead of stopping the run.
2. Upsert: insert new postings; rewrite existing ones only when their content_hash changed.
3. Stamp every posting the API returned (last_seen_at, last_run_id, is_active = true).
4. Deactivate the postings that closed, by the source's rule (spec, "Closed postings").
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import Connection, bindparam, insert, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from jobradar.ingest.sources import PARSERS, hn
from jobradar.models import Posting, RawPosting, RejectedPosting, Source

# Columns rewritten when a posting's content changes. Not in the list, on purpose: the natural key,
# first_seen_at (history), canonical_id (owned by the linking step) and the "seen" bookkeeping
# (step 3 below sets it for every posting the API returned, changed or not).
CONTENT_COLUMNS = (
    "url",
    "title",
    "company",
    "location_text",
    "remote",
    "seniority",
    "salary_min",
    "salary_max",
    "salary_text",
    "posted_at",
    "description",
    "tags",
    "content_hash",
    "dedup_key",
)


@dataclass(frozen=True)
class TransformCounts:
    inserted: int
    updated: int
    unchanged: int
    rejected: int
    deactivated: int  # logged by the pipeline; not a ledger column


def transform_source(
    conn: Connection, run_id: uuid.UUID, source: Source, *, now: datetime | None = None
) -> TransformCounts:
    """Transform one source's raw rows seen in `run_id`. `now` is a parameter so tests can fix it."""
    now = now or datetime.now(UTC)
    raw_rows = conn.execute(
        select(RawPosting.src_posting_id, RawPosting.payload).where(
            RawPosting.last_run_id == run_id, RawPosting.source == source
        )
    ).all()

    # 1. Parse. Any exception from a parser is that record's problem, not the run's: pydantic's
    # ValidationError for broken rules, KeyError/TypeError for a payload missing what we rely on.
    parse = PARSERS[source]
    valid: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    for row in raw_rows:
        try:
            posting = parse(row.payload)
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            rejected.append(
                {
                    "run_id": run_id,
                    "source": source,
                    "src_posting_id": row.src_posting_id,
                    "payload": row.payload,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
        else:
            valid.append(posting.to_row() | {"last_run_id": run_id})
    if rejected:
        conn.execute(insert(RejectedPosting), rejected)  # a list of dicts = one executemany

    # 2. Upsert.
    inserted = updated = 0
    if valid:
        stmt = pg_insert(Posting)
        # `excluded` is Postgres' name for the row that was proposed but hit the conflict.
        upsert = stmt.on_conflict_do_update(
            constraint="uq_postings_source_src_posting_id",
            set_={column: stmt.excluded[column] for column in CONTENT_COLUMNS},
            # Without this WHERE, every unchanged posting would be rewritten on every run: wasted
            # writes, and no way to tell "updated" from "unchanged".
            where=Posting.content_hash.is_distinct_from(stmt.excluded.content_hash),
        ).returning(
            # RETURNING yields one row per inserted or updated posting (skipped ones return nothing).
            # xmax is a hidden system column: 0 on a freshly inserted row version, the updating
            # transaction's id on an updated one. A well-known Postgres idiom to tell the two apart.
            literal_column("xmax = 0").label("was_inserted")
        )
        results = conn.execute(upsert, valid).all()
        inserted = sum(1 for r in results if r.was_inserted)
        updated = len(results) - inserted

    # 3. Everything the API returned this run is still listed, even if its new version was
    # rejected: the old clean row stays, and it is not closed.
    seen_ids = [row.src_posting_id for row in raw_rows]
    conn.execute(
        update(Posting)
        .where(Posting.source == source, Posting.src_posting_id.in_(bindparam("ids", expanding=True)))
        .values(last_seen_at=now, last_run_id=run_id, is_active=True),
        {"ids": seen_ids},
    )

    # 4. Close what closed.
    deactivated = _deactivate(conn, run_id, source, seen_ids, now)

    return TransformCounts(
        inserted=inserted,
        updated=updated,
        unchanged=len(valid) - inserted - updated,
        rejected=len(rejected),
        deactivated=deactivated,
    )


def _deactivate(
    conn: Connection, run_id: uuid.UUID, source: Source, seen_ids: list[str], now: datetime
) -> int:
    """Set is_active = false by the source's closed-posting rule; return how many rows changed.

    Only valid after a *successful* extract: an empty or failed fetch would close everything, which
    is why the pipeline fails a source that returns 0 records before getting here.
    """
    params = {"source": source.value, "seen": seen_ids}
    if source != Source.HN:
        # GetOnBoard and Remotive drop a posting from the API when it closes: not seen = closed.
        # `<> ALL(:seen)` is "not equal to any element of the array", i.e. NOT IN for an array.
        result = conn.execute(
            text("""
                UPDATE postings SET is_active = false
                WHERE source = :source AND is_active AND src_posting_id <> ALL(:seen)
            """),
            params,
        )
        return result.rowcount

    # HN (design decision D2): (a) the comment is older than MAX_AGE_DAYS, or (b) it belongs to a
    # thread fetched in this run but wasn't in it, i.e. it was deleted. A thread's comments are in
    # raw_postings with its story_id (->> reads a JSONB field as text).
    cutoff = now - timedelta(days=hn.MAX_AGE_DAYS)
    result = conn.execute(
        text("""
            WITH fetched_threads AS (
                SELECT DISTINCT payload->>'story_id' AS story_id
                FROM raw_postings
                WHERE source = 'hn' AND last_run_id = :run_id
            )
            UPDATE postings p SET is_active = false
            WHERE p.source = :source AND p.is_active
              AND (
                p.posted_at < :cutoff
                OR (
                  p.src_posting_id <> ALL(:seen)
                  AND EXISTS (
                    SELECT 1 FROM raw_postings r
                    WHERE r.source = 'hn' AND r.src_posting_id = p.src_posting_id
                      AND r.payload->>'story_id' IN (SELECT story_id FROM fetched_threads)
                  )
                )
              )
        """),
        params | {"run_id": run_id, "cutoff": cutoff},
    )
    return result.rowcount

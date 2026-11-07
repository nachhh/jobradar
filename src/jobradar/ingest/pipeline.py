"""The ingest pipeline: runs, the ledger, the lock, and the two stages per source.

    run_ingest()     a new run: extract-load, then transform, for each source; then link duplicates
    resume_ingest()  finish a run that failed or crashed, without re-fetching what it already has
    rebuild()        re-parse all of raw_postings into postings (after a parser fix)

Concepts:
- **One transaction per source and stage.** Each stage of each source commits on its own, so a
  failure in one never undoes another, and a crash mid-stage leaves nothing half-written: Postgres
  discards an uncommitted transaction when its connection dies.
- **The ledger is written outside those transactions.** The "running" row for a source commits
  before its work starts, so even a crash leaves a trace that says where the run stopped.
- **Plain functions.** The CLI calls them; an orchestrator (Dagster, later) can call the same ones.
"""

import json
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, Row, Select, Update, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from jobradar.ingest.http import Fetcher, make_client
from jobradar.ingest.link import relink
from jobradar.ingest.sources import EXTRACTORS
from jobradar.ingest.sources.base import Extractor, RawRecord
from jobradar.ingest.transform import TransformCounts, transform_source
from jobradar.ledger import advisory_lock, finish_run, lock_id, start_run
from jobradar.models import (
    Pipeline,
    PipelineRun,
    PipelineRunSource,
    RunStatus,
    Source,
    SourceStatus,
    Stage,
    Trigger,
)

logger = logging.getLogger(__name__)

# Data quality check (spec, "Checks"): above this share of rejected records, the source fails.
MAX_REJECT_RATE = 0.20
# Minimum time between two fetches of a source, from its terms or how often its data changes.
# A run within that window of the source's last completed extract skips it (unless --force).
MIN_FETCH_INTERVAL = {
    Source.REMOTIVE: timedelta(hours=6),  # at most 4 requests a day
    Source.HIMALAYAS: timedelta(hours=20),  # data refreshes daily; ~230 requests per fetch
    Source.JOBICY: timedelta(hours=1),  # "must not exceed once per hour"
}
LOCK_ID = lock_id("jobradar.ingest")  # the advisory lock held by ingest runs (see ledger.py)


class NothingToRunError(Exception):
    """Every requested source was skipped (each inside its minimum fetch interval)."""


class ResumeError(Exception):
    """The run can't be resumed (unknown, already succeeded, or not the latest run)."""


class EmptyExtractError(Exception):
    """A source returned no records: almost certainly an API change, not an empty job market.

    Failing here also protects the data: "not seen → inactive" would otherwise close everything.
    """


@dataclass(frozen=True)
class RunResult:
    run_id: uuid.UUID
    status: RunStatus
    skipped: tuple[Source, ...] = ()


def default_fetcher() -> Fetcher:
    return Fetcher(make_client())


# --- Entry points -----------------------------------------------------------------------------


def run_ingest(
    engine: Engine,
    sources: Sequence[Source] = tuple(Source),
    *,
    trigger: Trigger = Trigger.MANUAL,
    force: bool = False,
    extractors: Mapping[Source, Extractor] = EXTRACTORS,
    make_fetcher: Callable[[], Fetcher] = default_fetcher,
) -> RunResult:
    """Start a new run over `sources`. `extractors` and `make_fetcher` can be swapped in tests."""
    with advisory_lock(engine, LOCK_ID, "ingest"):
        to_run, skipped = _apply_fetch_intervals(engine, sources, force=force)
        if not to_run:
            raise NothingToRunError(f"all sources skipped: {', '.join(skipped)}")
        params = {"sources": list(to_run), "skipped": list(skipped), "force": force}
        run_id = start_run(engine, Pipeline.INGEST, trigger, params)
        logger.info("run %s started: %s", run_id, ", ".join(to_run))
        for source in to_run:
            _extract_and_transform(engine, run_id, source, extractors[source], make_fetcher)
        relink(engine)
        return RunResult(run_id, _finish_run(engine, run_id), tuple(skipped))


def resume_ingest(
    engine: Engine,
    run_id: uuid.UUID,
    *,
    force: bool = False,
    extractors: Mapping[Source, Extractor] = EXTRACTORS,
    make_fetcher: Callable[[], Fetcher] = default_fetcher,
) -> RunResult:
    """Finish `run_id`: skip sources that succeeded, re-transform those that reached `extracted`
    (no API calls), and extract again only those that never got that far."""
    with advisory_lock(engine, LOCK_ID, "ingest"):
        run = _resumable_run(engine, run_id)
        states = {
            row.source: row
            for row in _query(engine, select(PipelineRunSource).where(PipelineRunSource.run_id == run_id))
        }
        pending = [Source(s) for s in run.params["sources"]]
        needs_fetch = [s for s in pending if s not in states or states[s].stage is None]
        allowed, skipped = _apply_fetch_intervals(engine, needs_fetch, force=force)
        for source in pending:
            state = states.get(source)
            if state is not None and state.status == SourceStatus.SUCCEEDED:
                continue  # already done
            if state is not None and state.stage is not None:
                logger.info("%s: already extracted; transforming again without fetching", source)
                _start_source(engine, run_id, source)
                _transform(engine, run_id, source)
            elif source in allowed:
                _extract_and_transform(engine, run_id, source, extractors[source], make_fetcher)
        relink(engine)
        return RunResult(run_id, _finish_run(engine, run_id), tuple(skipped))


def rebuild(engine: Engine, sources: Sequence[Source] = tuple(Source)) -> RunResult:
    """Re-parse the latest raw version of every posting. No API calls; is_active is untouched.

    Only postings whose parsed content changes are rewritten (the content_hash rule), so after a
    parser fix exactly the affected postings show up as `updated` in the ledger.
    """
    with advisory_lock(engine, LOCK_ID, "ingest"):
        run_id = start_run(engine, Pipeline.REBUILD, Trigger.MANUAL, {"sources": list(sources)})
        for source in sources:
            _start_source(engine, run_id, source)
            try:
                with engine.begin() as conn:
                    counts = transform_source(conn, run_id, source, rebuild=True)
                    _record_counts(conn, run_id, source, counts, succeeded=True)
            except Exception as exc:
                _fail_source(engine, run_id, source, exc)
        relink(engine)  # a parser fix can change dedup keys
        return RunResult(run_id, _finish_run(engine, run_id))


# --- One source -------------------------------------------------------------------------------


def _extract_and_transform(
    engine: Engine, run_id: uuid.UUID, source: Source, extract: Extractor, make_fetcher: Callable[[], Fetcher]
) -> None:
    _start_source(engine, run_id, source)

    # Stage 1: extract-load. Any exception (HTTP, response shape, database) fails this source only,
    # and the run moves on. `except Exception` (not BaseException) still lets Ctrl-C through.
    try:
        fetcher = make_fetcher()
        try:
            records = extract(fetcher)
        finally:
            fetcher.client.close()  # release pooled connections even when the extract fails
        if not records:
            raise EmptyExtractError(f"{source} returned 0 records")
        with engine.begin() as conn:  # commits on success, rolls back on any exception
            new_versions = load_raw(conn, run_id, source, records)
            _mark_extracted(conn, run_id, source, fetched=len(records))
        logger.info("%s: fetched %d, %d new raw versions", source, len(records), new_versions)
    except Exception as exc:
        _fail_source(engine, run_id, source, exc)
        return

    # Stage 2 in its own transaction: if it fails, the raw rows above stay, ready for --resume.
    _transform(engine, run_id, source)


def _transform(engine: Engine, run_id: uuid.UUID, source: Source) -> None:
    try:
        with engine.begin() as conn:
            counts = transform_source(conn, run_id, source, max_reject_rate=MAX_REJECT_RATE)
            _record_counts(conn, run_id, source, counts, succeeded=counts.applied)
            if not counts.applied:
                # The rejections commit (evidence of what broke); postings were not touched.
                message = (
                    f"{counts.rejected}/{counts.read} records rejected "
                    f"({counts.reject_rate:.0%} > {MAX_REJECT_RATE:.0%})"
                )
                _set_failed(conn, run_id, source, "RejectRateExceeded", message)
                logger.error("%s: %s; transform not applied", source, message)
                return
        logger.info(
            "%s: inserted %d, updated %d, unchanged %d, rejected %d, deactivated %d",
            source,
            counts.inserted,
            counts.updated,
            counts.unchanged,
            counts.rejected,
            counts.deactivated,
        )
    except Exception as exc:
        _fail_source(engine, run_id, source, exc)


def load_raw(conn: Connection, run_id: uuid.UUID, source: Source, records: Sequence[RawRecord]) -> int:
    """Bulk-load records into raw_postings; return how many were new versions.

    COPY streams rows into a table in one round trip, much faster than INSERTs for thousands of
    rows. But COPY has no "ON CONFLICT", so rows land first in a temporary staging table, and one
    INSERT … SELECT moves them across with the conflict rule: a new version is inserted; a version
    already stored only gets its last_run_id/last_fetched_at refreshed.
    """
    # TEMP: visible to this connection only. ON COMMIT DROP: gone when the transaction ends.
    conn.execute(
        text("""
            CREATE TEMP TABLE stg_raw_postings (
                src_posting_id text NOT NULL,
                payload jsonb NOT NULL,
                payload_hash text NOT NULL
            ) ON COMMIT DROP
        """)
    )
    # COPY is a psycopg feature that SQLAlchemy doesn't wrap, so reach the driver's own connection.
    # It's the same connection, so the COPY runs inside this transaction.
    driver_conn = conn.connection.driver_connection
    if driver_conn is None:  # only when the connection is closed; can't happen inside engine.begin()
        raise RuntimeError("no driver connection")
    with (
        driver_conn.cursor() as cursor,
        cursor.copy("COPY stg_raw_postings (src_posting_id, payload, payload_hash) FROM STDIN") as copy,
    ):
        for record in records:
            copy.write_row((record.src_posting_id, json.dumps(record.payload), record.payload_hash))

    result = conn.execute(
        text("""
            INSERT INTO raw_postings
                (source, src_posting_id, payload, payload_hash, first_run_id, last_run_id)
            SELECT :source, src_posting_id, payload, payload_hash, :run_id, :run_id FROM stg_raw_postings
            ON CONFLICT (source, src_posting_id, payload_hash)
            DO UPDATE SET last_run_id = EXCLUDED.last_run_id, last_fetched_at = now()
            RETURNING (xmax = 0) AS was_inserted
        """),
        {"source": source.value, "run_id": run_id},
    )
    return sum(1 for row in result if row.was_inserted)


# --- Fetch-frequency limits ------------------------------------------------------------------


def _apply_fetch_intervals(
    engine: Engine, sources: Sequence[Source], *, force: bool
) -> tuple[list[Source], list[Source]]:
    """Split `sources` into (to run, skipped) by MIN_FETCH_INTERVAL.

    A source is skipped when its last *completed extract* finished less than its interval ago.
    That's "stage reached", not "source succeeded": a run whose transform failed still spent the
    requests.
    """
    limited = [s for s in sources if s in MIN_FETCH_INTERVAL]
    if force or not limited:
        return list(sources), []
    query = (
        select(PipelineRunSource.source, func.max(PipelineRunSource.finished_at))
        .where(PipelineRunSource.source.in_(limited), PipelineRunSource.stage.is_not(None))
        .group_by(PipelineRunSource.source)
    )
    with engine.connect() as conn:
        last_extracts: dict[str, datetime] = {
            source: finished for source, finished in conn.execute(query) if finished is not None
        }
    now = datetime.now(UTC)
    skipped = [s for s in limited if last_extracts.get(s) and now - last_extracts[s] < MIN_FETCH_INTERVAL[s]]
    for source in skipped:
        logger.warning(
            "%s: last extract at %s, less than %s ago; skipped (use --force)",
            source,
            last_extracts[source],
            MIN_FETCH_INTERVAL[source],
        )
    return [s for s in sources if s not in skipped], skipped


# --- The lock ---------------------------------------------------------------------------------


# --- Ledger writes ----------------------------------------------------------------------------


def _start_source(engine: Engine, run_id: uuid.UUID, source: Source) -> None:
    """Mark (run, source) running: insert it, or reset it when resuming. Commits immediately."""
    stmt = pg_insert(PipelineRunSource).values(run_id=run_id, source=source)
    reset = {
        "status": SourceStatus.RUNNING,
        "started_at": func.now(),
        "finished_at": None,
        "error_type": None,
        "error_message": None,
    }
    with engine.begin() as conn:
        conn.execute(stmt.on_conflict_do_update(index_elements=["run_id", "source"], set_=reset))


def _mark_extracted(conn: Connection, run_id: uuid.UUID, source: Source, *, fetched: int) -> None:
    conn.execute(_source_row(run_id, source).values(stage=Stage.EXTRACTED, fetched=fetched))


def _record_counts(
    conn: Connection, run_id: uuid.UUID, source: Source, counts: TransformCounts, *, succeeded: bool
) -> None:
    values: dict[str, Any] = {
        "inserted": counts.inserted,
        "updated": counts.updated,
        "unchanged": counts.unchanged,
        "rejected": counts.rejected,
    }
    if succeeded:
        values |= {"stage": Stage.TRANSFORMED, "status": SourceStatus.SUCCEEDED, "finished_at": func.now()}
    conn.execute(_source_row(run_id, source).values(**values))


def _set_failed(conn: Connection, run_id: uuid.UUID, source: Source, error_type: str, message: str) -> None:
    conn.execute(
        _source_row(run_id, source).values(
            status=SourceStatus.FAILED, finished_at=func.now(), error_type=error_type, error_message=message
        )
    )


def _fail_source(engine: Engine, run_id: uuid.UUID, source: Source, exc: Exception) -> None:
    """Record a failure in a fresh transaction (the one that failed was rolled back)."""
    logger.error("%s failed: %s: %s", source, type(exc).__name__, exc)
    with engine.begin() as conn:
        _set_failed(conn, run_id, source, type(exc).__name__, str(exc)[:2000])


def _finish_run(engine: Engine, run_id: uuid.UUID) -> RunStatus:
    """succeeded if every source succeeded, partial if some did, failed if none did."""
    with engine.begin() as conn:
        statuses = (
            conn.execute(select(PipelineRunSource.status).where(PipelineRunSource.run_id == run_id))
            .scalars()
            .all()
        )
        succeeded = sum(1 for s in statuses if s == SourceStatus.SUCCEEDED)
        if statuses and succeeded == len(statuses):
            status = RunStatus.SUCCEEDED
        elif succeeded:
            status = RunStatus.PARTIAL
        else:
            status = RunStatus.FAILED
    finish_run(engine, run_id, status)
    logger.info("run %s: %s", run_id, status)
    return status


def _source_row(run_id: uuid.UUID, source: Source) -> Update:
    return update(PipelineRunSource).where(
        PipelineRunSource.run_id == run_id, PipelineRunSource.source == source
    )


# --- Small helpers ----------------------------------------------------------------------------


def _resumable_run(engine: Engine, run_id: uuid.UUID) -> Row[Any]:
    # select(PipelineRun) on a Connection returns plain rows of columns (run.status, run.params…),
    # not ORM objects: those come from a Session. Rows are all this code needs.
    rows = _query(engine, select(PipelineRun).where(PipelineRun.run_id == run_id))
    if not rows:
        raise ResumeError(f"no run {run_id}")
    run = rows[0]
    if run.pipeline != Pipeline.INGEST:
        raise ResumeError(f"run {run_id} is a {run.pipeline} run, not an ingest run")
    if run.status == RunStatus.SUCCEEDED:
        raise ResumeError(f"run {run_id} already succeeded")
    latest = _query(
        engine,
        select(PipelineRun.run_id)
        .where(PipelineRun.pipeline == Pipeline.INGEST)
        .order_by(PipelineRun.started_at.desc())
        .limit(1),
    )[0][0]
    if latest != run_id:
        # A newer run has re-stamped last_run_id on the raw rows this run would transform, so the
        # old run would see an incomplete batch, and "not seen → inactive" would close live postings.
        raise ResumeError(f"run {run_id} is not the latest ingest run; start a new run instead")
    return run


def _query(engine: Engine, stmt: Select[Any]) -> Sequence[Row[Any]]:
    """Run a read-only query on a short-lived connection."""
    with engine.connect() as conn:
        return conn.execute(stmt).all()

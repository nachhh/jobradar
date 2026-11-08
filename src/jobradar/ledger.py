"""Run-ledger helpers shared by every pipeline (ingest, enrich, index).

Each pipeline records its runs in pipeline_runs and holds an advisory lock while it runs. The lock
is per pipeline: two enrich runs can't overlap, but an enrich run may run alongside an ingest run.

How a run's row always gets a final status:
- **Normal end:** the pipeline computes it (succeeded, partial, failed) and calls finish_run.
- **An exception:** run_ledger records `failed`.
- **Ctrl-C or a kill signal:** run_ledger records `interrupted`. Ctrl-C raises KeyboardInterrupt;
  install_signal_handlers makes SIGTERM (`kill`, what process managers send) and SIGHUP (the
  terminal closed) raise it too, since by default they end Python without running any cleanup.
- **kill -9, power loss:** nothing can run in the dying process, so the row stays `running`. The
  next run of the pipeline finds it (sweep_dead_runs) and marks it `interrupted`.
"""

import hashlib
import logging
import signal
import subprocess
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from types import FrameType
from typing import Any

from sqlalchemy import Engine, func, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from jobradar.models import Pipeline, PipelineRun, PipelineRunSource, RunStatus, SourceStatus, Trigger

logger = logging.getLogger(__name__)


class RunLockedError(Exception):
    """Another run of the same pipeline holds the lock."""


def lock_id(name: str) -> int:
    """A 64-bit advisory-lock id derived from a readable name.

    Postgres identifies advisory locks by a 64-bit integer, and any fixed value that nothing else in
    the database uses will do. Deriving it from a name documents what it locks (the first 8 bytes
    of a SHA-256, read as a signed 64-bit integer).
    """
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


@contextmanager
def advisory_lock(engine: Engine, key: int, what: str) -> Iterator[None]:
    """Hold a Postgres advisory lock for the whole run, or fail at once if someone else has it.

    An advisory lock is a named mutex inside Postgres with no table attached: the application
    decides what it means. pg_try_advisory_lock returns false instead of waiting (fail fast). The
    lock belongs to the *session* (connection), not a transaction: it's released by
    pg_advisory_unlock, or automatically when the connection closes, so a crash can't leave it stuck.
    """
    with engine.connect() as conn:
        acquired = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar_one()
        conn.commit()  # ends SQLAlchemy's implicit transaction; the session-level lock stays held
        if not acquired:
            raise RunLockedError(f"another {what} run is in progress")
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            conn.commit()


def start_run(engine: Engine, pipeline: Pipeline, trigger: Trigger, params: dict[str, Any]) -> uuid.UUID:
    """Insert the run's ledger row (status "running") and commit it before any work starts."""
    stmt = (
        pg_insert(PipelineRun)
        .values(pipeline=pipeline, trigger=trigger, params=params, git_sha=git_sha())
        .returning(PipelineRun.run_id)
    )
    with engine.begin() as conn:
        return conn.execute(stmt).scalar_one()


def finish_run(engine: Engine, run_id: uuid.UUID, status: RunStatus) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(PipelineRun)
            .where(PipelineRun.run_id == run_id)
            .values(status=status, finished_at=func.now())
        )


@contextmanager
def run_ledger(
    engine: Engine,
    pipeline: Pipeline,
    trigger: Trigger,
    params: dict[str, Any],
    *,
    sweep: Sequence[Pipeline] | None = None,
) -> Iterator[uuid.UUID]:
    """Start a run (after sweeping dead ones) and yield its id; record how it ended if it didn't
    finish normally. Call it while holding the pipeline's advisory lock.

    `sweep`: the pipelines whose dead runs to look for, i.e. every pipeline that shares this lock
    (ingest and rebuild do); defaults to `pipeline` alone.
    """
    sweep_dead_runs(engine, sweep or [pipeline])
    run_id = start_run(engine, pipeline, trigger, params)
    with guard_run(engine, run_id):
        yield run_id


@contextmanager
def guard_run(engine: Engine, run_id: uuid.UUID) -> Iterator[None]:
    """Record `interrupted` or `failed` if the block is stopped or raises; the exception goes on.

    Separate from run_ledger for `ingest --resume`, which continues an existing run.
    """
    try:
        yield
    except KeyboardInterrupt:
        # BaseException, not Exception, so the pipelines' `except Exception` handlers (a source
        # failing, a batch failing) don't swallow it: it unwinds straight to here, rolling back the
        # transactions it passes through.
        logger.warning("run %s interrupted", run_id)
        _mark_interrupted(engine, [run_id], finished=True)
        raise
    except Exception:
        finish_run(engine, run_id, RunStatus.FAILED)
        raise


def sweep_dead_runs(engine: Engine, pipelines: Sequence[Pipeline]) -> list[uuid.UUID]:
    """Mark `interrupted` the runs of `pipelines` still `running`: their process died without a word.

    Safe only while holding those pipelines' advisory lock: a live run would be holding it, so any
    `running` row left is from a process that is gone. Returns the swept run ids.
    """
    with engine.connect() as conn:
        dead = list(
            conn.execute(
                text(
                    "SELECT run_id FROM pipeline_runs WHERE status = :running AND pipeline = ANY(:pipelines)"
                ),
                {"running": RunStatus.RUNNING, "pipelines": list(pipelines)},
            ).scalars()
        )
    if dead:
        logger.warning("marking %d dead run(s) interrupted: %s", len(dead), ", ".join(map(str, dead)))
        # finished_at stays NULL: nobody knows when the process died.
        _mark_interrupted(engine, dead, finished=False)
    return dead


def _mark_interrupted(engine: Engine, run_ids: Sequence[uuid.UUID], *, finished: bool) -> None:
    """The runs become `interrupted`; their sources still `running` (ingest) become `failed`."""
    with engine.begin() as conn:
        conn.execute(
            update(PipelineRun)
            .where(PipelineRun.run_id.in_(run_ids))
            .values(status=RunStatus.INTERRUPTED, finished_at=func.now() if finished else None)
        )
        conn.execute(
            update(PipelineRunSource)
            .where(PipelineRunSource.run_id.in_(run_ids), PipelineRunSource.status == SourceStatus.RUNNING)
            .values(
                status=SourceStatus.FAILED,
                finished_at=func.now() if finished else None,
                error_type="Interrupted",
                error_message="the run was stopped before this source finished",
            )
        )


def install_signal_handlers() -> None:
    """Make SIGTERM and SIGHUP raise KeyboardInterrupt, like Ctrl-C (SIGINT) already does.

    Python runs signal handlers in the main thread, between bytecodes, so the exception appears
    wherever the main thread is (often waiting on the database or on worker threads) and unwinds
    from there. Called once by the CLI, the process's owner; library code shouldn't set handlers.
    """

    def interrupt(signum: int, _frame: FrameType | None) -> None:
        logger.warning("received %s: stopping", signal.Signals(signum).name)
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupt)


def git_sha() -> str | None:
    """The commit the code runs from, for lineage; None outside a git checkout."""
    try:
        # S603/S607: fixed arguments (no user input), and `git` found on PATH on purpose.
        result = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--short=12", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None

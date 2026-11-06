"""Run-ledger helpers shared by every pipeline (ingest, enrich, index).

Each pipeline records its runs in pipeline_runs and holds an advisory lock while it runs. The lock
is per pipeline: two enrich runs can't overlap, but an enrich run may run alongside an ingest run.
"""

import hashlib
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, func, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from jobradar.models import Pipeline, PipelineRun, RunStatus, Trigger


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

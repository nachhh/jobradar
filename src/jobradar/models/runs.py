"""The run ledger: what each pipeline run was asked to do and how it ended.

See specs/phase-1-ingestion.md, "Data model".
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from jobradar.db import Base
from jobradar.models._shared import TIMESTAMPTZ, one_of
from jobradar.models.postings import Source

# --- Allowed values ---------------------------------------------------------------------------
# StrEnum members are real strings (RunStatus.FAILED == "failed"), so they can be written to TEXT
# columns and compared with values read back, while typos become attribute errors, not bad data.


class Pipeline(StrEnum):
    """Which pipeline a run belongs to; every pipeline records its runs in the same ledger."""

    INGEST = "ingest"  # Phase 1: extract, load, transform, link
    REBUILD = "rebuild"  # Phase 1: postings rebuilt from raw_postings
    ENRICH = "enrich"  # Phase 2: LLM fields for stale postings
    INDEX = "index"  # Phase 2: chunks and embeddings for stale postings


class Trigger(StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"  # some sources succeeded, some failed
    FAILED = "failed"
    # Stopped from outside: Ctrl-C or a kill signal (recorded by the run itself), or killed so hard
    # it couldn't record anything (kill -9, power loss), found by the next run of its pipeline.
    INTERRUPTED = "interrupted"


class SourceStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Stage(StrEnum):
    """How far a source got in a run; what `--resume` reads to know what's left to do."""

    EXTRACTED = "extracted"  # raw rows committed, not yet transformed
    TRANSFORMED = "transformed"


# --- Tables -----------------------------------------------------------------------------------


class PipelineRun(Base):
    """One row per pipeline run: what was asked for, from which code, and how it ended."""

    __tablename__ = "pipeline_runs"
    __table_args__ = (
        one_of("pipeline", Pipeline),
        one_of("trigger", Trigger),
        one_of("status", RunStatus),
    )

    # gen_random_uuid() is built into Postgres 13+. UUIDs (not 1, 2, 3…) can be generated anywhere
    # without asking the database, and don't reveal how many runs exist.
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    pipeline: Mapped[str] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(Text)
    # JSONB = binary JSON: validated on write, indexable, and queryable with operators like ->>.
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    git_sha: Mapped[str | None] = mapped_column(Text)  # which code produced this run (lineage)
    status: Mapped[str] = mapped_column(Text, server_default=RunStatus.RUNNING.value)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)


class PipelineRunSource(Base):
    """One row per (run, source): its status, the stage reached and the row counts.

    Run totals are SUM()s over this table rather than columns on pipeline_runs, so there's one
    copy of each number and nothing to keep in sync.
    """

    __tablename__ = "pipeline_run_sources"
    __table_args__ = (
        one_of("source", Source),
        one_of("status", SourceStatus),
        one_of("stage", Stage),  # NULL (allowed by the CHECK) until the extract commits
    )

    # ondelete="CASCADE": deleting a run deletes its per-source rows with it (enforced by Postgres).
    # Two primary_key=True columns make a composite primary key: (run_id, source).
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, server_default=SourceStatus.RUNNING.value)
    stage: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    fetched: Mapped[int] = mapped_column(Integer, server_default="0")
    inserted: Mapped[int] = mapped_column(Integer, server_default="0")
    updated: Mapped[int] = mapped_column(Integer, server_default="0")
    unchanged: Mapped[int] = mapped_column(Integer, server_default="0")
    rejected: Mapped[int] = mapped_column(Integer, server_default="0")
    error_type: Mapped[str | None] = mapped_column(Text)  # exception class, e.g. "HTTPStatusError"
    error_message: Mapped[str | None] = mapped_column(Text)

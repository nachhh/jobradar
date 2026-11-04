"""ORM models: the Phase 1 ingestion tables (see specs/phase-1-ingestion.md, "Data model").

Concepts:
- SQLAlchemy 2 "typed" mappings: `Mapped[str]` declares the Python type of an attribute and,
  through it, NOT NULL; `Mapped[str | None]` makes the column nullable. `mapped_column()` adds
  the SQL details (type, default, index) the Python type can't express.
- These classes describe the schema; the migration in migrations/versions/ creates it. Alembic's
  autogenerate (and the test `test_models_match_migrations`) compare the two so they can't drift.
- The pipeline will write mostly with Core SQL (bulk INSERT … ON CONFLICT) rather than one ORM
  object per row, but it reuses these Table definitions, so column names live in one place.

Enumerated values (statuses, sources) are TEXT columns with a CHECK constraint, not Postgres
ENUM types: adding a value to a CHECK is a one-line migration, while ENUM types can't drop
values and are awkward to change inside a transaction.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from jobradar.db import Base

# --- Allowed values ---------------------------------------------------------------------------
# StrEnum members are real strings (Source.HN == "hn"), so they can be written to TEXT columns
# and compared with values read back, while typos become attribute errors instead of bad data.


class Source(StrEnum):
    GETONBOARD = "getonboard"
    REMOTIVE = "remotive"
    HN = "hn"
    HIMALAYAS = "himalayas"
    JOBICY = "jobicy"


class Trigger(StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"  # some sources succeeded, some failed
    FAILED = "failed"


class SourceStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Stage(StrEnum):
    """How far a source got in a run; what `--resume` reads to know what's left to do."""

    EXTRACTED = "extracted"  # raw rows committed, not yet transformed
    TRANSFORMED = "transformed"


class SalaryPeriod(StrEnum):
    """The period a salary amount refers to, as the source states it (no conversion)."""

    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class Remote(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


def _one_of(column: str, values: type[StrEnum]) -> CheckConstraint:
    """CHECK (column IN ('a', 'b', …)), built from the enum so the two never disagree.

    The values come from our own enums, never from user input, so formatting them into SQL is safe.
    A CHECK passes when its expression is NULL, so on a nullable column NULL stays allowed.
    """
    allowed = ", ".join(f"'{v.value}'" for v in values)
    # `name` fills %(constraint_name)s in the naming convention: ck_<table>_<column>.
    return CheckConstraint(f"{column} IN ({allowed})", name=column)


# Columns reused by several tables. server_default=now() makes Postgres fill the value, so rows
# inserted with plain SQL (or by hand in psql) get it too, not only rows inserted through the ORM.
# timezone=True → TIMESTAMPTZ: stored as UTC and unambiguous; plain TIMESTAMP has no time zone.
_TIMESTAMPTZ = DateTime(timezone=True)


# --- Run ledger -------------------------------------------------------------------------------


class PipelineRun(Base):
    """One row per pipeline run: what was asked for, from which code, and how it ended."""

    __tablename__ = "pipeline_runs"
    __table_args__ = (
        _one_of("trigger", Trigger),
        _one_of("status", RunStatus),
    )

    # gen_random_uuid() is built into Postgres 13+. UUIDs (not 1, 2, 3…) can be generated anywhere
    # without asking the database, and don't reveal how many runs exist.
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    pipeline: Mapped[str] = mapped_column(Text)  # "ingest" for now; later pipelines reuse the ledger
    trigger: Mapped[str] = mapped_column(Text)
    # JSONB = binary JSON: validated on write, indexable, and queryable with operators like ->>.
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    git_sha: Mapped[str | None] = mapped_column(Text)  # which code produced this run (lineage)
    status: Mapped[str] = mapped_column(Text, server_default=RunStatus.RUNNING.value)
    started_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ)


class PipelineRunSource(Base):
    """One row per (run, source): its status, the stage reached and the row counts.

    Run totals are SUM()s over this table rather than columns on pipeline_runs, so there's one
    copy of each number and nothing to keep in sync.
    """

    __tablename__ = "pipeline_run_sources"
    __table_args__ = (
        _one_of("source", Source),
        _one_of("status", SourceStatus),
        _one_of("stage", Stage),  # NULL (allowed by the CHECK) until the extract commits
    )

    # ondelete="CASCADE": deleting a run deletes its per-source rows with it (enforced by Postgres).
    # Two primary_key=True columns make a composite primary key: (run_id, source).
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, server_default=SourceStatus.RUNNING.value)
    stage: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ)
    fetched: Mapped[int] = mapped_column(Integer, server_default="0")
    inserted: Mapped[int] = mapped_column(Integer, server_default="0")
    updated: Mapped[int] = mapped_column(Integer, server_default="0")
    unchanged: Mapped[int] = mapped_column(Integer, server_default="0")
    rejected: Mapped[int] = mapped_column(Integer, server_default="0")
    error_type: Mapped[str | None] = mapped_column(Text)  # exception class, e.g. "HTTPStatusError"
    error_message: Mapped[str | None] = mapped_column(Text)


# --- Raw layer --------------------------------------------------------------------------------


class RawPosting(Base):
    """A posting's payload exactly as the API returned it: one row per distinct version.

    The payload columns never change after insert. first_* say which run first saw this version;
    last_* are refreshed whenever a run fetches the same version again, which is how the transform
    knows a posting is still listed even when nothing about it changed.
    """

    __tablename__ = "raw_postings"
    __table_args__ = (
        _one_of("source", Source),
        # The deduplication rule of the raw layer: the same content for the same posting is stored
        # once. INSERT … ON CONFLICT targets this constraint.
        UniqueConstraint("source", "src_posting_id", "payload_hash"),
        # The transform's read: "rows of source S seen in run R".
        Index(None, "last_run_id", "source"),
    )

    # Identity = a SQL-standard auto-increment column (Postgres' modern replacement for SERIAL).
    # BigInteger because append-heavy tables can outgrow 2^31.
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    src_posting_id: Mapped[str] = mapped_column(Text)  # TEXT for all sources: some IDs are slugs
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # SHA-256 of the canonical JSON (sorted keys): equal content → equal hash, so a re-fetch of an
    # unchanged posting hits the unique constraint instead of storing a duplicate.
    payload_hash: Mapped[str] = mapped_column(Text)
    first_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_runs.run_id"))
    first_fetched_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, server_default=func.now())
    last_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_runs.run_id"))
    last_fetched_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, server_default=func.now())


# --- Clean layer ------------------------------------------------------------------------------


class Posting(Base):
    """The clean, current view of a posting: one row per (source, src_posting_id), upserted."""

    __tablename__ = "postings"
    __table_args__ = (
        _one_of("source", Source),
        _one_of("remote", Remote),
        _one_of("salary_period", SalaryPeriod),
        # The natural key: what makes two rows "the same posting" within a source.
        UniqueConstraint("source", "src_posting_id"),
        CheckConstraint("salary_min <= salary_max", name="salary_range"),
        # A posting can't be its own canonical copy (that would be a one-row cycle).
        CheckConstraint("canonical_id <> id", name="canonical_not_self"),
        # "Active postings of a source": the closed-postings step and search both filter on this.
        Index(None, "source", "is_active"),
        Index(None, "dedup_key"),
        Index(None, "canonical_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    src_posting_id: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    # Nullable: HN comments don't always name the company or role; Phase 2's LLM fills the gaps.
    title: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    location_text: Mapped[str | None] = mapped_column(Text)  # as written by the source
    remote: Mapped[str] = mapped_column(Text, server_default=Remote.UNKNOWN.value)
    seniority: Mapped[str | None] = mapped_column(Text)
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    # Amounts are stored as the source gives them: salary_currency (ISO code, e.g. "USD") and
    # salary_period say what they mean. Comparing across units is a query-time concern.
    salary_currency: Mapped[str | None] = mapped_column(Text)
    salary_period: Mapped[str | None] = mapped_column(Text)
    salary_text: Mapped[str | None] = mapped_column(Text)  # the original wording, when there is one
    posted_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ)
    description: Mapped[str] = mapped_column(Text)  # plain text; HTML stripped by the parser
    # TEXT[] = a Postgres array column: one value holds a list of strings.
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    # Hash of the parsed fields: the upsert only rewrites a row when this changes.
    content_hash: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, server_default=func.now())
    last_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_runs.run_id"))
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    dedup_key: Mapped[str | None] = mapped_column(Text)  # normalized company + title
    # Self-reference: points at the canonical copy of the same job on another board. NULL = this row
    # is canonical (or unlinked). SET NULL: deleting the canonical row unlinks instead of failing.
    canonical_id: Mapped[int | None] = mapped_column(ForeignKey("postings.id", ondelete="SET NULL"))


class RejectedPosting(Base):
    """Quarantine: records that failed validation, kept with the reason instead of being dropped.

    Dropping bad records silently hides API changes; keeping them makes the reject rate visible and
    lets a fixed parser re-process them (they are still in raw_postings too).
    """

    __tablename__ = "rejected_postings"
    __table_args__ = (
        _one_of("source", Source),
        Index(None, "run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_runs.run_id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(Text)
    src_posting_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text)  # the validation error, readable by a human
    rejected_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, server_default=func.now())

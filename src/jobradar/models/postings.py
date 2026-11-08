"""The ingestion layers: raw payloads, clean postings and the quarantine.

See specs/phase-1-ingestion.md, "Data model".
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from jobradar.db import Base
from jobradar.models._shared import TIMESTAMPTZ, one_of

# --- Allowed values ---------------------------------------------------------------------------
# StrEnum members are real strings (Source.HN == "hn"), so they can be written to TEXT columns
# and compared with values read back, while typos become attribute errors instead of bad data.


class Source(StrEnum):
    GETONBOARD = "getonboard"
    REMOTIVE = "remotive"
    HN = "hn"
    HIMALAYAS = "himalayas"
    JOBICY = "jobicy"


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


# --- Raw layer --------------------------------------------------------------------------------


class RawPosting(Base):
    """A posting's payload exactly as the API returned it: one row per distinct version.

    The payload columns never change after insert. first_* say which run first saw this version;
    last_* are refreshed whenever a run fetches the same version again, which is how the transform
    knows a posting is still listed even when nothing about it changed.
    """

    __tablename__ = "raw_postings"
    __table_args__ = (
        one_of("source", Source),
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
    first_fetched_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    last_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_runs.run_id"))
    last_fetched_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


# --- Clean layer ------------------------------------------------------------------------------


class Posting(Base):
    """The clean, current view of a posting: one row per (source, src_posting_id), upserted."""

    __tablename__ = "postings"
    __table_args__ = (
        one_of("source", Source),
        one_of("remote", Remote),
        one_of("salary_period", SalaryPeriod),
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
    posted_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    description: Mapped[str] = mapped_column(Text)  # plain text; HTML stripped by the parser
    # TEXT[] = a Postgres array column: one value holds a list of strings.
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    # Hash of the parsed fields: the upsert only rewrites a row when this changes.
    content_hash: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
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
        one_of("source", Source),
        Index(None, "run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_runs.run_id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(Text)
    src_posting_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text)  # the validation error, readable by a human
    rejected_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

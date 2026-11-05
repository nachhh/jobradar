"""LLM enrichment: the fields extracted from each posting, and a log of every LLM call.

See specs/phase-2-enrichment-retrieval.md, "Part A: enrichment" and "Data model".

The extracted fields live in their own table, never in `postings` (decision D4): `postings` keeps
exactly what the sources said, so a fact a source gave can always be told apart from one a model
inferred, and re-enriching never touches the clean layer.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from jobradar.db import Base
from jobradar.models._shared import TIMESTAMPTZ, one_of
from jobradar.models.postings import Remote, SalaryPeriod

# --- Allowed values ---------------------------------------------------------------------------
# The extraction schema (enrich/schema.py) reuses these enums, so the values the LLM may answer
# with and the values the database accepts are one list.


class Seniority(StrEnum):
    INTERN = "intern"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    PRINCIPAL = "principal"
    UNKNOWN = "unknown"


class RoleFamily(StrEnum):
    BACKEND = "backend"
    FRONTEND = "frontend"
    FULLSTACK = "fullstack"
    MOBILE = "mobile"
    DATA_ENGINEERING = "data_engineering"
    DATA_SCIENCE = "data_science"
    ANALYTICS = "analytics"
    ML_AI_ENGINEERING = "ml_ai_engineering"
    AI_TRAINING = "ai_training"  # data labelling and RLHF contract work, not building AI systems
    DEVOPS_PLATFORM_CLOUD = "devops_platform_cloud"
    QA = "qa"
    SECURITY = "security"
    EMBEDDED = "embedded"
    PRODUCT = "product"
    DESIGN = "design"
    MANAGEMENT = "management"
    SUPPORT = "support"
    OTHER = "other"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    FREELANCE = "freelance"
    INTERNSHIP = "internship"
    UNKNOWN = "unknown"


class LatamHiring(StrEnum):
    """Does the job hire from Latin America? The question this project's search filters on most."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class SalaryBasis(StrEnum):
    """Before (gross) or after (net) taxes. Chilean postings often quote "sueldo líquido" (net)."""

    GROSS = "gross"
    NET = "net"
    UNKNOWN = "unknown"


class EnrichmentStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"  # invalid output even after the retry; extracted fields are NULL


class LlmPurpose(StrEnum):
    """Why a call was made. Only enrichment for now; the Phase 3 agent adds its own purposes."""

    ENRICH = "enrich"


class LlmCallStatus(StrEnum):
    OK = "ok"
    INVALID_OUTPUT = "invalid_output"  # the answer didn't validate against the schema
    UNGROUNDED = "ungrounded"  # valid, but the quoted LATAM evidence isn't in the posting
    REFUSED = "refused"  # the provider declined to answer
    ERROR = "error"  # network or API error: no usable answer


# --- Tables -----------------------------------------------------------------------------------

# Columns the extraction always fills on success. num_nulls() is a Postgres function that counts
# its NULL arguments; the CHECK below makes "ok" rows complete at the database level, not only in
# Python, so a bug that writes a half-filled row fails loudly.
_REQUIRED_WHEN_OK = (
    "seniority",
    "role_family",
    "employment_type",
    "remote",
    "latam_hiring",
    "salary_basis",
    "summary",
)


class PostingEnrichment(Base):
    """The latest enrichment of a posting: one row per posting, replaced when it goes stale.

    Stale = the posting's content_hash no longer equals source_content_hash, or the prompt version
    changed. That comparison is the enrich pipeline's to-do list, which is why there is no
    --resume: an interrupted run leaves stale rows behind, and the next run picks them up.
    """

    __tablename__ = "posting_enrichments"
    __table_args__ = (
        one_of("status", EnrichmentStatus),
        one_of("seniority", Seniority),
        one_of("role_family", RoleFamily),
        one_of("employment_type", EmploymentType),
        one_of("remote", Remote),
        one_of("latam_hiring", LatamHiring),
        one_of("salary_period", SalaryPeriod),
        one_of("salary_basis", SalaryBasis),
        CheckConstraint("salary_min <= salary_max", name="salary_range"),
        CheckConstraint(
            f"status <> 'ok' OR num_nulls({', '.join(_REQUIRED_WHEN_OK)}) = 0", name="ok_is_complete"
        ),
        Index(None, "last_run_id"),
        Index(None, "dedup_key"),  # cross-source linking matches on it (HN postings, step s4)
    )

    # The primary key is also the foreign key: at most one enrichment per posting. CASCADE: if a
    # posting is ever deleted, its enrichment goes with it.
    posting_id: Mapped[int] = mapped_column(ForeignKey("postings.id", ondelete="CASCADE"), primary_key=True)
    source_content_hash: Mapped[str] = mapped_column(Text)  # postings.content_hash it was made from
    prompt_version: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)  # "claude", "gemini"
    model: Mapped[str] = mapped_column(Text)  # e.g. "claude-haiku-5-5"
    status: Mapped[str] = mapped_column(Text)
    # Failed attempts on this content and prompt version; after 3 the posting is skipped until either
    # changes, so a posting the model can't handle doesn't cost money on every run.
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)

    # The extracted fields (EnrichmentOut). Nullable because a failed row has none of them.
    company: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    seniority: Mapped[str | None] = mapped_column(Text)
    role_family: Mapped[str | None] = mapped_column(Text)
    employment_type: Mapped[str | None] = mapped_column(Text)
    remote: Mapped[str | None] = mapped_column(Text)
    latam_hiring: Mapped[str | None] = mapped_column(Text)
    latam_evidence: Mapped[str | None] = mapped_column(Text)  # a sentence quoted from the posting
    location_restrictions: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    skills_required: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    skills_nice: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    years_experience_min: Mapped[int | None] = mapped_column(Integer)
    languages: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(Text)
    salary_period: Mapped[str | None] = mapped_column(Text)
    salary_basis: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)

    # normalized company + title, built with the same dedup_key() as postings, so HN postings
    # (which have no company in the source) can be linked to copies on other boards.
    dedup_key: Mapped[str | None] = mapped_column(Text)
    last_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_runs.run_id"))
    enriched_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


class LlmCall(Base):
    """One row per LLM call: what was asked, of which model, what it cost and how it went.

    The full history, retries and failures included; posting_enrichments only keeps the latest
    result. Run totals (calls, tokens, cost) are SUM()s over this table.
    """

    __tablename__ = "llm_calls"
    __table_args__ = (
        one_of("purpose", LlmPurpose),
        one_of("status", LlmCallStatus),
        Index(None, "run_id"),
        Index(None, "posting_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_runs.run_id", ondelete="CASCADE"))
    # SET NULL: the cost history survives even if the posting it was about is deleted.
    posting_id: Mapped[int | None] = mapped_column(ForeignKey("postings.id", ondelete="SET NULL"))
    purpose: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    # NULL when the call failed before the provider reported usage (e.g. a network error).
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    # NUMERIC (exact decimal), not float: money summed over thousands of calls must not drift.
    # (10, 6) = up to 10 digits, 6 after the point: a single call costs fractions of a cent.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # the raw answer, for debugging
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

"""Add the enrichment and retrieval tables, and the enriched_postings view.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-05

- posting_enrichments: the latest LLM enrichment of each posting (one row per posting).
- llm_calls: every LLM call, with tokens, cost, latency and outcome.
- posting_chunks: chunks of each posting with a tsvector (keyword search) and an embedding
  (vector search). The `vector` type comes from pgvector, enabled in 0001.
- unaccent: a contrib extension whose unaccent('ingeniería') returns 'ingenieria', so keyword
  search matches Spanish words typed with or without accents.
- pipeline_runs.pipeline gets a CHECK: ingest, rebuild, enrich, index.
- enriched_postings: current_postings plus their enrichment, with "the source wins when it has a
  value, the LLM fills the gaps" applied to the fields both have.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values are written out here rather than imported from the enums: a migration must keep
# describing the schema as it was at this revision, even after the Python enums change.
SENIORITY = "('intern', 'junior', 'mid', 'senior', 'lead', 'principal', 'unknown')"
ROLE_FAMILY = (
    "('backend', 'frontend', 'fullstack', 'mobile', 'data_engineering', 'data_science', 'analytics', "
    "'ml_ai_engineering', 'ai_training', 'devops_platform_cloud', 'qa', 'security', 'embedded', "
    "'product', 'design', 'management', 'support', 'other')"
)
EMPLOYMENT_TYPE = "('full_time', 'part_time', 'contract', 'freelance', 'internship', 'unknown')"
REMOTE = "('remote', 'hybrid', 'onsite', 'unknown')"
LATAM_HIRING = "('yes', 'no', 'unknown')"
SALARY_PERIOD = "('hour', 'day', 'week', 'month', 'year')"
SALARY_BASIS = "('gross', 'net', 'unknown')"
LLM_CALL_STATUS = "('ok', 'invalid_output', 'ungrounded', 'refused', 'error')"
PIPELINES = "('ingest', 'rebuild', 'enrich', 'index')"

EMPTY_TEXT_ARRAY = sa.text("'{}'::text[]")

# Reads current_postings (one row per job group, see 0003) and adds the enrichment columns.
# - LEFT JOIN: postings without an enrichment (not yet enriched) still appear, with NULLs.
# - `e.status = 'ok'` in the ON clause: a failed enrichment counts as no enrichment.
# - resolved_*: the source's value when it has one, otherwise the LLM's (COALESCE returns its first
#   non-NULL argument). The source's remote 'unknown' counts as "no value" (NULLIF turns it into
#   NULL). Salary is resolved as a whole, never mixing the source's minimum with the LLM's maximum.
# - The enrichment's normalized seniority is exposed as seniority_level; `seniority` stays the
#   source's free text. Filters use the normalized one.
ENRICHED_POSTINGS = """
    CREATE VIEW enriched_postings AS
    SELECT
        c.*,
        e.status AS enrichment_status,
        COALESCE(c.title, e.title) AS resolved_title,
        COALESCE(c.company, e.company) AS resolved_company,
        COALESCE(NULLIF(c.remote, 'unknown'), e.remote, 'unknown') AS resolved_remote,
        CASE WHEN has_source_salary THEN c.salary_min ELSE e.salary_min END AS resolved_salary_min,
        CASE WHEN has_source_salary THEN c.salary_max ELSE e.salary_max END AS resolved_salary_max,
        CASE WHEN has_source_salary THEN c.salary_currency ELSE e.salary_currency END
            AS resolved_salary_currency,
        CASE WHEN has_source_salary THEN c.salary_period ELSE e.salary_period END AS resolved_salary_period,
        e.salary_basis,
        e.seniority AS seniority_level,
        e.role_family,
        e.employment_type,
        e.latam_hiring,
        e.latam_evidence,
        e.location_restrictions,
        e.skills_required,
        e.skills_nice,
        e.years_experience_min,
        e.languages,
        e.summary
    FROM current_postings c
    -- LATERAL lets the subquery use c's columns: it computes has_source_salary once per row.
    CROSS JOIN LATERAL (
        SELECT (c.salary_min IS NOT NULL OR c.salary_max IS NOT NULL) AS has_source_salary
    ) s
    LEFT JOIN posting_enrichments e ON e.posting_id = c.id AND e.status = 'ok'
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")

    op.create_check_constraint(op.f("ck_pipeline_runs_pipeline"), "pipeline_runs", f"pipeline IN {PIPELINES}")

    op.create_table(
        "posting_enrichments",
        sa.Column("posting_id", sa.BigInteger(), nullable=False),
        sa.Column("source_content_hash", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("seniority", sa.Text(), nullable=True),
        sa.Column("role_family", sa.Text(), nullable=True),
        sa.Column("employment_type", sa.Text(), nullable=True),
        sa.Column("remote", sa.Text(), nullable=True),
        sa.Column("latam_hiring", sa.Text(), nullable=True),
        sa.Column("latam_evidence", sa.Text(), nullable=True),
        sa.Column(
            "location_restrictions",
            postgresql.ARRAY(sa.Text()),
            server_default=EMPTY_TEXT_ARRAY,
            nullable=False,
        ),
        sa.Column(
            "skills_required", postgresql.ARRAY(sa.Text()), server_default=EMPTY_TEXT_ARRAY, nullable=False
        ),
        sa.Column(
            "skills_nice", postgresql.ARRAY(sa.Text()), server_default=EMPTY_TEXT_ARRAY, nullable=False
        ),
        sa.Column("years_experience_min", sa.Integer(), nullable=True),
        sa.Column("languages", postgresql.ARRAY(sa.Text()), server_default=EMPTY_TEXT_ARRAY, nullable=False),
        sa.Column("salary_min", sa.Integer(), nullable=True),
        sa.Column("salary_max", sa.Integer(), nullable=True),
        sa.Column("salary_currency", sa.Text(), nullable=True),
        sa.Column("salary_period", sa.Text(), nullable=True),
        sa.Column("salary_basis", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("dedup_key", sa.Text(), nullable=True),
        sa.Column("last_run_id", sa.UUID(), nullable=False),
        sa.Column("enriched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('ok', 'failed')", name=op.f("ck_posting_enrichments_status")),
        sa.CheckConstraint(f"seniority IN {SENIORITY}", name=op.f("ck_posting_enrichments_seniority")),
        sa.CheckConstraint(f"role_family IN {ROLE_FAMILY}", name=op.f("ck_posting_enrichments_role_family")),
        sa.CheckConstraint(
            f"employment_type IN {EMPLOYMENT_TYPE}", name=op.f("ck_posting_enrichments_employment_type")
        ),
        sa.CheckConstraint(f"remote IN {REMOTE}", name=op.f("ck_posting_enrichments_remote")),
        sa.CheckConstraint(
            f"latam_hiring IN {LATAM_HIRING}", name=op.f("ck_posting_enrichments_latam_hiring")
        ),
        sa.CheckConstraint(
            f"salary_period IN {SALARY_PERIOD}", name=op.f("ck_posting_enrichments_salary_period")
        ),
        sa.CheckConstraint(
            f"salary_basis IN {SALARY_BASIS}", name=op.f("ck_posting_enrichments_salary_basis")
        ),
        sa.CheckConstraint("salary_min <= salary_max", name=op.f("ck_posting_enrichments_salary_range")),
        sa.CheckConstraint(
            "status <> 'ok' OR num_nulls(seniority, role_family, employment_type, remote, latam_hiring, "
            "salary_basis, summary) = 0",
            name=op.f("ck_posting_enrichments_ok_is_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["posting_id"],
            ["postings.id"],
            name=op.f("fk_posting_enrichments_posting_id_postings"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["last_run_id"],
            ["pipeline_runs.run_id"],
            name=op.f("fk_posting_enrichments_last_run_id_pipeline_runs"),
        ),
        sa.PrimaryKeyConstraint("posting_id", name=op.f("pk_posting_enrichments")),
    )
    op.create_index(op.f("ix_posting_enrichments_last_run_id"), "posting_enrichments", ["last_run_id"])
    op.create_index(op.f("ix_posting_enrichments_dedup_key"), "posting_enrichments", ["dedup_key"])

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("posting_id", sa.BigInteger(), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("response", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("purpose IN ('enrich')", name=op.f("ck_llm_calls_purpose")),
        sa.CheckConstraint(f"status IN {LLM_CALL_STATUS}", name=op.f("ck_llm_calls_status")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["pipeline_runs.run_id"],
            name=op.f("fk_llm_calls_run_id_pipeline_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["posting_id"],
            ["postings.id"],
            name=op.f("fk_llm_calls_posting_id_postings"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )
    op.create_index(op.f("ix_llm_calls_run_id"), "llm_calls", ["run_id"])
    op.create_index(op.f("ix_llm_calls_posting_id"), "llm_calls", ["posting_id"])

    op.create_table(
        "posting_chunks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("posting_id", sa.BigInteger(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("tsv", postgresql.TSVECTOR(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("index_fingerprint", sa.Text(), nullable=False),
        sa.Column("last_run_id", sa.UUID(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["posting_id"],
            ["postings.id"],
            name=op.f("fk_posting_chunks_posting_id_postings"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["last_run_id"],
            ["pipeline_runs.run_id"],
            name=op.f("fk_posting_chunks_last_run_id_pipeline_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_posting_chunks")),
        sa.UniqueConstraint(
            "posting_id", "chunk_index", name=op.f("uq_posting_chunks_posting_id_chunk_index")
        ),
    )
    op.create_index(op.f("ix_posting_chunks_tsv"), "posting_chunks", ["tsv"], postgresql_using="gin")
    op.create_index(op.f("ix_posting_chunks_last_run_id"), "posting_chunks", ["last_run_id"])

    op.execute(ENRICHED_POSTINGS)


def downgrade() -> None:
    op.execute("DROP VIEW enriched_postings")
    op.drop_table("posting_chunks")
    op.drop_table("llm_calls")
    op.drop_table("posting_enrichments")
    # Runs of the new pipelines would violate nothing after the drop, but they mean nothing to the
    # older schema either; removing them keeps the ledger consistent with what the code can read.
    op.execute("DELETE FROM pipeline_runs WHERE pipeline IN ('enrich', 'index')")
    op.drop_constraint(op.f("ck_pipeline_runs_pipeline"), "pipeline_runs", type_="check")
    op.execute("DROP EXTENSION IF EXISTS unaccent")

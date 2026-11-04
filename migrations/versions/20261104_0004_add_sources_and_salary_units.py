"""Add the Himalayas and Jobicy sources, and salary currency/period columns.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-04

- The `source` CHECK constraints list the allowed sources, so a new source means replacing them
  (Postgres can't alter a CHECK in place: drop, then add). This is the "one-line migration"
  advantage of TEXT + CHECK over Postgres ENUM types mentioned in models.py.
- salary_currency / salary_period: sources now give salaries in different units (GetOnBoard
  USD per month, Himalayas and Jobicy hourly or yearly, sometimes not in USD). No backfill here:
  GetOnBoard's parser now sets USD/month, which changes those postings' content_hash, so the next
  ingest (or `jobradar transform --rebuild`) rewrites them through the normal upsert.
- current_postings is re-created: its `postings.*` was expanded when it was created, so without
  this the new columns wouldn't appear in it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES_WITH_SOURCE = ("pipeline_run_sources", "raw_postings", "postings", "rejected_postings")
OLD_SOURCES = "('getonboard', 'remotive', 'hn')"
NEW_SOURCES = "('getonboard', 'remotive', 'hn', 'himalayas', 'jobicy')"

# Same definition as in 0003 (migrations stay self-contained: each one keeps working even if a
# later one changes this text).
CURRENT_POSTINGS = """
    CREATE VIEW current_postings AS
    SELECT DISTINCT ON (COALESCE(canonical_id, id))
        COALESCE(canonical_id, id) AS group_id,
        postings.*
    FROM postings
    WHERE is_active
    ORDER BY COALESCE(canonical_id, id), (canonical_id IS NULL) DESC, first_seen_at, id
"""


def _replace_source_checks(allowed: str) -> None:
    for table in TABLES_WITH_SOURCE:
        name = f"ck_{table}_source"
        # op.f(): the name is final. Without it, the naming convention in db.py would be applied
        # again, producing "ck_<table>_ck_<table>_source", which doesn't exist.
        op.drop_constraint(op.f(name), table, type_="check")
        op.create_check_constraint(op.f(name), table, f"source IN {allowed}")


def upgrade() -> None:
    _replace_source_checks(NEW_SOURCES)

    # The view depends on the table's columns, so it is dropped before altering and re-created after.
    op.execute("DROP VIEW current_postings")
    op.add_column("postings", sa.Column("salary_currency", sa.Text(), nullable=True))
    op.add_column("postings", sa.Column("salary_period", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_postings_salary_period"),
        "postings",
        "salary_period IN ('hour', 'day', 'week', 'month', 'year')",
    )
    op.execute(CURRENT_POSTINGS)


def downgrade() -> None:
    op.execute("DROP VIEW current_postings")
    op.drop_constraint(op.f("ck_postings_salary_period"), "postings", type_="check")
    op.drop_column("postings", "salary_period")
    op.drop_column("postings", "salary_currency")
    op.execute(CURRENT_POSTINGS)
    # Rows from the new sources would violate the old constraints; remove them first.
    for table in ("rejected_postings", "postings", "raw_postings", "pipeline_run_sources"):
        op.execute(f"DELETE FROM {table} WHERE source IN ('himalayas', 'jobicy')")  # noqa: S608
    _replace_source_checks(OLD_SOURCES)

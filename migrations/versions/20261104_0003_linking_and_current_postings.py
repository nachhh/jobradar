"""Enable pg_trgm and create the current_postings view.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-04

- pg_trgm adds trigram similarity: similarity('acme analytics', 'acme analytic') is the share of
  3-letter chunks the two strings have in common (0 to 1). The linking step uses it to match
  company and title names that are spelled slightly differently across job boards.
- current_postings is what search and the agent read: one row per job, whichever boards posted
  it (design decision: the canonical is fixed identity; the representative is picked at read time).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pg_trgm ships with Postgres ("contrib"), so it only needs enabling, like pgvector.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # DISTINCT ON (group) keeps the first row of each group under the ORDER BY:
    #   - group_id: the canonical's id; every member shares it (the canonical's canonical_id is NULL);
    #   - WHERE is_active: closed copies never represent the group, and an all-closed group vanishes;
    #   - (canonical_id IS NULL) DESC: the canonical itself first, when it's active;
    #   - first_seen_at, id: otherwise the oldest active copy (id breaks ties deterministically).
    # A view stores the query, not data: it always reflects the current rows.
    # Note: `postings.*` is expanded when the view is created, so a column added to postings later
    # needs the view re-created in that migration to show up here.
    op.execute("""
        CREATE VIEW current_postings AS
        SELECT DISTINCT ON (COALESCE(canonical_id, id))
            COALESCE(canonical_id, id) AS group_id,
            postings.*
        FROM postings
        WHERE is_active
        ORDER BY COALESCE(canonical_id, id), (canonical_id IS NULL) DESC, first_seen_at, id
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS current_postings")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")

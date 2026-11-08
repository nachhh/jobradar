"""enriched_postings: remote_modes instead of resolved_remote.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-06

After the backfill, 27 GetOnBoard postings had a board modality that contradicts their own text
(e.g. board "fully_remote", description "modalidad híbrida"). With a single resolved value, one of
the two always loses: "source wins" shows a hybrid job as remote, and "resolve to hybrid" hides it
from remote searches. The owner's decision (design task, 2026-11-06): a contradiction counts as
*both*, so the posting appears in either search and the reader can ask the employer.

remote_modes is a text[]: one value when board and text agree (or only one of them says), two
when they disagree (the board's first). Filters test membership: 'remote' = ANY(remote_modes).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Shared by both versions of the view: everything except the remote column.
_SELECT = """
    CREATE VIEW enriched_postings AS
    SELECT
        c.*,
        e.status AS enrichment_status,
        COALESCE(c.title, e.title) AS resolved_title,
        COALESCE(c.company, e.company) AS resolved_company,
        {remote},
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
    CROSS JOIN LATERAL (
        SELECT (c.salary_min IS NOT NULL OR c.salary_max IS NOT NULL) AS has_source_salary
    ) s
    LEFT JOIN posting_enrichments e ON e.posting_id = c.id AND e.status = 'ok'
"""

# "unknown" counts as saying nothing. The CASE branches, in order: both say something different
# (both values), only the board says, only the LLM says, neither.
REMOTE_MODES = """CASE
            WHEN c.remote <> 'unknown' AND e.remote IS NOT NULL AND e.remote NOT IN ('unknown', c.remote)
                THEN ARRAY[c.remote, e.remote]
            WHEN c.remote <> 'unknown' THEN ARRAY[c.remote]
            WHEN e.remote IS NOT NULL AND e.remote <> 'unknown' THEN ARRAY[e.remote]
            ELSE ARRAY['unknown']
        END AS remote_modes"""

RESOLVED_REMOTE = "COALESCE(NULLIF(c.remote, 'unknown'), e.remote, 'unknown') AS resolved_remote"


def upgrade() -> None:
    op.execute("DROP VIEW enriched_postings")
    op.execute(_SELECT.format(remote=REMOTE_MODES))


def downgrade() -> None:
    op.execute("DROP VIEW enriched_postings")
    op.execute(_SELECT.format(remote=RESOLVED_REMOTE))

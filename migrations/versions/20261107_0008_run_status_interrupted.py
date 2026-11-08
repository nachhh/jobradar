"""pipeline_runs.status: add 'interrupted'.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-07

A run stopped by Ctrl-C or a kill signal now records `interrupted` instead of staying `running`
forever (ledger.run_ledger), and a run killed too hard to record anything (kill -9, power loss) is
marked `interrupted` by the next run of its pipeline (ledger.sweep_dead_runs). A separate value,
not `partial`, so the ledger can tell "a source is down" from "someone stopped the run".

Allowed values are a CHECK constraint (see the ERD), so adding one means dropping and re-creating it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "('running', 'succeeded', 'partial', 'failed')"
NEW = "('running', 'succeeded', 'partial', 'failed', 'interrupted')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_pipeline_runs_status"), "pipeline_runs", type_="check")
    op.create_check_constraint(op.f("ck_pipeline_runs_status"), "pipeline_runs", f"status IN {NEW}")


def downgrade() -> None:
    # The old constraint would reject existing 'interrupted' rows; 'failed' is the closest old value.
    op.execute("UPDATE pipeline_runs SET status = 'failed' WHERE status = 'interrupted'")
    op.drop_constraint(op.f("ck_pipeline_runs_status"), "pipeline_runs", type_="check")
    op.create_check_constraint(op.f("ck_pipeline_runs_status"), "pipeline_runs", f"status IN {OLD}")

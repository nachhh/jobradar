"""Record prompt-cache tokens on llm_calls.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-05

Live calls showed that about two thirds of each enrichment request (the instructions plus the
schema, ~3,300 tokens) is identical every time, so it is served from the provider's prompt cache:
written once at 1.25x the input price, then read at 0.1x. Those tokens are billed apart from the
rest, so they get their own columns; without them, input_tokens and output_tokens wouldn't explain
cost_usd. Nullable like the other counts: NULL when the call failed before reporting usage.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("llm_calls", sa.Column("cache_write_tokens", sa.Integer(), nullable=True))
    op.add_column("llm_calls", sa.Column("cache_read_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_calls", "cache_read_tokens")
    op.drop_column("llm_calls", "cache_write_tokens")

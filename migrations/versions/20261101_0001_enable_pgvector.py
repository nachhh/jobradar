"""Enable the pgvector extension.

Revision ID: 0001
Revises:
Create Date: 2026-11-01

The Docker image ships the pgvector binaries, but each database must opt in with
CREATE EXTENSION. After this, columns of type `vector(n)` and the distance operators
(<-> Euclidean, <=> cosine, <#> negative inner product) are available.
"""

from collections.abc import Sequence

from alembic import op

# Alembic chains migrations through these IDs: each one names its parent in down_revision.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS makes the migration safe to run on a database that already has it.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector")

"""Integration tests: run against the real Postgres test database (needs `make up`)."""

import pytest
from sqlalchemy import Engine, text

from jobradar.db import check_database

# Applies the "integration" marker to every test in this file.
# `uv run pytest -m "not integration"` runs only the unit tests.
pytestmark = pytest.mark.integration


def test_migrations_install_pgvector(db_engine: Engine) -> None:
    health = check_database(db_engine)
    assert health.pgvector_version is not None


def test_vector_distances_in_sql(db_engine: Engine) -> None:
    """The operators retrieval will rely on in Phase 2, checked on tiny hand-computable vectors."""
    with db_engine.connect() as conn:
        # <-> is Euclidean (L2) distance: sqrt((1-1)² + (2-2)² + (3-5)²) = 2
        l2 = conn.execute(text("SELECT '[1,2,3]'::vector <-> '[1,2,5]'::vector")).scalar_one()
        # <=> is cosine distance = 1 - cosine similarity. Perpendicular vectors: 1 - 0 = 1
        cosine = conn.execute(text("SELECT '[1,0]'::vector <=> '[0,1]'::vector")).scalar_one()
    assert l2 == pytest.approx(2.0)
    assert cosine == pytest.approx(1.0)

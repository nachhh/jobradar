"""Database access: the SQLAlchemy engine, the declarative Base for models, and a health check.

Concepts:
- The *engine* owns a pool of connections to Postgres. Create it once per process and reuse it;
  opening a fresh connection per query is slow (TCP + auth handshake each time).
- *Base* is the parent class of every ORM model (tables arrive in Phase 1). Alembic compares
  Base.metadata with the live database to autogenerate migrations.
"""

from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase

from jobradar.config import get_settings


class Base(DeclarativeBase):
    """Parent class for all ORM models. Empty in Phase 0."""


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    """Return a shared engine for `url` (default: DATABASE_URL from settings).

    pool_pre_ping=True runs a tiny "SELECT 1" before handing out a pooled connection, so a
    connection killed by a database restart is replaced instead of failing the next query.
    """
    return create_engine(url or get_settings().database_url, pool_pre_ping=True)


@dataclass(frozen=True)
class DatabaseHealth:
    postgres_version: str
    # None when the database is up but the vector extension isn't installed (migrations not run).
    pgvector_version: str | None


def check_database(engine: Engine) -> DatabaseHealth:
    """Connect and report the server and pgvector versions. Raises if the database is unreachable."""
    # `with engine.connect()` borrows a connection from the pool and returns it on exit.
    with engine.connect() as conn:
        # text() marks a raw SQL string. Fine for fixed queries; never build SQL by
        # concatenating user input (SQL injection) — pass parameters instead.
        postgres_version = conn.execute(text("SHOW server_version")).scalar_one()
        # pg_extension lists the extensions installed in *this* database.
        pgvector_version = conn.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one_or_none()
    return DatabaseHealth(postgres_version=postgres_version, pgvector_version=pgvector_version)

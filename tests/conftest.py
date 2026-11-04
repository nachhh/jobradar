"""Shared pytest fixtures. pytest discovers this file automatically and makes its fixtures
available to every test by parameter name (a test asking for `db_engine` gets the one below)."""

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text

from jobradar.config import get_settings


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    """An engine on a freshly migrated `jobradar_test` database.

    scope="session": built once for the whole test run, not once per test, because resetting
    the schema and running migrations is the slow part.
    """
    url = get_settings().test_database_url
    # Guard against pointing the tests at a real database: the next step deletes everything.
    assert url.rsplit("/", 1)[-1].endswith("_test"), f"refusing to reset a non-test database: {url}"

    engine = create_engine(url)
    # Start from an empty schema so every run sees exactly what the migrations create.
    # engine.begin() opens a transaction and commits it on exit.
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    # Run `alembic upgrade head` from Python. This also tests the migrations themselves.
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.attributes["database_url"] = url  # read by migrations/env.py
    command.upgrade(alembic_cfg, "head")

    yield engine  # tests run here
    engine.dispose()  # teardown: close pooled connections


@pytest.fixture
def db_conn(db_engine: Engine) -> Iterator[Connection]:
    """A connection inside a transaction that is rolled back after the test.

    Default scope="function": a fresh one per test. Whatever the test writes disappears at the
    rollback, so tests can't see each other's rows and the order they run in doesn't matter.
    """
    with db_engine.connect() as conn:
        transaction = conn.begin()
        yield conn
        transaction.rollback()


@pytest.fixture
def clean_db(db_engine: Engine) -> Iterator[Engine]:
    """The test engine with empty ingestion tables, emptied again afterwards.

    For code that opens and commits its own transactions (the pipeline), which `db_conn`'s
    rollback can't contain. TRUNCATE empties tables at once; CASCADE covers foreign keys.
    """

    def truncate() -> None:
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE pipeline_runs, pipeline_run_sources, raw_postings, postings, rejected_postings "
                    "RESTART IDENTITY CASCADE"
                )
            )

    truncate()
    yield db_engine
    truncate()

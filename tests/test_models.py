"""Integration tests for the ingestion schema: the migration matches the models, and the
constraints reject the bad data they exist to reject."""

import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, Engine, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from jobradar.config import get_settings
from jobradar.db import Base
from jobradar.models import PipelineRun, PipelineRunSource, Posting, Source

pytestmark = pytest.mark.integration


def _new_run(conn: Connection) -> uuid.UUID:
    """Insert a run and return its server-generated run_id (RETURNING reads it back).

    Core statements accept model classes (insert(PipelineRun)) as well as tables, and run on a
    plain Connection: no ORM Session or object tracking involved.
    """
    stmt = insert(PipelineRun).values(pipeline="ingest", trigger="manual").returning(PipelineRun.run_id)
    return conn.execute(stmt).scalar_one()


def _posting(run_id: uuid.UUID, **overrides: object) -> dict[str, object]:
    """The minimum columns a posting needs (the rest have defaults), with optional overrides."""
    row: dict[str, object] = {
        "source": Source.GETONBOARD,
        "src_posting_id": "data-engineer-acme-remote-9cff",
        "url": "https://www.getonbrd.com/jobs/data-engineer-acme-remote-9cff",
        "description": "Build pipelines.",
        "content_hash": "abc",
        "last_run_id": run_id,
    }
    return row | overrides


def test_models_match_migrations(db_engine: Engine) -> None:
    """The schema the migrations built equals the one the models describe.

    compare_metadata is what `alembic revision --autogenerate` runs internally: an empty diff
    means nobody changed a model without writing the matching migration (or the other way round).
    """
    with db_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_downgrade_and_upgrade_again(db_engine: Engine) -> None:
    """The downgrade really undoes the upgrade, so a bad migration can be rolled back."""
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = get_settings().test_database_url
    command.downgrade(cfg, "0001")
    command.upgrade(cfg, "head")  # leave the schema as the other tests expect it

    with db_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(Posting)).scalar_one() == 0


def test_natural_key_is_unique(db_conn: Connection) -> None:
    run_id = _new_run(db_conn)
    db_conn.execute(insert(Posting).values(_posting(run_id)))

    # begin_nested() = a SAVEPOINT. A failed statement aborts the whole Postgres transaction;
    # rolling back to the savepoint keeps the rest of the test's transaction usable.
    with pytest.raises(IntegrityError, match="uq_postings_source_src_posting_id"), db_conn.begin_nested():
        db_conn.execute(insert(Posting).values(_posting(run_id)))

    # The same src_posting_id from another source is a different posting: allowed.
    db_conn.execute(insert(Posting).values(_posting(run_id, source=Source.REMOTIVE)))


def test_unknown_source_is_rejected(db_conn: Connection) -> None:
    run_id = _new_run(db_conn)
    with pytest.raises(IntegrityError, match="ck_postings_source"), db_conn.begin_nested():
        db_conn.execute(insert(Posting).values(_posting(run_id, source="linkedin")))


def test_salary_range_must_be_ordered(db_conn: Connection) -> None:
    run_id = _new_run(db_conn)
    with pytest.raises(IntegrityError, match="ck_postings_salary_range"), db_conn.begin_nested():
        db_conn.execute(insert(Posting).values(_posting(run_id, salary_min=5000, salary_max=3000)))


def test_posting_cannot_be_its_own_canonical(db_conn: Connection) -> None:
    run_id = _new_run(db_conn)
    posting_id = db_conn.execute(insert(Posting).values(_posting(run_id)).returning(Posting.id)).scalar_one()
    with pytest.raises(IntegrityError, match="ck_postings_canonical_not_self"), db_conn.begin_nested():
        db_conn.execute(update(Posting).where(Posting.id == posting_id).values(canonical_id=posting_id))


def test_defaults_are_filled_by_postgres(db_conn: Connection) -> None:
    run_id = _new_run(db_conn)
    row = db_conn.execute(insert(Posting).values(_posting(run_id)).returning(*Posting.__table__.c)).one()
    assert row.remote == "unknown"
    assert row.is_active is True
    assert row.tags == []
    assert row.first_seen_at is not None


def test_deleting_a_run_deletes_its_source_rows(db_conn: Connection) -> None:
    run_id = _new_run(db_conn)
    db_conn.execute(insert(PipelineRunSource).values(run_id=run_id, source=Source.HN))

    db_conn.execute(delete(PipelineRun).where(PipelineRun.run_id == run_id))

    remaining = db_conn.execute(select(func.count()).select_from(PipelineRunSource)).scalar_one()
    assert remaining == 0

"""Integration tests for the schema: the migrations match the models, the constraints reject the
bad data they exist to reject, and the views return what they promise."""

import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, Engine, delete, func, insert, select, text, update
from sqlalchemy.exc import DataError, IntegrityError

from jobradar.config import get_settings
from jobradar.db import Base
from jobradar.models import (
    EMBEDDING_DIM,
    LlmCall,
    PipelineRun,
    PipelineRunSource,
    Posting,
    PostingChunk,
    PostingEnrichment,
    Source,
)

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


def test_unknown_pipeline_is_rejected(db_conn: Connection) -> None:
    with pytest.raises(IntegrityError, match="ck_pipeline_runs_pipeline"), db_conn.begin_nested():
        db_conn.execute(insert(PipelineRun).values(pipeline="scrape", trigger="manual"))


# --- Phase 2: enrichment and retrieval ---------------------------------------------------------


def _enrichment(posting_id: int, run_id: uuid.UUID, **overrides: object) -> dict[str, object]:
    """A complete, successful enrichment row, with optional overrides."""
    row: dict[str, object] = {
        "posting_id": posting_id,
        "source_content_hash": "abc",
        "prompt_version": "v1",
        "provider": "fake",
        "model": "fake-model",
        "status": "ok",
        "seniority": "senior",
        "role_family": "data_engineering",
        "employment_type": "full_time",
        "remote": "remote",
        "latam_hiring": "yes",
        "salary_basis": "unknown",
        "summary": "Builds data pipelines.",
        "last_run_id": run_id,
    }
    return row | overrides


def _new_posting(conn: Connection, run_id: uuid.UUID, **overrides: object) -> int:
    return conn.execute(
        insert(Posting).values(_posting(run_id, **overrides)).returning(Posting.id)
    ).scalar_one()


def test_ok_enrichment_must_be_complete(db_conn: Connection) -> None:
    """An "ok" row with a missing required field is refused; a "failed" row may leave them empty."""
    run_id = _new_run(db_conn)
    posting_id = _new_posting(db_conn, run_id)

    with pytest.raises(IntegrityError, match="ck_posting_enrichments_ok_is_complete"), db_conn.begin_nested():
        db_conn.execute(insert(PostingEnrichment).values(_enrichment(posting_id, run_id, summary=None)))

    failed = {
        "status": "failed",
        "seniority": None,
        "role_family": None,
        "summary": None,
        "error": "bad JSON",
    }
    db_conn.execute(insert(PostingEnrichment).values(_enrichment(posting_id, run_id, **failed)))


def test_unknown_role_family_is_rejected(db_conn: Connection) -> None:
    run_id = _new_run(db_conn)
    posting_id = _new_posting(db_conn, run_id)
    with pytest.raises(IntegrityError, match="ck_posting_enrichments_role_family"), db_conn.begin_nested():
        db_conn.execute(
            insert(PostingEnrichment).values(_enrichment(posting_id, run_id, role_family="wizard"))
        )


def test_deleting_a_posting_keeps_its_llm_calls(db_conn: Connection) -> None:
    """Enrichment and chunks go with the posting (CASCADE); the cost history stays (SET NULL)."""
    run_id = _new_run(db_conn)
    posting_id = _new_posting(db_conn, run_id)
    db_conn.execute(insert(PostingEnrichment).values(_enrichment(posting_id, run_id)))
    db_conn.execute(
        insert(LlmCall).values(
            run_id=run_id,
            posting_id=posting_id,
            purpose="enrich",
            provider="fake",
            model="fake-model",
            prompt_version="v1",
            status="ok",
        )
    )

    db_conn.execute(delete(Posting).where(Posting.id == posting_id))

    assert db_conn.execute(select(func.count()).select_from(PostingEnrichment)).scalar_one() == 0
    assert db_conn.execute(select(LlmCall.posting_id)).scalar_one() is None


def _chunk(posting_id: int, run_id: uuid.UUID, body: str, embedding: list[float]) -> dict[str, object]:
    return {
        "posting_id": posting_id,
        "chunk_index": 0,
        "text": body,
        # The same expression the indexer will use: accents stripped, words kept as they are.
        "tsv": func.to_tsvector("simple", func.unaccent(body)),
        "embedding": embedding,
        "embedding_model": "fake",
        "index_fingerprint": "f1",
        "last_run_id": run_id,
    }


def test_embedding_must_have_the_model_dimension(db_conn: Connection) -> None:
    """pgvector refuses a vector of the wrong length (a data error, not a constraint violation)."""
    run_id = _new_run(db_conn)
    posting_id = _new_posting(db_conn, run_id)
    with pytest.raises(DataError, match=f"expected {EMBEDDING_DIM} dimensions"), db_conn.begin_nested():
        db_conn.execute(insert(PostingChunk).values(_chunk(posting_id, run_id, "text", [0.1, 0.2, 0.3])))


def test_keyword_search_ignores_accents(db_conn: Connection) -> None:
    """With 'simple' + unaccent, Spanish words match whether or not the query has the accents."""
    run_id = _new_run(db_conn)
    posting_id = _new_posting(db_conn, run_id)
    body = "Buscamos ingeniería de datos con Python"
    db_conn.execute(insert(PostingChunk).values(_chunk(posting_id, run_id, body, [0.0] * EMBEDDING_DIM)))

    def matches(query: str) -> bool:
        stmt = text(
            "SELECT count(*) FROM posting_chunks WHERE tsv @@ websearch_to_tsquery('simple', unaccent(:q))"
        )
        return db_conn.execute(stmt, {"q": query}).scalar_one() == 1

    assert matches("ingeniería python")
    assert matches("ingenieria PYTHON")
    assert not matches("ingeniero")  # no stemming: a different word doesn't match (vectors cover it)


def test_enriched_postings_prefers_source_values(db_conn: Connection) -> None:
    """The source wins when it has a value; the LLM fills the gaps; a failed enrichment is ignored."""
    run_id = _new_run(db_conn)
    # An HN-like posting: no company or title in the source, salary from the source.
    hn_id = _new_posting(
        db_conn, run_id, source=Source.HN, src_posting_id="1", salary_min=4000, salary_currency="USD"
    )
    db_conn.execute(
        insert(PostingEnrichment).values(
            _enrichment(hn_id, run_id, company="Acme", title="Data Engineer", salary_min=1, salary_max=2)
        )
    )
    # A posting whose enrichment failed.
    failed_id = _new_posting(db_conn, run_id, src_posting_id="2", company="Globex")
    failed = {"status": "failed", "seniority": None, "role_family": None, "summary": None}
    db_conn.execute(insert(PostingEnrichment).values(_enrichment(failed_id, run_id, **failed)))

    rows = {r.id: r for r in db_conn.execute(text("SELECT * FROM enriched_postings")).all()}

    hn = rows[hn_id]
    assert (hn.resolved_company, hn.resolved_title) == ("Acme", "Data Engineer")  # LLM fills the gaps
    assert (hn.resolved_salary_min, hn.resolved_salary_max) == (4000, None)  # source salary, not mixed
    assert hn.remote_modes == ["remote"]  # source said "unknown", so the LLM's value is used
    assert hn.seniority_level == "senior"

    assert rows[failed_id].resolved_company == "Globex"
    assert rows[failed_id].enrichment_status is None  # failed counts as not enriched
    assert rows[failed_id].remote_modes == ["unknown"]


def test_remote_conflict_counts_as_both(db_conn: Connection) -> None:
    """Board says remote, the posting's text says hybrid: the job matches either search."""
    run_id = _new_run(db_conn)
    posting_id = _new_posting(db_conn, run_id, remote="remote")
    db_conn.execute(insert(PostingEnrichment).values(_enrichment(posting_id, run_id, remote="hybrid")))

    row = db_conn.execute(
        text("SELECT remote_modes FROM enriched_postings WHERE id = :id"), {"id": posting_id}
    ).one()

    assert row.remote_modes == ["remote", "hybrid"]  # the board's value first
    matches = "SELECT count(*) FROM enriched_postings WHERE :mode = ANY(remote_modes)"
    assert db_conn.execute(text(matches), {"mode": "remote"}).scalar_one() == 1
    assert db_conn.execute(text(matches), {"mode": "hybrid"}).scalar_one() == 1

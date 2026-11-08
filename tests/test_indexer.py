"""Integration tests for the index pipeline, with FakeEmbedder (no model download)."""

import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import Engine, func, insert, select, text, update

from jobradar.models import EMBEDDING_DIM, PipelineRun, Posting, PostingChunk, PostingEnrichment, Source
from jobradar.retrieval import chunk, indexer
from jobradar.retrieval.embed import FakeEmbedder, FastEmbedder
from jobradar.retrieval.indexer import NothingToIndexError, run_index

pytestmark = pytest.mark.integration


def add_posting(engine: Engine, description: str, **overrides: Any) -> int:
    with engine.begin() as conn:
        run_id = conn.execute(
            insert(PipelineRun).values(pipeline="ingest", trigger="manual").returning(PipelineRun.run_id)
        ).scalar_one()
        row = {
            "source": Source.GETONBOARD,
            "src_posting_id": uuid.uuid4().hex,
            "url": "https://example.com/job",
            "title": "Data Engineer",
            "company": "Acme",
            "description": description,
            "content_hash": uuid.uuid4().hex,
            "last_run_id": run_id,
        } | overrides
        return conn.execute(insert(Posting).values(row).returning(Posting.id)).scalar_one()


def chunks_of(engine: Engine, posting_id: int) -> list[Any]:
    with engine.connect() as conn:
        stmt = (
            select(PostingChunk)
            .where(PostingChunk.posting_id == posting_id)
            .order_by(PostingChunk.chunk_index)
        )
        return list(conn.execute(stmt).all())


def test_indexes_active_postings_then_has_nothing_to_do(clean_db: Engine) -> None:
    long_text = "\n".join(f"Requirement {i}: Python, Spark and Airflow experience." for i in range(80))
    posting_id = add_posting(clean_db, long_text)
    add_posting(clean_db, "Closed job.", is_active=False)

    result = run_index(clean_db, FakeEmbedder())

    assert result.postings == 1
    rows = chunks_of(clean_db, posting_id)
    assert len(rows) == result.chunks > 1  # long enough for several chunks
    assert all(r.text.startswith("Data Engineer at Acme") for r in rows)  # every chunk has the header
    assert len(rows[0].embedding) == EMBEDDING_DIM
    with pytest.raises(NothingToIndexError):
        run_index(clean_db, FakeEmbedder())


def test_changed_content_or_enrichment_reindexes_only_that_posting(clean_db: Engine) -> None:
    edited = add_posting(clean_db, "Build pipelines.")
    enriched = add_posting(clean_db, "Train models.")
    untouched = add_posting(clean_db, "Write tests.")
    run_index(clean_db, FakeEmbedder())

    with clean_db.begin() as conn:
        conn.execute(
            update(Posting).where(Posting.id == edited).values(content_hash="new", description="Build APIs.")
        )
        run_id = conn.execute(select(PipelineRun.run_id).limit(1)).scalar_one()
        # A new enrichment changes the header ("· senior …"), so the chunks are stale too.
        conn.execute(
            insert(PostingEnrichment).values(
                posting_id=enriched,
                source_content_hash="x",
                prompt_version="v3",
                provider="fake",
                model="fake-model",
                status="ok",
                seniority="senior",
                role_family="ml_ai_engineering",
                employment_type="full_time",
                remote="remote",
                latam_hiring="yes",
                salary_basis="unknown",
                summary="…",
                skills_required=["pytorch"],
                last_run_id=run_id,
            )
        )
    before = chunks_of(clean_db, untouched)[0].id

    result = run_index(clean_db, FakeEmbedder())

    assert result.postings == 2
    assert "Build APIs." in chunks_of(clean_db, edited)[0].text
    assert "senior · ml_ai_engineering · remote · LATAM: yes" in chunks_of(clean_db, enriched)[0].text
    assert chunks_of(clean_db, untouched)[0].id == before  # not rewritten


def test_new_chunker_version_reindexes_everything(clean_db: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    add_posting(clean_db, "Build pipelines.")
    add_posting(clean_db, "Train models.")
    run_index(clean_db, FakeEmbedder())

    monkeypatch.setattr(indexer, "CHUNKER_VERSION", chunk.CHUNKER_VERSION + "-next")
    assert run_index(clean_db, FakeEmbedder()).postings == 2


def test_keyword_index_matches_without_accents(clean_db: Engine) -> None:
    add_posting(clean_db, "Buscamos experiencia en ingeniería de datos.")
    run_index(clean_db, FakeEmbedder())
    with clean_db.connect() as conn:
        found = conn.execute(
            text(
                "SELECT count(*) FROM posting_chunks"
                " WHERE tsv @@ websearch_to_tsquery('simple', unaccent(:q))"
            ),
            {"q": "ingenieria datos"},
        ).scalar_one()
    assert found == 1


def test_similar_text_is_nearer_in_vector_space(clean_db: Engine) -> None:
    """The fake embedder behaves like an embedding: shared words → smaller cosine distance."""
    spark = add_posting(clean_db, "Spark and Airflow data pipelines.", title="Data Engineer")
    chef = add_posting(clean_db, "Pastry and desserts for our bakery.", title="Pastry Chef", company="Bakery")
    embedder = FakeEmbedder()
    run_index(clean_db, embedder)
    query = embedder.embed_query("data pipelines with Spark")
    with clean_db.connect() as conn:
        nearest = conn.execute(
            select(PostingChunk.posting_id).order_by(PostingChunk.embedding.cosine_distance(query)).limit(1)
        ).scalar_one()
        assert conn.execute(select(func.count()).select_from(PostingChunk)).scalar_one() == 2
    assert nearest == spark != chef


@pytest.mark.model
def test_real_model_ranks_by_meaning() -> None:
    """embeddinggemma itself (downloads ~1.2 GB on first run): `uv run pytest -m model`.

    A Spanish query finds the English data posting, with no words in common with it: what keyword
    search can't do, and the reason for vector search.
    """
    embedder = FastEmbedder(cache_dir=".cache/fastembed")
    docs = [
        ("Data Engineer", "Build batch pipelines with Spark and Airflow on AWS."),
        ("Pastry Chef", "Prepare desserts and pastries for our bakery."),
    ]
    data, chef = embedder.embed_documents(docs)
    query = embedder.embed_query("ingeniero de datos, procesamiento distribuido")

    def cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        return dot / (sum(x * x for x in a) ** 0.5 * sum(y * y for y in b) ** 0.5)

    assert len(data) == EMBEDDING_DIM
    assert cosine(query, data) > cosine(query, chef)
    assert embedder.count_tokens("Build batch pipelines") > 0


class StoppedEmbedder(FakeEmbedder):
    """Ctrl-C (or a kill) arriving during the second batch's embeddings."""

    def __init__(self) -> None:
        super().__init__()
        self.batches = 0

    def embed_documents(self, documents: Sequence[tuple[str | None, str]]) -> list[list[float]]:
        self.batches += 1
        if self.batches == 2:
            raise KeyboardInterrupt
        return super().embed_documents(documents)


def test_interrupted_run_keeps_finished_batches_and_records_interrupted(
    clean_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(indexer, "BATCH_SIZE", 1)  # one posting per batch
    first, second, third = (add_posting(clean_db, f"Job {i}.") for i in range(3))

    with pytest.raises(KeyboardInterrupt):
        run_index(clean_db, StoppedEmbedder())

    assert chunks_of(clean_db, first) and not chunks_of(clean_db, second) and not chunks_of(clean_db, third)
    with clean_db.connect() as conn:
        status = conn.execute(select(PipelineRun.status).where(PipelineRun.pipeline == "index")).scalar_one()
    assert status == "interrupted"
    assert run_index(clean_db, FakeEmbedder()).postings == 2  # the next run picks up the rest

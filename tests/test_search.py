"""Integration tests for hybrid search, with FakeEmbedder (shared words → similar vectors).

Each test builds a few postings, indexes them, and checks one property of search: fusion order,
filters applied inside the searches, one result per posting and per job group, reranking.
"""

import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import Engine, insert, select, update

from jobradar.models import PipelineRun, Posting, PostingEnrichment, Source
from jobradar.retrieval.embed import FakeEmbedder
from jobradar.retrieval.indexer import run_index
from jobradar.retrieval.search import SearchFilters, keyword_terms, search

pytestmark = pytest.mark.integration

EMBEDDER = FakeEmbedder()


def add(
    engine: Engine, title: str, description: str, *, source: Source = Source.GETONBOARD, **enrichment: Any
) -> int:
    """A posting with an enrichment (defaults below, overridable), returning its id."""
    with engine.begin() as conn:
        run_id = conn.execute(
            insert(PipelineRun).values(pipeline="ingest", trigger="manual").returning(PipelineRun.run_id)
        ).scalar_one()
        posting_id = conn.execute(
            insert(Posting)
            .values(
                source=source,
                src_posting_id=uuid.uuid4().hex,
                url=f"https://example.com/{uuid.uuid4().hex}",
                title=title,
                company="Acme",
                description=description,
                content_hash=uuid.uuid4().hex,
                last_run_id=run_id,
                remote="remote",
            )
            .returning(Posting.id)
        ).scalar_one()
        fields = {
            "seniority": "senior",
            "role_family": "data_engineering",
            "employment_type": "full_time",
            "remote": "remote",
            "latam_hiring": "yes",
            "salary_basis": "unknown",
            "summary": title,
        } | enrichment
        conn.execute(
            insert(PostingEnrichment).values(
                posting_id=posting_id,
                source_content_hash="x",
                prompt_version="v3",
                provider="fake",
                model="fake-model",
                status="ok",
                last_run_id=run_id,
                **fields,
            )
        )
        return posting_id


def ids(hits: Sequence[Any]) -> list[int]:
    return [hit.posting_id for hit in hits]


def test_keyword_terms_drop_stopwords_and_operators() -> None:
    assert keyword_terms("Data engineer with Spark & (Airflow") == "data | engineer | spark | airflow"
    assert keyword_terms("y de con") is None  # nothing left: keyword search is skipped


def test_posting_found_by_both_searches_ranks_first(clean_db: Engine) -> None:
    both = add(clean_db, "Data Engineer", "Spark and Airflow pipelines.")
    other = add(clean_db, "Backend Developer", "Django APIs and Postgres.")
    run_index(clean_db, EMBEDDER)

    hits = search(clean_db, "spark airflow pipelines", EMBEDDER)

    assert hits[0].posting_id == both
    assert hits[0].keyword_rank == 1 and hits[0].vector_rank == 1
    # The other posting matches no keyword, but every posting has *some* vector distance: it still
    # appears, from the vector list only, below.
    assert other in ids(hits) and hits[-1].keyword_rank is None


def test_filters_apply_inside_both_searches(clean_db: Engine) -> None:
    us_only = add(clean_db, "Spark Engineer", "Spark Spark Spark pipelines.", latam_hiring="no")
    latam = add(clean_db, "Data Engineer", "Some Spark work.", latam_hiring="yes")
    junior = add(clean_db, "Junior Data Engineer", "Spark pipelines.", seniority="junior")
    hybrid = add(clean_db, "Data Engineer Hybrid", "Spark pipelines.", remote="hybrid")
    run_index(clean_db, EMBEDDER)

    found = ids(search(clean_db, "spark pipelines", EMBEDDER, filters=SearchFilters(latam=True)))
    assert us_only not in found and latam in found  # the best keyword match, filtered out

    found = ids(search(clean_db, "spark pipelines", EMBEDDER, filters=SearchFilters(seniority=["senior"])))
    assert junior not in found

    # remote_modes: the board says remote, the text says hybrid, so it counts as both.
    remote = ids(search(clean_db, "spark", EMBEDDER, filters=SearchFilters(remote="remote")))
    only_hybrid = ids(search(clean_db, "spark", EMBEDDER, filters=SearchFilters(remote="hybrid")))
    assert hybrid in remote and only_hybrid == [hybrid]


def test_one_result_per_posting_and_per_job_group(clean_db: Engine) -> None:
    long_text = "\n".join(f"Spark pipelines, part {i}, with Airflow and dbt and Python." for i in range(60))
    many_chunks = add(clean_db, "Data Engineer", long_text)
    canonical = add(clean_db, "Analytics Engineer", "Spark and dbt models.", source=Source.HIMALAYAS)
    copy = add(clean_db, "Analytics Engineer", "Spark and dbt models.", source=Source.JOBICY)
    with clean_db.begin() as conn:
        conn.execute(update(Posting).where(Posting.id == copy).values(canonical_id=canonical))
    run_index(clean_db, EMBEDDER)

    found = ids(search(clean_db, "spark pipelines airflow dbt", EMBEDDER))

    assert found.count(many_chunks) == 1  # several matching chunks, one result
    assert canonical in found and copy not in found  # the group's representative only


def test_inactive_postings_are_not_returned(clean_db: Engine) -> None:
    closed = add(clean_db, "Data Engineer", "Spark pipelines.")
    run_index(clean_db, EMBEDDER)
    with clean_db.begin() as conn:
        conn.execute(update(Posting).where(Posting.id == closed).values(is_active=False))
        assert conn.execute(select(Posting.id)).scalars().all() == [closed]  # chunks kept, posting closed

    assert search(clean_db, "spark pipelines", EMBEDDER) == []


class ReverseReranker:
    """Scores results in the reverse of the order it receives them, to make reordering visible."""

    model = "fake-reranker"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        self.seen = list(texts)
        return [float(i) for i in range(len(texts))]


def test_reranker_reorders_the_fused_results(clean_db: Engine) -> None:
    add(clean_db, "Data Engineer", "Spark and Airflow pipelines.")
    add(clean_db, "Spark Developer", "Spark streaming.")
    add(clean_db, "Backend Developer", "Django APIs.")
    run_index(clean_db, EMBEDDER)
    fused = ids(search(clean_db, "spark airflow", EMBEDDER))
    reranker = ReverseReranker()

    reranked = search(clean_db, "spark airflow", EMBEDDER, reranker=reranker)

    assert ids(reranked) == list(reversed(fused))
    assert all(len(text) <= 600 for text in reranker.seen)  # truncated input (RERANK_MAX_CHARS)
    assert reranked[0].rerank_score is not None

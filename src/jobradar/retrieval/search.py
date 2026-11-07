"""Hybrid search: keyword search and vector search over posting chunks, fused with RRF, then reranked.

    search()   the entry point; the CLI calls it, and so will the Phase 3 agent (as a tool)

How a query runs (one SQL statement, then optional reranking in Python):
1. **eligible:** the postings allowed by the filters: active, one representative per job group
   (enriched_postings is built on current_postings), and the CLI filters (remote, seniority, role,
   LATAM). Filters live here, *inside* both searches: filtering after the merge could leave almost
   nothing of each top list.
2. **keyword:** chunks whose keyword index matches any of the query's words (accents ignored,
   decision D5), ranked by ts_rank, which rewards matching more of the words and matching them more
   often. Each posting counts once, with its best chunk.
3. **vector:** chunks nearest to the query's embedding by cosine distance (pgvector's `<=>`),
   again one best chunk per posting.
4. **fusion (Reciprocal Rank Fusion):** each posting scores 1/(K + rank) in each list it appears
   in, summed. Ranks, not raw scores, because ts_rank and cosine distance are on different scales.
   K = 60 (from the paper that introduced RRF) keeps a single #1 from dominating: 1/61 vs 1/62.
5. **rerank:** a cross-encoder re-scores the fused top results against the query (rerank.py).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text

from jobradar.retrieval.embed import Embedder
from jobradar.retrieval.rerank import Reranker

RRF_K = 60
LIST_DEPTH = 20  # postings taken from each of the keyword and vector lists before fusion

# What the reranker reads of each result's best chunk. A cross-encoder reads query and text
# together, so its cost grows with the text's length: on this Mac, 20 full chunks (~1,700
# characters) take ~18 s, the first 600 characters ~6 s. Those keep the header (title, seniority,
# role, remote, LATAM, skills) and the start of the chunk, the most informative part.
RERANK_MAX_CHARS = 600

# Words too common to say anything about a posting. The 'simple' text-search configuration has no
# stopword list (that's what lets it work for both languages), so the query side drops them: with
# "any word" matching, "and" or "de" would otherwise match nearly every posting.
STOPWORDS = frozenset(
    "a an and are as at be by for from in is of on or the to with we you our your "
    "de del el la las los y o en con para por un una que se al lo como".split()
)

_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class SearchFilters:
    remote: str | None = None  # remote, hybrid or onsite: matched against remote_modes
    seniority: Sequence[str] = ()  # any of these normalized levels
    roles: Sequence[str] = ()  # any of these role families
    latam: bool = False  # only latam_hiring = yes


@dataclass
class SearchHit:
    posting_id: int
    group_id: int
    source: str
    url: str
    title: str | None
    company: str | None
    latam_hiring: str | None
    remote_modes: list[str]
    salary: str | None
    summary: str | None
    best_chunk: str
    keyword_rank: int | None  # position in the keyword list (1 = best), None if absent
    vector_rank: int | None
    rrf_score: float
    rerank_score: float | None = None


def keyword_terms(query: str) -> str | None:
    """The tsquery text for "any of these words": `python | spark | airflow`.

    Words come from \\w+ only, so nothing the user types can break the tsquery syntax (to_tsquery
    raises on stray operators like an unbalanced parenthesis). None if no word is left.
    """
    words = [w for w in _WORD.findall(query.lower()) if w not in STOPWORDS]
    return " | ".join(dict.fromkeys(words)) or None


_SEARCH_SQL = text("""
    WITH eligible AS (
        SELECT id FROM enriched_postings
        WHERE (CAST(:remote AS text) IS NULL OR :remote = ANY(remote_modes))
          AND (cardinality(CAST(:seniority AS text[])) = 0 OR seniority_level = ANY(:seniority))
          AND (cardinality(CAST(:roles AS text[])) = 0 OR role_family = ANY(:roles))
          AND (NOT :latam OR latam_hiring = 'yes')
    ),
    keyword AS (
        SELECT posting_id, row_number() OVER (ORDER BY score DESC, posting_id) AS rank
        FROM (
            SELECT c.posting_id, max(ts_rank(c.tsv, q.query)) AS score
            FROM posting_chunks c
            JOIN eligible e ON e.id = c.posting_id
            CROSS JOIN (SELECT to_tsquery('simple', unaccent(:terms)) AS query) q
            WHERE CAST(:terms AS text) IS NOT NULL AND c.tsv @@ q.query
            GROUP BY c.posting_id
            ORDER BY score DESC, c.posting_id
            LIMIT :depth
        ) best
    ),
    vector AS (
        SELECT posting_id, row_number() OVER (ORDER BY distance, posting_id) AS rank
        FROM (
            SELECT c.posting_id, min(c.embedding <=> CAST(:vector AS vector)) AS distance
            FROM posting_chunks c
            JOIN eligible e ON e.id = c.posting_id
            GROUP BY c.posting_id
            ORDER BY distance, c.posting_id
            LIMIT :depth
        ) best
    ),
    fused AS (
        -- FULL OUTER JOIN keeps postings found by only one of the two searches; COALESCE gives
        -- them 0 points from the other.
        SELECT COALESCE(k.posting_id, v.posting_id) AS posting_id, k.rank AS keyword_rank,
               v.rank AS vector_rank,
               COALESCE(1.0 / (:rrf_k + k.rank), 0) + COALESCE(1.0 / (:rrf_k + v.rank), 0) AS rrf
        FROM keyword k
        FULL OUTER JOIN vector v ON v.posting_id = k.posting_id
        ORDER BY rrf DESC, posting_id
        LIMIT :candidates
    )
    SELECT f.*, ep.group_id, ep.source, ep.url, ep.resolved_title AS title,
           ep.resolved_company AS company, ep.latam_hiring, ep.remote_modes, ep.summary,
           ep.resolved_salary_min AS salary_min, ep.resolved_salary_max AS salary_max,
           ep.resolved_salary_currency AS salary_currency, ep.resolved_salary_period AS salary_period,
           -- The posting's chunk nearest to the query: shown, and what the reranker reads.
           (SELECT c.text FROM posting_chunks c WHERE c.posting_id = f.posting_id
            ORDER BY c.embedding <=> CAST(:vector AS vector) LIMIT 1) AS best_chunk
    FROM fused f
    JOIN enriched_postings ep ON ep.id = f.posting_id
    ORDER BY f.rrf DESC, f.posting_id
""")


def _salary(row: Any) -> str | None:
    low, high = row.salary_min, row.salary_max
    if low is None and high is None:
        return None
    amount = (
        f"{low}–{high}" if low is not None and high is not None else str(low if low is not None else high)
    )
    unit = " ".join(v for v in (row.salary_currency, row.salary_period) if v)
    return f"{amount} {unit}".strip()


def search(
    engine: Engine,
    query: str,
    embedder: Embedder,
    *,
    filters: SearchFilters | None = None,
    limit: int = 10,
    reranker: Reranker | None = None,
    candidates: int = LIST_DEPTH,
) -> list[SearchHit]:
    """The best `limit` postings for `query`: hybrid search, RRF, then reranking if a reranker is given.

    `candidates` postings leave the fusion step; the reranker re-orders all of them, so it can lift
    a posting from, say, 15th to 1st.
    """
    filters = filters or SearchFilters()
    params = {
        "remote": filters.remote,
        "seniority": list(filters.seniority),
        "roles": list(filters.roles),
        "latam": filters.latam,
        "terms": keyword_terms(query),
        "vector": str(embedder.embed_query(query)),  # pgvector's text form: "[0.1, 0.2, …]"
        "depth": LIST_DEPTH,
        "rrf_k": RRF_K,
        "candidates": max(candidates, limit),
    }
    with engine.connect() as conn:
        rows = conn.execute(_SEARCH_SQL, params).all()
    hits = [
        SearchHit(
            posting_id=row.posting_id,
            group_id=row.group_id,
            source=row.source,
            url=row.url,
            title=row.title,
            company=row.company,
            latam_hiring=row.latam_hiring,
            remote_modes=list(row.remote_modes),
            salary=_salary(row),
            summary=row.summary,
            best_chunk=row.best_chunk,
            keyword_rank=row.keyword_rank,
            vector_rank=row.vector_rank,
            rrf_score=float(row.rrf),
        )
        for row in rows
    ]
    if reranker is not None and hits:
        scores = reranker.score(query, [hit.best_chunk[:RERANK_MAX_CHARS] for hit in hits])
        for hit, score in zip(hits, scores, strict=True):
            hit.rerank_score = score
        hits.sort(key=lambda hit: hit.rerank_score or 0.0, reverse=True)
    return hits[:limit]

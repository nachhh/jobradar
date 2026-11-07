"""The index pipeline: chunks, keyword index and embeddings for every active posting that needs them.

    run_index()    a run: find postings whose chunks are missing or out of date, rebuild them
    find_stale()   the to-do list, with each posting's header and fingerprint

Concepts:
- **Fingerprint.** A hash of everything a posting's chunks depend on: the embedding model, the
  chunker version, the posting's content and its header (which comes from the enrichment). Any
  change gives a different fingerprint, and that posting is re-indexed; nothing else is. Same idea
  as enrichment's staleness, with no API cost: re-indexing only spends local CPU time.
- **All active postings are indexed**, not only each job group's representative: the representative
  can change (when a canonical closes), and search de-duplicates at query time through
  current_postings instead.
- **One transaction per batch:** a batch's old chunks are deleted and the new ones inserted together,
  so search never sees a posting with half its chunks. An interrupted run resumes by itself: the
  postings it didn't reach still have old fingerprints.
"""

import hashlib
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, delete, func, insert, text

from jobradar.ledger import advisory_lock, finish_run, lock_id, start_run
from jobradar.models import Pipeline, PostingChunk, RunStatus, Trigger
from jobradar.retrieval.chunk import CHUNKER_VERSION, build_header, split_chunks
from jobradar.retrieval.embed import Embedder

logger = logging.getLogger(__name__)

LOCK_ID = lock_id("jobradar.index")
BATCH_SIZE = 32  # postings per transaction (~100 chunks)

# Active postings with what the header needs from their (successful) enrichment, and the
# fingerprint their current chunks were built with (NULL when they have none). Source values come
# first, the LLM's fill the gaps, as in the enriched_postings view.
_CANDIDATES = text("""
    SELECT p.id, p.description, p.content_hash,
           COALESCE(p.title, e.title) AS title,
           COALESCE(p.company, e.company) AS company,
           p.remote AS board_remote, e.remote AS llm_remote,
           e.seniority AS seniority_level, e.role_family, e.latam_hiring, e.skills_required,
           (SELECT min(c.index_fingerprint) FROM posting_chunks c WHERE c.posting_id = p.id)
               AS stored_fingerprint
    FROM postings p
    LEFT JOIN posting_enrichments e ON e.posting_id = p.id AND e.status = 'ok'
    WHERE p.is_active
    ORDER BY p.id
""")


class NothingToIndexError(Exception):
    """Every active posting's chunks are up to date."""


@dataclass(frozen=True)
class StalePosting:
    id: int
    title: str | None
    description: str
    header: str
    fingerprint: str


@dataclass(frozen=True)
class IndexResult:
    run_id: uuid.UUID
    status: RunStatus
    postings: int
    chunks: int


def remote_modes(board: str | None, llm: str | None) -> list[str]:
    """Same rule as the enriched_postings view (migration 0007): both values when they disagree."""
    said = [m for m in (board, llm) if m not in (None, "unknown")]
    return list(dict.fromkeys(said)) or ["unknown"]  # dict.fromkeys drops a repeat, keeps order


def fingerprint(embedding_model: str, content_hash: str, header: str) -> str:
    material = "\x1f".join((embedding_model, CHUNKER_VERSION, content_hash, header))  # \x1f: separator
    return hashlib.sha256(material.encode()).hexdigest()


def find_stale(engine: Engine, embedding_model: str, limit: int | None = None) -> list[StalePosting]:
    with engine.connect() as conn:
        rows = conn.execute(_CANDIDATES).mappings().all()
    stale: list[StalePosting] = []
    for row in rows:
        posting: Mapping[str, Any] = dict(row) | {
            "remote_modes": remote_modes(row["board_remote"], row["llm_remote"])
        }
        header = build_header(posting)
        current = fingerprint(embedding_model, row["content_hash"], header)
        if row["stored_fingerprint"] != current:
            stale.append(StalePosting(row["id"], row["title"], row["description"] or "", header, current))
            if limit is not None and len(stale) >= limit:
                break
    return stale


def run_index(
    engine: Engine, embedder: Embedder, *, limit: int | None = None, trigger: Trigger = Trigger.MANUAL
) -> IndexResult:
    """Rebuild the chunks of every stale active posting (at most `limit`)."""
    with advisory_lock(engine, LOCK_ID, "index"):
        stale = find_stale(engine, embedder.model, limit)
        if not stale:
            raise NothingToIndexError("every active posting is indexed and up to date")
        run_id = start_run(engine, Pipeline.INDEX, trigger, {"stale": len(stale), "model": embedder.model})
        logger.info("index run %s: %d postings, %s", run_id, len(stale), embedder.model)
        chunks = 0
        try:
            for start in range(0, len(stale), BATCH_SIZE):
                chunks += _index_batch(engine, embedder, run_id, stale[start : start + BATCH_SIZE])
                done = min(start + BATCH_SIZE, len(stale))
                logger.info("indexed %d/%d postings (%d chunks)", done, len(stale), chunks)
        except Exception:
            finish_run(engine, run_id, RunStatus.FAILED)
            raise
        finish_run(engine, run_id, RunStatus.SUCCEEDED)
        return IndexResult(run_id, RunStatus.SUCCEEDED, len(stale), chunks)


def _index_batch(engine: Engine, embedder: Embedder, run_id: uuid.UUID, batch: Sequence[StalePosting]) -> int:
    """Chunk and embed a batch (outside any transaction: embedding is the slow part), then swap
    the batch's chunks in one short transaction."""
    rows: list[dict[str, Any]] = []
    for posting in batch:
        for index, body in enumerate(split_chunks(posting.description, embedder.count_tokens)):
            rows.append(
                {
                    "posting_id": posting.id,
                    "chunk_index": index,
                    "text": f"{posting.header}\n{body}".rstrip(),
                    "title": posting.title,
                    "fingerprint": posting.fingerprint,
                }
            )
    vectors = embedder.embed_documents([(row["title"], row["text"]) for row in rows])

    with engine.begin() as conn:
        conn.execute(delete(PostingChunk).where(PostingChunk.posting_id.in_([p.id for p in batch])))
        # insert().values(list) builds one multi-row INSERT … VALUES (…), (…): unlike executemany
        # (a list passed as parameters), it lets each row hold a SQL expression, here to_tsvector().
        conn.execute(
            insert(PostingChunk).values(
                [
                    {
                        "posting_id": row["posting_id"],
                        "chunk_index": row["chunk_index"],
                        "text": row["text"],
                        # Same expression keyword search uses on queries: accents stripped, no
                        # stemming (decision D5). Computed by Postgres from the inserted text.
                        "tsv": func.to_tsvector("simple", func.unaccent(row["text"])),
                        "embedding": vector,
                        "embedding_model": embedder.model,
                        "index_fingerprint": row["fingerprint"],
                        "last_run_id": run_id,
                    }
                    for row, vector in zip(rows, vectors, strict=True)
                ]
            )
        )
    return len(rows)

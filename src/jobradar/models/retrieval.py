"""Retrieval: postings split into chunks, each with a keyword index and an embedding.

See specs/phase-2-enrichment-retrieval.md, "Part B: indexing" and "Part C: search".
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, ForeignKey, Identity, Index, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from jobradar.db import Base
from jobradar.models._shared import TIMESTAMPTZ

# Fixed by the embedding model (google/embeddinggemma-300m outputs 768 numbers per text). pgvector
# rejects vectors of any other length, and vectors from different models can't be compared anyway,
# so changing the model means a migration and a full re-index.
EMBEDDING_DIM = 768


class PostingChunk(Base):
    """A piece of a posting's description, as embedded: header + chunk text.

    Search runs over chunks (keyword and vector) and then collapses the hits back to postings.
    A posting's chunks are replaced together, in one transaction, when its index_fingerprint changes.
    """

    __tablename__ = "posting_chunks"
    __table_args__ = (
        UniqueConstraint("posting_id", "chunk_index"),
        # GIN ("generalized inverted index") maps each word to the rows containing it: the classic
        # structure behind full-text search, so `tsv @@ query` doesn't read every row.
        Index(None, "tsv", postgresql_using="gin"),
        Index(None, "last_run_id"),
        # No index on `embedding`: with ~8,000 vectors an exact scan takes milliseconds and never
        # misses a neighbour. An HNSW index (approximate, much faster at scale) can come later.
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    posting_id: Mapped[int] = mapped_column(ForeignKey("postings.id", ondelete="CASCADE"))
    chunk_index: Mapped[int] = mapped_column(Integer)  # 0, 1, 2… in reading order
    text: Mapped[str] = mapped_column(Text)  # exactly what was embedded, header included
    # tsvector = the text as a sorted list of normalized words with positions, ready for keyword
    # matching. Written by the insert with to_tsvector('simple', unaccent(text)): unaccent isn't
    # declared IMMUTABLE, which a generated column would require.
    tsv: Mapped[str] = mapped_column(TSVECTOR)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str] = mapped_column(Text)
    # Hash of everything the chunks depend on (posting content, enrichment, model, chunker version).
    # A different fingerprint means the chunks are out of date.
    index_fingerprint: Mapped[str] = mapped_column(Text)
    last_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_runs.run_id"))
    indexed_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

"""Embedding models: text in, a vector of EMBEDDING_DIM numbers out.

Concepts:
- An *embedding* places a text on a "map of meaning": texts that mean similar things get vectors
  pointing in similar directions, even with no words in common ("ETL pipelines" near "data
  engineering"). Search compares directions with cosine distance (pgvector's `<=>`).
- Unlike the chat LLM, the embedding model never writes text. It runs locally (decision D2):
  free, unlimited, and fast enough on a CPU for ~8,000 chunks.
- Queries and documents are embedded *with different prefixes*. embeddinggemma was trained that
  way ("task: search result | query: …" vs "title: … | text: …"), so leaving them out puts both
  kinds of vector in slightly different regions of the map and search gets worse. fastembed only
  documents these prefixes; the code below adds them.

Embedder is the interface; FastEmbedder is the real model, FakeEmbedder a deterministic stand-in
for tests that still behaves like an embedding (shared words → similar vectors).
"""

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Any, Protocol

from jobradar.models import EMBEDDING_DIM

MODEL = "google/embeddinggemma-300m"

# The model's prompt formats, from its model card (and fastembed's description of it).
QUERY_PREFIX = "task: search result | query: "
DOCUMENT_FORMAT = "title: {title} | text: {text}"


class Embedder(Protocol):
    model: str  # stored in posting_chunks.embedding_model

    def count_tokens(self, text: str) -> int:
        """Tokens the model would read for `text` (chunk sizes are measured in these)."""
        ...

    def embed_documents(self, documents: Sequence[tuple[str | None, str]]) -> list[list[float]]:
        """Vectors for (title, text) pairs, in order."""
        ...

    def embed_query(self, query: str) -> list[float]: ...


class FastEmbedder:
    """embeddinggemma-300m through fastembed: ONNX Runtime, no PyTorch (Intel-Mac constraint).

    The model (~1.2 GB) downloads from Hugging Face on first use into `cache_dir`, and loads lazily:
    commands that never embed (e.g. `jobradar runs`) don't pay the few seconds it takes.
    """

    model = MODEL

    def __init__(self, cache_dir: str | None = None, batch_size: int = 16) -> None:
        self._cache_dir = cache_dir
        self._batch_size = batch_size  # texts per ONNX call: bigger is faster, up to memory limits
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding  # imported here: loading it pulls in ONNX Runtime

            self._model = TextEmbedding(self.model, cache_dir=self._cache_dir)
        return self._model

    def count_tokens(self, text: str) -> int:
        # The model's own tokenizer (a Hugging Face `tokenizers` object): the exact count the model
        # sees, not a characters/4 guess. Special tokens (start/end markers) are left out.
        tokenizer = self._load().model.tokenizer
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    def embed_documents(self, documents: Sequence[tuple[str | None, str]]) -> list[list[float]]:
        texts = [DOCUMENT_FORMAT.format(title=title or "none", text=text) for title, text in documents]
        # embed() yields numpy arrays; tolist() turns each into plain floats for pgvector.
        return [vector.tolist() for vector in self._load().embed(texts, batch_size=self._batch_size)]

    def embed_query(self, query: str) -> list[float]:
        return next(iter(self._load().embed([QUERY_PREFIX + query]))).tolist()


_WORD = re.compile(r"\w+")


class FakeEmbedder:
    """A "hashing embedder" for tests: each word adds 1 to a dimension chosen by hashing it.

    Texts sharing words get similar vectors, so search tests can check ranking without the real
    model (which would mean a 1.2 GB download in CI). It's also a classic baseline: before neural
    embeddings, "feature hashing" was a common way to turn text into fixed-size vectors.
    """

    model = "fake-hashing"

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIM
        for word in _WORD.findall(text.lower()):
            # sha256 rather than hash(): Python randomizes hash() per process, and tests need the
            # same vector on every run.
            vector[int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % EMBEDDING_DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_documents(self, documents: Sequence[tuple[str | None, str]]) -> list[list[float]]:
        return [self._vector(text) for _title, text in documents]

    def embed_query(self, query: str) -> list[float]:
        return self._vector(query)

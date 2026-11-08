"""Reranking: a slower, more precise model re-orders the top results of hybrid search.

Concepts:
- The embedding model is a *bi-encoder*: query and chunk become vectors separately, and search
  compares the vectors. Fast (chunk vectors are stored), but the two texts never "see" each other.
- A *cross-encoder* reads (query, text) together as one input and outputs a single relevance score.
  It can tell "5 years of Python required" from "Python nice to have" for a Python query, which two
  separately made vectors blur. Nothing can be precomputed, so it only scores the top ~20.

Decision D3: BAAI/bge-reranker-v2-m3 (int8 ONNX), the multilingual cross-encoder fastembed offers
with a permissive license. The mini-eval (step s7) showed no gain: every variant scored
precision@5 = 1.00 on realistic queries. So it stays available but off by default (`--rerank`), until
Phase 4's harder golden set can tell whether it earns its ~6 s.
"""

from collections.abc import Sequence
from typing import Any, Protocol

MODEL = "BAAI/bge-reranker-v2-m3-int8"


class Reranker(Protocol):
    model: str

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        """A relevance score per text (higher is better), in the order given."""
        ...


class FastReranker:
    """The cross-encoder through fastembed (~0.6 GB, downloaded on first use, loaded lazily)."""

    model = MODEL

    def __init__(self, cache_dir: str | None = None) -> None:
        self._cache_dir = cache_dir
        self._model: Any = None

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if self._model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self._model = TextCrossEncoder(self.model, cache_dir=self._cache_dir)
        return [float(s) for s in self._model.rerank(query, list(texts))]

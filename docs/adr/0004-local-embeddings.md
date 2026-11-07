# ADR-0004: Local embeddings with fastembed and embeddinggemma

- **Status:** Accepted
- **Date:** 2026-11-07

## Context

Search (Phase 2) and resume matching (Phase 3) need embeddings: vectors that place texts with similar meaning close together, so a query can find postings that share no words with it. The corpus is about 2,700 postings (about 8,000 chunks), re-indexed whenever a posting or its enrichment changes. The postings mix languages: most GetOnBoard postings are in Spanish, the other boards mostly English. The machine is an Intel Mac without a GPU, where PyTorch no longer ships new builds.

## Decision

- **Embed locally, not through an API:** free, unlimited, no data leaves the machine, and re-indexing everything after a chunker change costs only CPU time.
- **`google/embeddinggemma-300m`** (768 dimensions, multilingual, reads up to 2,048 tokens). In a spot test on 60 real postings it ranked best of the multilingual models that run here, including a Spanish query finding English postings.
- **fastembed** to run it: ONNX Runtime instead of PyTorch, a small dependency, models downloaded on first use (to `.cache/fastembed`, git-ignored).
- **`onnxruntime<1.24` on Intel Macs only** (an environment marker in `pyproject.toml`): 1.23.2 is the last release with Intel-macOS wheels. Other platforms get current releases. The project also allows only Python 3.12, so that uv can resolve one lockfile for all of them.
- **The model's prompt prefixes are added in our code** (`task: search result | query: …` for queries, `title: … | text: …` for documents): the model was trained with them, and fastembed documents but doesn't apply them.
- **Chunks with a contextual header** built from the enrichment, stored in `posting_chunks` with their vector (pgvector) and keyword index (`tsvector`). A fingerprint per posting (model, chunker version, content, header) decides what to re-index.
- **No vector index for now:** an exact scan over ~8,000 vectors takes milliseconds and never misses a neighbour.

## Consequences

- Indexing everything takes about half an hour of CPU; daily updates about a minute. Search embeds each query locally in well under a second once the model is loaded (loading takes a few seconds).
- The vector column has 768 dimensions: changing the model means a migration and a full re-index, since vectors from different models can't be compared. The fingerprint includes the model name, so the re-index happens by itself.
- Tests use a deterministic hashing embedder; one test runs the real model and is skipped by default (`uv run pytest -m model`), since it downloads 1.2 GB.
- The Intel-Mac pin will need revisiting if the project moves to another machine or Python version.

## Alternatives considered

- **`paraphrase-multilingual-MiniLM-L12-v2`:** 13 times faster, but it reads only 128 tokens and silently drops the rest; it also ranked worse in the spot test. Rejected.
- **Larger models (`multilingual-e5-large`, `Qwen3-Embedding-0.6B`):** 1–2 GB more and several times slower on this CPU, for gains this corpus doesn't need yet.
- **An embeddings API (Gemini, Voyage, OpenAI):** good quality, but costs or rate limits on every re-index, and the corpus would leave the machine. Rejected for now.
- **sentence-transformers (PyTorch):** the reference library, but PyTorch has no current Intel-macOS builds. Rejected.
- **An HNSW index:** approximate nearest-neighbour search, the answer at ~100k+ vectors. Not needed at this size.

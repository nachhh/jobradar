"""Retrieval (Phase 2, parts B and C): finding the postings that match a query.

- chunk.py: split postings into chunks with a context header.
- embed.py: the embedding model (local embeddinggemma, or a fake for tests).
- indexer.py: the `index` pipeline, which keeps posting_chunks up to date.
"""

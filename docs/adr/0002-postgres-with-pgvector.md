# ADR-0002: Postgres with pgvector as the only data store

- **Status:** Accepted
- **Date:** 2026-11-01

## Context

The project stores relational data (postings, pipeline runs, LLM enrichments), text for full-text search, vector embeddings for semantic search, and later the agent's checkpoints. The volume is small: thousands to tens of thousands of postings.

## Decision

- **One Postgres 17 database with the pgvector extension** holds everything: relational tables, full-text indexes (`tsvector`), embeddings (`vector` columns) and LangGraph checkpoints.
- Run locally with the `pgvector/pgvector` Docker image.
- Schema changes go through **Alembic** migrations, never by hand.

## Consequences

- Hybrid search (keywords plus vectors) is a single SQL query that can also filter and join on relational columns.
- One service to run, back up and reason about. Transactions cover data and embeddings together.
- Fewer specialized features than a dedicated vector database, which does not matter at this size. An HNSW index can be added when needed.

## Alternatives considered

- **Qdrant, Weaviate or Milvus:** strong vector databases, but a second store to keep in sync with Postgres. Rejected at this size; worth knowing about.
- **Chroma or FAISS files:** fine for notebooks, but no SQL, transactions or joins. Rejected.
- **A managed vector service (Pinecone):** paid beyond the free tier, and data leaves the machine. Rejected.

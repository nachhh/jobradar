# JobRadar: project overview

## Problem

Job postings for data and AI roles are spread across several boards, written in inconsistent formats, and filtered by keyword-matching systems. Finding the postings that fit a given profile, and understanding *why* they fit or not, is slow manual work.

## What JobRadar does

1. **Ingests** job postings on a schedule from free public sources into Postgres, through raw → staging → clean layers, with idempotent loads, deduplication and data quality checks.
2. **Enriches** each posting with an LLM: structured fields (skills, seniority, remote policy, "hires from LATAM?", salary) validated against a schema.
3. **Indexes** postings for retrieval: local embeddings in pgvector, full-text search, hybrid ranking and a reranker.
4. **Answers questions** over the postings and a candidate's resume with RAG ("which roles fit me, and why?").
5. **Runs an agent** (LangGraph) that scores fit for a posting, finds skill gaps, drafts tailored resume bullets, critiques its own output, and pauses for human approval.
6. **Measures itself**: eval sets, tracing, token and cost tracking.
7. **Exposes** everything through a REST API, an MCP server and a minimal UI.

## Goals

- A working, end-to-end tool the author actually uses in their own job search.
- Hands-on coverage of the current LLM-application stack, built the way a data engineer would build it: data contracts, idempotency, tests, observability.
- Every phase leaves the repository in a state that can be shown.

## Non-goals

- Production scale, multi-tenancy, authentication.
- Scraping sites whose terms forbid it (LinkedIn included). Only public APIs and feeds.
- Applying to jobs automatically.

## Constraints

- **Cost:** free tiers and open source first. Total spend target under US$20.
- **Hardware:** Intel MacBook Pro, 16 GB RAM, no usable GPU. Local LLMs are too slow for the main flow; local embedding models are fine. PyTorch no longer ships new Intel-macOS builds, so embedding libraries are chosen with that in mind (verified in Phase 2).
- **Time:** about two to three weeks in total.

## Architecture (target)

```
 Job sources (public APIs)
        │  Phase 1: extract, load raw JSON, transform
        ▼
 ┌──────────────── Postgres + pgvector ────────────────┐
 │ raw_postings → stg_postings → postings              │
 │ pipeline_runs (run ledger)                          │
 │ posting_enrichment (LLM fields)    Phase 2          │
 │ posting_chunks (text + embedding)  Phase 2          │
 │ agent checkpoints                  Phase 3          │
 └─────────────────────────────────────────────────────┘
        │
        ▼
 Retrieval: full-text + vector, fused, reranked   (Phase 2)
        │
        ▼
 LangGraph agent: fit → gaps → bullets → critique → human approval   (Phase 3)
        │
        ├── Langfuse traces, eval sets, cost tracking   (Phase 4)
        ▼
 FastAPI · MCP server · Streamlit UI   (Phase 5)
```

## Phases

Each phase starts with its own spec in `specs/`, approved before code is written, and ends with a "done" check.

| # | Phase | Main concepts | Done when |
|---|---|---|---|
| 0 | Foundation | uv, Docker Compose, Postgres + pgvector, Alembic, config and secrets, Ruff, pyright, pytest, pre-commit, CI | `make check` passes locally and in CI; the health command reports Postgres and pgvector |
| 1 | Ingestion | ELT layers, run ledger, idempotent upserts, deduplication, data quality checks, scheduling | Two or more sources load repeatedly with no duplicates; quality checks run on every load |
| 2 | Enrichment and retrieval | Structured LLM outputs, provider abstraction, embeddings, chunking, hybrid search, reranking | Postings carry validated LLM fields; a search query returns relevant postings with scores |
| 3 | Agent | LangGraph state, nodes and edges, conditional routing, tool calling, self-critique, human-in-the-loop, checkpointing | The agent produces a fit report and tailored bullets for a posting and waits for approval |
| 4 | Evals and observability | Golden datasets, retrieval and answer metrics, LLM-as-judge, tracing (Langfuse), cost tracking, caching | An eval run produces a report; every LLM call is traced with tokens and cost |
| 5 | Exposure | FastAPI, MCP server, Streamlit UI, architecture docs | The agent can be used from the UI and from an MCP client |
| 6 | Optional: batch analytics | PySpark (local mode), optionally Snowflake and dbt | A Spark job computes skill trends over all postings |

## Candidate sources (verified in Phase 1)

- GetOnBoard public API (LATAM tech jobs).
- Hacker News "Who is hiring?" monthly threads, through the Algolia HN API.
- Remotive public API (remote jobs).
- Others with free, documented APIs if those fall short.

## Decisions

Architecture decisions are recorded in [docs/adr/](../docs/adr/).

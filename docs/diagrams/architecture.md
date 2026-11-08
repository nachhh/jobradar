# Architecture

What runs where: ingestion (Phase 1), LLM enrichment, indexing and search (Phase 2). The agent comes next, on top of the same Postgres database (see the [project overview](../../specs/00-overview.md)).

```mermaid
flowchart LR
    subgraph apis["Public job APIs"]
        direction TB
        GOB["GetOnBoard<br/>JSON:API, paged"]
        HN["Hacker News<br/>Algolia API"]
        REM["Remotive<br/>1 request per run"]
        HIM["Himalayas<br/>search API, Bearer key"]
        JOB["Jobicy<br/>cursor paging"]
    end

    CRON(["cron<br/>08:00 and 20:00"]) --> CLI
    YOU(["you"]) --> CLI
    CLI["CLI (Typer)<br/>ingest · transform --rebuild · enrich · index · search · link · runs"] --> PIPE
    CLI --> ENRICH
    CLI --> INDEXER
    CLI --> SEARCH

    subgraph app["Python: jobradar.ingest"]
        direction TB
        PIPE["pipeline.py<br/>run_ingest · resume_ingest · rebuild<br/>advisory lock, fetch intervals, checks"]
        S1["Stage 1: extract-load<br/>Fetcher (httpx: retries, backoff, polite interval)<br/>extract() per source → load_raw (COPY)"]
        S2["Stage 2: transform<br/>parse() per source → PostingIn (Pydantic)<br/>upsert, quarantine, closed postings"]
        S3["Stage 3: link duplicates (link.py)<br/>keys from the source, else the enrichment<br/>blocking + pg_trgm similarity + union-find"]
        PIPE --> S1 --> S2 --> S3
    end

    apis -- "HTTPS / JSON" --> S1

    subgraph enrich["Python: jobradar.enrich + jobradar.llm"]
        direction TB
        ENRICH["enrich/pipeline.py<br/>stale postings → threads + rate limiter<br/>validate (Pydantic), grounding check"]
        PROVIDER["llm/: provider interface<br/>Claude (default) · Gemini · fake"]
        ENRICH --> PROVIDER
    end

    subgraph llms["LLM APIs"]
        direction TB
        CLAUDE["Claude Haiku 5.5<br/>structured output, prompt cache"]
        GEMINI["Gemini (free tier)"]
    end
    PROVIDER -- "HTTPS / JSON schema" --> llms

    subgraph retrieval["Python: jobradar.retrieval (local, no API)"]
        direction TB
        INDEXER["indexer.py<br/>stale postings (fingerprint) → chunks + header"]
        EMB["embeddinggemma-300m<br/>fastembed / ONNX Runtime, CPU"]
        SEARCH["search.py<br/>keyword + vector, filters, RRF"]
        RERANK["bge-reranker-v2-m3 (int8)<br/>cross-encoder, top 20, opt-in"]
        INDEXER --> EMB
        SEARCH -- "embed the query" --> EMB
        SEARCH -. "opt-in" .-> RERANK
    end

    subgraph pg["Postgres 17 + pgvector + pg_trgm (Docker)"]
        direction TB
        LEDGER[("pipeline_runs<br/>pipeline_run_sources")]
        RAW[("raw_postings")]
        CLEAN[("postings<br/>rejected_postings")]
        VIEW[["view: current_postings"]]
        ENR[("posting_enrichments<br/>llm_calls")]
        CHUNKS[("posting_chunks<br/>tsvector + vector(768)")]
        EVIEW[["view: enriched_postings"]]
        CLEAN --> VIEW
        VIEW --> EVIEW
        ENR --> EVIEW
    end

    PIPE -- "run ledger, lock" --> LEDGER
    S1 -- "one row per version" --> RAW
    RAW -- "rows seen in this run" --> S2
    S2 --> CLEAN
    S3 -- "canonical_id" --> CLEAN
    CLEAN -- "stale active postings" --> ENRICH
    ENRICH -- "one transaction per posting" --> ENR
    ENRICH -- "relink when done" --> S3
    CLEAN -- "active postings" --> INDEXER
    ENR -- "header fields" --> INDEXER
    INDEXER -- "one transaction per batch" --> CHUNKS
    CHUNKS -- "one SQL query" --> SEARCH
    EVIEW -- "filters, display fields" --> SEARCH
    SEARCH -. "Phase 3: a tool of the agent" .-> NEXT(["next phase"])
```

## Reading it

- **One process per run.** The CLI calls plain functions in `pipeline.py`; cron is just another caller. An orchestrator (Dagster, later) can call the same functions.
- **The lock** is a Postgres advisory lock held for the whole run, so a second run started meanwhile exits at once.
- **Each stage of each source is its own transaction**, so a failure or a crash undoes only the stage it hit (see [state machines](state-machines.md)).
- **Enrichment runs separately** (`jobradar enrich`), with its own advisory lock, so it can run alongside an ingest. It only reads what ingestion wrote and never changes `postings` ([ADR-0003](../adr/0003-llm-provider-and-structured-outputs.md)).
- **Indexing runs locally** (`jobradar index`): the embedding model runs on the CPU, so re-indexing costs time, not money ([ADR-0004](../adr/0004-local-embeddings.md)).
- **Everything lives in one database** ([ADR-0002](../adr/0002-postgres-with-pgvector.md)): the ledger, the raw and clean layers, and later the embeddings.

Code: [cli.py](../../src/jobradar/cli.py), [pipeline.py](../../src/jobradar/ingest/pipeline.py), [enrich/](../../src/jobradar/enrich/), [llm/](../../src/jobradar/llm/), [ledger.py](../../src/jobradar/ledger.py), [retrieval/](../../src/jobradar/retrieval/), [http.py](../../src/jobradar/ingest/http.py), [sources/](../../src/jobradar/ingest/sources/), [transform.py](../../src/jobradar/ingest/transform.py), [link.py](../../src/jobradar/ingest/link.py).

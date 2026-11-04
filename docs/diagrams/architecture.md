# Architecture

What runs where, as built in Phase 1. Later phases add LLM enrichment, retrieval and the agent on top of the same Postgres database (see the [project overview](../../specs/00-overview.md)).

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
    CLI["CLI (Typer)<br/>ingest · transform --rebuild · runs"] --> PIPE

    subgraph app["Python: jobradar.ingest"]
        direction TB
        PIPE["pipeline.py<br/>run_ingest · resume_ingest · rebuild<br/>advisory lock, fetch intervals, checks"]
        S1["Stage 1: extract-load<br/>Fetcher (httpx: retries, backoff, polite interval)<br/>extract() per source → load_raw (COPY)"]
        S2["Stage 2: transform<br/>parse() per source → PostingIn (Pydantic)<br/>upsert, quarantine, closed postings"]
        S3["Stage 3: link duplicates<br/>blocking + pg_trgm similarity + union-find"]
        PIPE --> S1 --> S2 --> S3
    end

    apis -- "HTTPS / JSON" --> S1

    subgraph pg["Postgres 17 + pgvector + pg_trgm (Docker)"]
        direction TB
        LEDGER[("pipeline_runs<br/>pipeline_run_sources")]
        RAW[("raw_postings")]
        CLEAN[("postings<br/>rejected_postings")]
        VIEW[["view: current_postings"]]
        CLEAN --> VIEW
    end

    PIPE -- "run ledger, lock" --> LEDGER
    S1 -- "one row per version" --> RAW
    RAW -- "rows seen in this run" --> S2
    S2 --> CLEAN
    S3 -- "canonical_id" --> CLEAN
    VIEW -. "Phase 2: enrichment, embeddings, search" .-> NEXT(["next phases"])
```

## Reading it

- **One process per run.** The CLI calls plain functions in `pipeline.py`; cron is just another caller. An orchestrator (Dagster, later) can call the same functions.
- **The lock** is a Postgres advisory lock held for the whole run, so a second run started meanwhile exits at once.
- **Each stage of each source is its own transaction**, so a failure or a crash undoes only the stage it hit (see [state machines](state-machines.md)).
- **Everything lives in one database** ([ADR-0002](../adr/0002-postgres-with-pgvector.md)): the ledger, the raw and clean layers, and later the embeddings.

Code: [cli.py](../../src/jobradar/cli.py), [pipeline.py](../../src/jobradar/ingest/pipeline.py), [http.py](../../src/jobradar/ingest/http.py), [sources/](../../src/jobradar/ingest/sources/), [transform.py](../../src/jobradar/ingest/transform.py), [link.py](../../src/jobradar/ingest/link.py).

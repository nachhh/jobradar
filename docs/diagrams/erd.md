# Entity-relationship diagram

The schema after migration `0007`: ingestion (Phase 1) plus enrichment and retrieval (Phase 2). Types are Postgres types, with brackets and parentheses spelled out because Mermaid doesn't accept them in type names: `text_array` is `text[]`, `vector_768` is `vector(768)`, `numeric_10_6` is `numeric(10,6)`.

```mermaid
erDiagram
    PIPELINE_RUNS ||--o{ PIPELINE_RUN_SOURCES : "one row per source"
    PIPELINE_RUNS ||--o{ RAW_POSTINGS : "first_run_id, last_run_id"
    PIPELINE_RUNS ||--o{ POSTINGS : "last_run_id"
    PIPELINE_RUNS ||--o{ REJECTED_POSTINGS : "run_id"
    POSTINGS |o--o{ POSTINGS : "canonical_id (duplicates point at their canonical)"
    RAW_POSTINGS }o..|| POSTINGS : "same source + src_posting_id (no FK)"
    POSTINGS ||--o| POSTING_ENRICHMENTS : "posting_id (latest enrichment)"
    POSTINGS ||--o{ POSTING_CHUNKS : "posting_id"
    POSTINGS |o--o{ LLM_CALLS : "posting_id (SET NULL)"
    PIPELINE_RUNS ||--o{ LLM_CALLS : "run_id"
    PIPELINE_RUNS ||--o{ POSTING_ENRICHMENTS : "last_run_id"
    PIPELINE_RUNS ||--o{ POSTING_CHUNKS : "last_run_id"

    PIPELINE_RUNS {
        uuid run_id PK "gen_random_uuid()"
        text pipeline "ingest, rebuild, enrich, index"
        text trigger "manual or scheduled"
        jsonb params "sources, skipped, force"
        text git_sha "code that ran"
        text status "running, succeeded, partial, failed"
        timestamptz started_at
        timestamptz finished_at
    }

    PIPELINE_RUN_SOURCES {
        uuid run_id PK, FK
        text source PK
        text status "running, succeeded, failed"
        text stage "NULL, extracted, transformed"
        int fetched
        int inserted
        int updated
        int unchanged
        int rejected
        text error_type
        text error_message
        timestamptz started_at
        timestamptz finished_at
    }

    RAW_POSTINGS {
        bigint id PK
        text source UK "unique with src_posting_id, payload_hash"
        text src_posting_id UK
        jsonb payload "exactly as received"
        text payload_hash UK "volatile fields ignored"
        uuid first_run_id FK "run that first saw this version"
        timestamptz first_fetched_at
        uuid last_run_id FK "last run that saw it: the seen stamp"
        timestamptz last_fetched_at
    }

    POSTINGS {
        bigint id PK
        text source UK "natural key with src_posting_id"
        text src_posting_id UK
        text url
        text title "NULL for most HN postings"
        text company "NULL for most HN postings"
        text location_text
        text remote "remote, hybrid, onsite, unknown"
        text seniority
        int salary_min
        int salary_max
        text salary_currency "ISO code"
        text salary_period "hour, day, week, month, year"
        text salary_text "free-text salaries"
        timestamptz posted_at
        text description "plain text"
        text_array tags
        text content_hash "hash of the parsed fields"
        timestamptz first_seen_at
        timestamptz last_seen_at
        uuid last_run_id FK
        boolean is_active
        text dedup_key "normalized company|title"
        bigint canonical_id FK "NULL: canonical or unlinked"
    }

    REJECTED_POSTINGS {
        bigint id PK
        uuid run_id FK
        text source
        text src_posting_id
        jsonb payload
        text reason "validation error"
        timestamptz rejected_at
    }

    POSTING_ENRICHMENTS {
        bigint posting_id PK, FK
        text source_content_hash "postings.content_hash it was made from"
        text prompt_version
        text provider "claude, gemini"
        text model
        text status "ok, failed"
        int attempts "failed attempts; skipped after 3"
        text error
        text company
        text title
        text seniority "intern … principal, unknown"
        text role_family "backend, data_engineering, ai_training, …"
        text employment_type
        text remote
        text latam_hiring "yes, no, unknown"
        text latam_evidence "quoted from the posting"
        text_array location_restrictions
        text_array skills_required
        text_array skills_nice
        int years_experience_min
        text_array languages
        int salary_min
        int salary_max
        text salary_currency
        text salary_period
        text salary_basis "gross, net, unknown"
        text summary
        text dedup_key "for linking HN postings"
        uuid last_run_id FK
        timestamptz enriched_at
    }

    LLM_CALLS {
        bigint id PK
        uuid run_id FK
        bigint posting_id FK
        text purpose "enrich"
        text provider
        text model
        text prompt_version
        text status "ok, invalid_output, ungrounded, refused, error"
        int input_tokens
        int output_tokens
        int cache_write_tokens "prompt cache, billed apart"
        int cache_read_tokens
        numeric_10_6 cost_usd
        int latency_ms
        text error
        jsonb response "raw answer, for debugging"
        timestamptz created_at
    }

    POSTING_CHUNKS {
        bigint id PK
        bigint posting_id FK, UK "unique with chunk_index"
        int chunk_index UK
        text text "header + chunk, as embedded"
        tsvector tsv "simple + unaccent, GIN index"
        vector_768 embedding "no vector index (exact scan)"
        text embedding_model
        text index_fingerprint "changes, then re-index"
        uuid last_run_id FK
        timestamptz indexed_at
    }
```

## Notes

- **Two identities.** Within a source, `(source, src_posting_id)` is the natural key. Across sources, `canonical_id` groups postings of the same job (entity resolution); rows are never merged or deleted.
- **`raw_postings` ↔ `postings` has no foreign key on purpose.** Raw holds every version and lands first; `postings` holds the current parse. They join on `(source, src_posting_id)`.
- **Run totals are not stored** on `pipeline_runs`: they're `SUM`s over `pipeline_run_sources` (one copy of each number).
- **Allowed values** (sources, statuses, stages, remote, salary period) are `TEXT` columns with `CHECK` constraints, not Postgres `ENUM` types.
- **View `current_postings`** (not drawn): `postings` columns plus `group_id`, one row per job group, keeping the canonical if active, else the oldest active copy.
- **Source facts and LLM fields are kept apart.** `postings` holds what the sources said; `posting_enrichments` holds what the LLM extracted. The view **`enriched_postings`** (not drawn) joins `current_postings` to successful enrichments and adds `resolved_*` columns (the source's value when it has one, otherwise the LLM's) and `remote_modes` (both values when the board's modality and the posting's text disagree).
- **Run counts for enrich and index aren't stored:** they are `SUM`s over `llm_calls.run_id` and counts by `last_run_id`.

Code: [models/](../../src/jobradar/models/), [migrations/versions/](../../migrations/versions/).

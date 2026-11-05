# Entity-relationship diagram

The ingestion schema after migration `0004`. Types are Postgres types (`text_array` is `text[]`, written that way because Mermaid doesn't accept brackets in type names).

```mermaid
erDiagram
    PIPELINE_RUNS ||--o{ PIPELINE_RUN_SOURCES : "one row per source"
    PIPELINE_RUNS ||--o{ RAW_POSTINGS : "first_run_id, last_run_id"
    PIPELINE_RUNS ||--o{ POSTINGS : "last_run_id"
    PIPELINE_RUNS ||--o{ REJECTED_POSTINGS : "run_id"
    POSTINGS |o--o{ POSTINGS : "canonical_id (duplicates point at their canonical)"
    RAW_POSTINGS }o..|| POSTINGS : "same source + src_posting_id (no FK)"

    PIPELINE_RUNS {
        uuid run_id PK "gen_random_uuid()"
        text pipeline "ingest or rebuild"
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
        text title "NULL for HN until Phase 2"
        text company "NULL for HN until Phase 2"
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
```

## Notes

- **Two identities.** Within a source, `(source, src_posting_id)` is the natural key. Across sources, `canonical_id` groups postings of the same job (entity resolution); rows are never merged or deleted.
- **`raw_postings` ↔ `postings` has no foreign key on purpose.** Raw holds every version and lands first; `postings` holds the current parse. They join on `(source, src_posting_id)`.
- **Run totals are not stored** on `pipeline_runs`: they're `SUM`s over `pipeline_run_sources` (one copy of each number).
- **Allowed values** (sources, statuses, stages, remote, salary period) are `TEXT` columns with `CHECK` constraints, not Postgres `ENUM` types.
- **View `current_postings`** (not drawn): `postings` columns plus `group_id`, one row per job group, keeping the canonical if active, else the oldest active copy.

Code: [models/](../../src/jobradar/models/), [migrations/versions/](../../migrations/versions/).

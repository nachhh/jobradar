# Phase 1: Ingestion

- **Status:** Accepted (2026-11-02)

## Goal

Job postings from three public sources land in Postgres on every run, through raw → clean layers, with no duplicates, no partial loads, and a ledger that says what each run did. Re-running is always safe.

## Sources

Verified on 2026-11-02. All three return a stable ID per posting.

| Source | Endpoint | Source ID | Notes |
|---|---|---|---|
| GetOnBoard | `GET /api/v0/categories/{category}/jobs?page=…` | `id` (a slug, e.g. `data-engineer-acme-remote-9cff`) | LATAM. Paginated. Whole categories, not keyword searches: `programming`, `data-science-analytics`, `machine-learning-ai`, `sysadmin-devops-qa`. Structured `seniority`, `min_salary`, `max_salary`, `modality`. HTML in description fields. |
| Remotive | `GET /api/remote-jobs?category=…` | `id` (integer) | Terms: link back to the posting URL and credit Remotive; **at most 4 requests a day**; jobs are delayed 24 h. |
| Hacker News | Algolia: the last two "Ask HN: Who is hiring?" stories by `whoishiring`, then their top-level comments | comment `objectID` (integer) | Free text, usually `Company \| Role \| Location \| REMOTE \| …` on the first line. Company and title can be missing; Phase 2's LLM extraction fills the gaps. Deleted comments are skipped. |

Ingestion is deliberately independent of any resume: it builds one broad corpus, and matching against a resume happens later (Phases 2 and 3), so the same corpus serves any profile.

## Identity

Two different questions, solved in two different places:

1. **Record identity: "is this the same posting as last run?"** Within a source, the source's own ID decides: **`(source, src_posting_id)` is the natural key** of a posting. It is stable across edits, unlike company, title or date, which change when a posting is edited or reposted.
2. **Entity identity: "is this the same job posted on two boards?"** That is entity resolution, handled in the clean layer by linking, never by deleting (see Cross-source duplicates).

## Data model

```
pipeline_runs          one row per run
pipeline_run_sources   one row per (run, source): status, counts, error
raw_postings           payloads as received, one row per distinct version of a posting
postings               clean, one row per (source, src_posting_id), upserted
rejected_postings      records that failed validation, with the reason (quarantine)
```

- **`pipeline_runs`:** `run_id` (UUID), `pipeline` (`ingest`), `trigger` (`manual` or `scheduled`), `params` (JSONB: sources, queries), `git_sha`, `status` (`running`, `succeeded`, `partial`, `failed`), `started_at`, `finished_at`.
- **`pipeline_run_sources`:** primary key `(run_id, source)`. `status`, `stage` reached (`extracted`, `transformed`), `started_at`, `finished_at`, counts (`fetched`, `inserted`, `updated`, `unchanged`, `rejected`), `error_type`, `error_message`. Run totals are not stored on `pipeline_runs`; they are a `SUM` over this table (no duplicated data).
- **`raw_postings`:** `id`, `source`, `src_posting_id`, `payload` (JSONB, exactly as received), `payload_hash`, `first_run_id`, `first_fetched_at`, `last_run_id`, `last_fetched_at`. Unique on `(source, src_posting_id, payload_hash)`: an unchanged posting fetched again is not stored again, so the table holds one row per *version* of each posting. That history makes it possible to rebuild `postings` from raw at any time, for example after fixing a parser bug, without calling the APIs again.
  - The payload columns never change. `last_run_id` and `last_fetched_at` are refreshed every time the API returns that version again: they record that the posting was *seen* in a run. Without them, an unchanged posting would leave no trace of the run, and the transform could not tell "unchanged and still listed" from "closed" (both add no new version).
- **`postings`:** `id`, `source`, `src_posting_id` (unique together), `url`, `title`, `company`, `location_text`, `remote` (`remote`, `hybrid`, `onsite` or `unknown`), `seniority`, `salary_min`, `salary_max`, `salary_text`, `posted_at`, `description` (plain text, HTML stripped), `tags`, `content_hash`, `first_seen_at`, `last_seen_at`, `last_run_id`, `is_active`, `dedup_key`, `canonical_id` (nullable self-reference).
- **Closed postings:** GetOnBoard and Remotive drop a posting from their API when it closes. After a successful extract of a source, its postings not seen in that run get `is_active = false`. HN has no closed signal, so HN postings become inactive when their thread is more than two months old.
- **`rejected_postings`:** `run_id`, `source`, `src_posting_id`, `payload`, `reason`.

LLM call metrics (model, tokens, cost, latency) are not part of ingestion: they get their own `llm_calls` table in Phase 2, one row per call, linked to its run.

## Pipeline: extract-load, then transform (ELT)

Two stages with a durable hand-off between them: what was fetched is committed to `raw_postings` before any parsing happens. A parser bug or a failed transform never costs another API call.

**Stage 1, extract-load (per source, one transaction each):**
1. Fetch every page with `httpx`: timeouts, retries with exponential backoff on 429 and 5xx, a descriptive user agent, and a delay between requests. Within the batch, duplicates by `(source, src_posting_id)` are dropped in memory (pagination can overlap).
2. `COPY` the payloads into a temporary staging table (fast bulk load), then insert into `raw_postings` with `ON CONFLICT (source, src_posting_id, payload_hash) DO UPDATE SET last_run_id, last_fetched_at` (COPY itself cannot handle conflicts). A new version is a new row; a version seen before only gets its "last seen" stamp.
3. Record `fetched` and stage `extracted` in `pipeline_run_sources`, and commit. A crash before the commit leaves nothing behind for that source.

**Stage 2, transform (per source, one transaction each):**
1. Read the source's raw rows seen in this run (`last_run_id` = this run, which includes unchanged postings), and parse each with a per-source parser into one shared Pydantic model, `PostingIn`. Records that fail validation go to `rejected_postings` with the reason.
2. Upsert into `postings`: `INSERT … ON CONFLICT (source, src_posting_id) DO UPDATE … WHERE postings.content_hash IS DISTINCT FROM excluded.content_hash`. New rows count as inserted, changed rows as updated; for unchanged rows only `last_seen_at` moves.
3. Update `is_active`, record the counts and stage `transformed`, and commit.

**Stage 3, link duplicates** across sources (see Cross-source duplicates).

**Checks:** a source that returns 0 records, or rejects more than 20% of them, is marked `failed` (an API change is the likely cause). The run is `succeeded` if every source succeeded, `partial` if some did, and `failed` if none did.

**Recovery:**
- Loads are idempotent, so re-running everything is always safe and gives the same result. That is the guarantee.
- As an optimization, `jobradar ingest --resume <run_id>` skips the sources that run already finished and re-transforms sources that reached `extracted` without fetching them again.
- `jobradar transform --rebuild` rebuilds `postings` from the whole of `raw_postings`, for example after a parser fix.

**Concurrency:** a run takes a Postgres advisory lock with `pg_try_advisory_lock`. This is pessimistic locking (claim first, then work) that fails fast: a second run started while one is active exits with a message instead of waiting. The unique constraints would already prevent duplicate rows if two runs overlapped; the lock prevents duplicated work and double API calls (Remotive's daily limit).

**Remotive limit:** the Remotive source is skipped if it last succeeded less than 6 hours ago, unless `--force` is given.

## Cross-source duplicates

- **Normalization:** company and title are lowercased, with accents and punctuation removed and legal suffixes ("S.A.", "Inc.", "LLC", "Ltda.") dropped. `dedup_key` = normalized company plus normalized title.
- **Exact match:** postings from different sources with the same `dedup_key` are linked.
- **Fuzzy match** with `pg_trgm` trigram similarity, because company names vary across sources too:
  - **Blocking:** only pairs from different sources whose normalized companies share their first token are compared, which keeps comparisons few and avoids linking unrelated companies with similar titles.
  - **Rule:** company similarity ≥ 0.7 and title similarity ≥ 0.8. Thresholds are tuned on real data.
- **Linking:** the oldest posting in a group becomes canonical, and the others get its `canonical_id`. Rows are never deleted or merged, so a wrong link can be undone. Search and the agent read canonical, active postings only.
- Postings without a company (some HN comments) are not linked in Phase 1. Phase 2's LLM extraction fills in the company, and embeddings can add a semantic signal to matching later.

## CLI

- `jobradar ingest [--source getonboard|remotive|hn] [--force] [--resume RUN_ID]`
- `jobradar transform --rebuild`
- `jobradar runs [--limit N]`: the most recent runs, with per-source status and counts.
- Each stage is a plain Python function that the CLI calls, so an orchestrator can wrap the same functions later without changing them. Until then, scheduling is a documented `crontab` line (twice a day).

## Tests

- Parsers: recorded sample responses from each source saved as fixtures (HTML, missing fields, odd HN formats).
- HTTP: mocked with `respx`, including 429 and 500 followed by success.
- Integration, against `jobradar_test`:
  - loading the same batch twice leaves the counts unchanged and the second run reports everything as `unchanged`;
  - a changed payload updates the posting and adds a raw version;
  - an exception in the middle of a load leaves no rows behind for that source;
  - a failed transform keeps the raw rows, and `--resume` finishes the run without fetching again;
  - a posting missing from a successful extract becomes inactive;
  - a second concurrent run is refused;
  - exact and fuzzy cross-source links are created, and none are created across different companies.

## Out of scope

LLM extraction and embeddings (Phase 2), search, a UI, any paid source, LinkedIn.

## Acceptance criteria

1. `jobradar ingest` loads all three sources, and `jobradar runs` shows the run with its counts.
2. Running it again immediately inserts nothing new and reports the postings as unchanged.
3. Killing a run mid-load leaves the database as it was before that source started.
4. `make check` passes, including the integration tests above, locally and in CI.
5. At least one cross-source duplicate is linked on real data, or the report explains why none exist.

## Decisions

- Raw storage: one row per version of a posting (2026-11-02).
- Raw rows carry `last_run_id`/`last_fetched_at`, refreshed on every fetch, so each run records which postings it saw; preferred over a separate append-only sightings table for simplicity (2026-11-03).
- HN: the last two monthly threads (2026-11-02).
- GetOnBoard: whole categories instead of keyword searches (2026-11-02).
- **Open:** whether to add an orchestrator, and which one (see the project overview).

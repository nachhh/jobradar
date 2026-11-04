# Phase 1: Ingestion

- **Status:** Accepted (2026-11-02)

## Goal

Job postings from public sources (three at first, five after step s7) land in Postgres on every run, through raw → clean layers, with no duplicates, no partial loads, and a ledger that says what each run did. Re-running is always safe.

## Sources

Verified on 2026-11-02 (Himalayas and Jobicy on 2026-11-04). All return a stable ID per posting. Sources marked *(s7)* are added in step s7; other candidates were checked and rejected on 2026-11-04 (no official API or feed, or terms that forbid scraping).

| Source | Endpoint | Source ID | Notes |
|---|---|---|---|
| GetOnBoard | `GET /api/v0/categories/{category}/jobs?page=…&per_page=100&expand[]=company&expand[]=seniority&expand[]=tags&expand[]=modality` (`expand[]` inlines related objects, which otherwise come back as bare IDs) | `id` (a slug, e.g. `data-engineer-acme-remote-9cff`) | LATAM. Paginated. Whole categories, not keyword searches: `programming`, `data-science-analytics`, `machine-learning-ai`, `sysadmin-devops-qa`. Structured `seniority`, `min_salary`, `max_salary`, `modality`. HTML in description fields. |
| Remotive | `GET /api/remote-jobs` (all categories in **one** request, no paging) | `id` (integer) | Terms: link back to the posting URL and credit Remotive; **at most 4 requests a day**; jobs are delayed 24 h. Test fixtures are hand-written from the docs, never recorded with a live request. **The free API returns only 10 jobs in total** (verified 2026-11-04: `"total-job-count": 10`); the full list is a paid API. |
| Himalayas *(s7)* | `GET /jobs/api/search` (filters: `country`, `seniority`, `q`, `page`), with a free API key | `guid` (the job URL) | Remote jobs worldwide (114,633 on 2026-11-04). Each job lists the countries it is open to (`locationRestrictions`), structured salary (`minSalary`, `maxSalary`, `currency`, `salaryPeriod`), `seniority` and `categories`. Free key: 60 requests/min, 10,000/day; data refreshes daily. Terms: credit Himalayas, link back, and apply through `applicationLink`. |
| Jobicy *(s7)* | `GET /api/v2/remote-jobs` (filters: `industry` such as `engineering`/`data-science`, `geo`, `count` up to 100, `cursor`) | `id` (integer) | Remote jobs with structured `salaryMin`/`salaryMax`, `jobLevel`, `jobGeo`. Poll at most once an hour. Terms: credit Jobicy; apply links go to Jobicy. |
| Hacker News | Algolia: `search_by_date?tags=story,author_whoishiring` for the last two "Ask HN: Who is hiring?" stories, then `items/{story_id}` for each, whose top-level comments are the postings (replies dropped) | comment `objectID` (integer) | Free text, usually `Company \| Role \| Location \| REMOTE \| …` on the first line. Company and title can be missing; Phase 2's LLM extraction fills the gaps. Deleted comments are skipped. |

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
  - `payload_hash` ignores *volatile* fields that change without the posting changing (GetOnBoard's `applications_count` and the company's `response_time_in_days`); otherwise nearly every run would add a version. The stored payload still keeps them.
  - The payload columns never change. `last_run_id` and `last_fetched_at` are refreshed every time the API returns that version again: they record that the posting was *seen* in a run. Without them, an unchanged posting would leave no trace of the run, and the transform could not tell "unchanged and still listed" from "closed" (both add no new version).
- **`postings`:** `id`, `source`, `src_posting_id` (unique together), `url`, `title`, `company`, `location_text`, `remote` (`remote`, `hybrid`, `onsite` or `unknown`), `seniority`, `salary_min`, `salary_max`, `salary_currency` (ISO code), `salary_period` (`hour`, `day`, `week`, `month`, `year`; added in s7, since sources give salaries in different units and amounts are stored as given, without conversion), `salary_text`, `posted_at`, `description` (plain text, HTML stripped), `tags`, `content_hash`, `first_seen_at`, `last_seen_at`, `last_run_id`, `is_active`, `dedup_key`, `canonical_id` (nullable self-reference).
- **Closed postings:** GetOnBoard and Remotive drop a posting from their API when it closes. After a successful extract of a source, its postings not seen in that run get `is_active = false`. HN has no explicit closed signal, so an HN posting becomes inactive when **(a)** its comment is more than 45 days old (`created_at`), **or (b)** its thread was fetched successfully in this run and the comment was not in it (the comment was deleted, usually because the role was filled). Rule (b) applies only to threads fetched in the run: a comment whose thread has left the last-two window is out of range, not missing. A per-comment age gives every posting the same lifetime, while a thread-age or "not seen" rule would give 1 to 2 months depending on when in the month the comment was posted.
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
3. Stamp every posting returned by this run's extract (`last_seen_at`, `last_run_id`, `is_active = true`), including postings whose new version was rejected: they are still listed, so their previous clean row stays active. Then deactivate per the closed-postings rules, record the counts and stage `transformed`, and commit.

**Stage 3, link duplicates** across sources (see Cross-source duplicates).

**Checks:** a source that returns 0 records, or rejects more than 20% of them, is marked `failed` (an API change is the likely cause). A source returning 0 records fails before its transform, so "not seen → inactive" can't close everything. When the reject rate is exceeded, the rejections are kept as evidence but `postings` is not touched; `--resume` re-transforms after the parser is fixed. The run is `succeeded` if every source succeeded, `partial` if some did, and `failed` if none did.

**Recovery:**
- Loads are idempotent, so re-running everything is always safe and gives the same result. That is the guarantee.
- As an optimization, `jobradar ingest --resume <run_id>` skips the sources that run already finished and re-transforms sources that reached `extracted` without fetching them again. Only the latest ingest run can be resumed: a newer run has re-stamped the raw rows, so an older run would see an incomplete batch. Re-transforming a run replaces its earlier rejections instead of duplicating them.
- `jobradar transform --rebuild` rebuilds `postings` from the whole of `raw_postings`, for example after a parser fix.

**Concurrency:** a run takes a Postgres advisory lock with `pg_try_advisory_lock`. This is pessimistic locking (claim first, then work) that fails fast: a second run started while one is active exits with a message instead of waiting. The unique constraints would already prevent duplicate rows if two runs overlapped; the lock prevents duplicated work and double API calls (Remotive's daily limit).

**Fetch-frequency limits:** a source with a minimum interval is skipped if its last completed extract (stage `extracted` reached, whatever happened afterwards, since the requests were spent either way) finished less than that interval ago, unless `--force` is given. Remotive: 6 hours (4 requests a day). Himalayas: 20 hours (data refreshes daily; ~230 requests per fetch). Jobicy: 1 hour (its terms). If every requested source is skipped, no run is created.

## Cross-source duplicates

- **Normalization:** company and title are lowercased, with accents and punctuation removed and legal suffixes ("S.A.", "Inc.", "LLC", "Ltda.") dropped. `dedup_key` = normalized company plus normalized title.
- **Exact match:** postings from different sources with the same `dedup_key` are linked.
- **Fuzzy match** with `pg_trgm` trigram similarity, because company names vary across sources too:
  - **Blocking:** only pairs from different sources whose normalized companies share their first token are compared, which keeps comparisons few and avoids linking unrelated companies with similar titles.
  - **Rule:** company similarity ≥ 0.7 and title similarity ≥ 0.8. Thresholds are tuned on real data.
- **Linking:** matching is transitive (A~B and B~C put all three in one group; computed with union-find). The oldest posting in a new group becomes canonical, and the others get its `canonical_id`. If a new pair joins two existing groups, the older canonical wins and the other group's members move under it. Linking runs after every ingest, resume and rebuild, over all of `postings`; a failure is logged and retried next run, and doesn't fail the run. Rows are never deleted or merged, so a wrong link can be undone.
- **Canonical is identity, fixed; the representative is chosen at read time.** The canonical never changes, even after it closes, so links stay stable. No source is the truth about whether a job is still open (each board only knows its own copy), so a group counts as open while **any** member is active. A view, `current_postings`, returns one row per group: the canonical if it is active, otherwise the oldest active member (`DISTINCT ON (COALESCE(canonical_id, id))`). Search and the agent read that view.
- Postings without a company (some HN comments) are not linked in Phase 1. Phase 2's LLM extraction fills in the company, and embeddings can add a semantic signal to matching later.

## CLI

- `jobradar ingest [--source getonboard|remotive|hn] [--force] [--resume RUN_ID] [--trigger manual|scheduled]`; exits non-zero unless the run succeeded, so cron can notice.
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

## Acceptance report (2026-11-04)

Run against the local database with real data, on branch `jobradar-nachhh-p1-postings-ingestion`.

| # | Criterion | Result |
|---|---|---|
| 1 | `jobradar ingest` loads all three sources; `jobradar runs` shows the counts | ✅ Run `1451aadd`, started with the README's crontab command in a cron-like environment (empty environment, minimal `PATH`): GetOnBoard 602, Remotive 10, HN 436 fetched, all `succeeded`. First loads: 602 + 436 inserted (run `07e1dd15`), 10 inserted (run `995b1383`). |
| 2 | An immediate re-run inserts nothing and reports everything unchanged | ✅ Run `09a0961c`: 602 + 436 unchanged, 0 new raw versions; run `1451aadd`: all three sources, 1,048 unchanged. |
| 3 | Killing a run mid-load leaves the database as before | ✅ Run `12c24bd3` killed with `kill -9` during GetOnBoard: raw and clean row counts and `last_seen_at` unchanged; the ledger kept `running` for the run and the source, with no stage; the advisory lock was released by Postgres when the connection dropped. `ingest --resume` then finished the run (`succeeded`). |
| 4 | `make check` passes locally and in CI | ✅ locally (81 tests) and in CI (GitHub Actions run `41787386052` on `4fdab4a`, including the history-wide secret scan). |
| 5 | A cross-source duplicate linked on real data, or why none | ✅ After s7 (run `fdcc7102`): **21 links**, 20 exact and 1 fuzzy (Askable, "Software Engineer, AI Coding Tasks" on Remotive vs "… (Contract)" on Himalayas), all correct on inspection (Jobicy↔Himalayas and Remotive↔Himalayas). `current_postings` shows 2,688 jobs out of 2,709 postings. Before s7 there were no candidate pairs: HN postings have no company until Phase 2 (decision D1), and Remotive's 10 jobs shared no company with GetOnBoard. One known near-miss to revisit: "senior data engineer" vs "senior data engineer remote" scores 0.75, below the 0.8 title threshold. |

Data quality on real data: 0 records rejected out of 2,709 (all five sources). Himalayas kept 1,593 tech jobs of the 4,599 open to Argentina; 847 of the dropped ones have no category at all, worth sampling later in case some are tech.

## Steps

The order the phase is built in, one reviewable piece at a time. Steps are about building the code; they are not the pipeline's *stages* (extract-load, transform, link), which describe what every run does. Commits name their step: `[jobradar][p1][s<N>] <description>`.

| Step | Scope | Status |
|---|---|---|
| s1 | Schema: ORM models, migration `0002`, schema tests | Done (`d46666c`) |
| s2 | HTTP `Fetcher` (retries, backoff, polite interval) and the three extractors, with fixtures | Done (`bd98296`) |
| s3 | `PostingIn`, a parser per source, and the transform (upsert, quarantine, closed postings) | Done (`a04d28c`) |
| s4 | Pipeline: `COPY` load, run ledger, advisory lock, checks, `--resume`, `--force`, `transform --rebuild`, CLI | Done (`0751cfc`) |
| s5 | Cross-source linking (`pg_trgm`, union-find over candidate pairs) and the `current_postings` view | Done (`533d28c`) |
| s6 | Scheduling (documented `crontab` line) and the acceptance run on real data | Done (`4fdab4a`) |
| s7 | Two more sources, Himalayas and Jobicy: extractor, parser and fixtures each; migration `0004` (`source` CHECK constraints, `salary_currency`/`salary_period`), per-source fetch intervals, optional `HIMALAYAS_API_KEY` | Done |

Steps s1 to s3 were committed before this convention, as `[jobradar][p1] …`.

## Decisions

- Raw storage: one row per version of a posting (2026-11-02).
- Himalayas scope: jobs open to Argentina (`country=AR`, worldwide included), tech only by `parentCategories`, filtered at extract; at most once every 20 hours (2026-11-04).
- Jobicy scope: `engineering`, `data-science` and `qa-testing` with `geo=latam` (LATAM plus "Anywhere") (2026-11-04).
- Salaries stored as the source gives them, with `salary_currency` and `salary_period`; no conversion at ingestion (2026-11-04).
- Raw rows carry `last_run_id`/`last_fetched_at`, refreshed on every fetch, so each run records which postings it saw; preferred over a separate append-only sightings table for simplicity (2026-11-03).
- HN: the last two monthly threads (2026-11-02).
- HN inactivity: comment older than 45 days, or deleted from a thread fetched in the run (2026-11-04).
- GetOnBoard: whole categories instead of keyword searches (2026-11-02).
- Cross-source groups: fixed canonical, representative chosen at read time by the `current_postings` view (2026-11-03).
- Remotive: one request per run for all categories; fixtures hand-written to spare the daily limit (2026-11-03).
- **Open:** whether to add an orchestrator, and which one (see the project overview).

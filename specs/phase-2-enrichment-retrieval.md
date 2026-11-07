# Phase 2: Enrichment and retrieval

- **Status:** Accepted (2026-11-05).

## Goal

Every active posting carries structured fields extracted by an LLM and validated against a schema (skills, seniority, remote policy, "hires from LATAM?", salary), and `jobradar search "<query>"` returns the most relevant jobs with their scores, using keyword search plus vector search, fused and reranked. Like ingestion, both pipelines are safe to re-run: a second run does no work unless something changed.

The corpus stays broad (every posting of the ingested categories, including hybrid and on-site jobs): the tool serves the owner's own search, but also has to work for other profiles. Narrowing happens at search time with filters, not at ingestion.

## Key choices

Proposed in the draft and approved by the owner on 2026-11-05 (also listed under [Decisions](#decisions)).

| # | Choice | Why |
|---|---|---|
| D1 | **LLM provider:** Claude Haiku 5.5 (`claude-haiku-5-5`) by default; Gemini (free tier) as a second implementation behind the same interface | Haiku costs $0.10 / $0.50 per million input / output tokens. A **full pass** over ~2,700 postings (~3.5M input, ~0.8M output tokens, plus some thinking tokens) is **about $1**, paid from the owner's API credits. Full passes happen only on the first backfill and when `PROMPT_VERSION` changes; other runs enrich only new or edited postings (estimated 150–300 a day, about $0.06–0.12, to be measured once the cron has run a few days), and a run with nothing to do costs $0. Free tiers cap requests per day, which turns a backfill into several days of resumed runs. |
| D2 | **Local embedding model:** `google/embeddinggemma-300m` through `fastembed` (ONNX, no PyTorch) | The corpus mixes languages: 83% of GetOnBoard postings are in Spanish (97% of hybrid and on-site ones, 64% of remote ones), while Himalayas, Jobicy, Remotive and HN are mostly English. The model is multilingual, reads up to 2,048 tokens, has 768 dimensions, and gave the best results in a spot test on 60 real postings. ~5 chunks/s in the benchmark, ~2.3/s on real chunks: 53 min for the first full index, about a minute a day after that. The faster `paraphrase-multilingual-MiniLM-L12-v2` reads only **128 tokens** and silently drops the rest. |
| D3 | **Reranker:** `BAAI/bge-reranker-v2-m3-int8` over the top 20, kept only if the mini-eval shows it helps | It is the only multilingual reranker that fastembed offers with a license that allows this use. ~9 s for 20 candidates on this Mac, so it can be turned off per search (`--no-rerank`). The English-only `ms-marco-MiniLM-L-12-v2` takes ~2 s but can't read Spanish well. |
| D4 | **LLM fields in their own table,** `posting_enrichments`; `postings` stays exactly what the sources said | Lineage: we can always tell a fact the source gave from one a model inferred, and re-enrich without touching the clean layer. A view joins the two for search. |
| D5 | **Keyword search:** Postgres text search with the `simple` setting plus `unaccent` | The English setting doesn't stem Spanish. `simple` works for both languages without stemming ("engineers" doesn't match "engineer"), and vector search covers that gap. |
| D6 | **Mini retrieval eval:** 8 queries; the owner judges the pooled top 5 of each search variant (~80 yes/no answers, ~15 min) | Turns "we added RRF and a reranker" into a measured result (precision@5 per variant). The full eval framework is Phase 4. |

## Concepts in this phase

Introduced in the chat on 2026-11-04 (plain language first); **evals** get a short introduction before s3 (whose spot check is a small eval) and a fuller one, with examples, after the phase. Where each concept ends up:

| Concept | Where |
|---|---|
| LLM call, message roles, tokens, cost per call | `src/jobradar/llm/` (one adapter per provider), `llm_calls` table |
| Provider abstraction | `src/jobradar/llm/base.py` |
| Structured output (schema-constrained JSON, validated with Pydantic) | `src/jobradar/enrich/schema.py` |
| Grounding check (a cheap guard against hallucinated evidence) | `src/jobradar/enrich/grounding.py` |
| Prompt, prompt versioning | `src/jobradar/enrich/prompt.py` |
| Embeddings, cosine distance | `src/jobradar/retrieval/embed.py`, `posting_chunks.embedding` |
| Chunking, contextual chunk headers | `src/jobradar/retrieval/chunk.py` |
| Hybrid search, Reciprocal Rank Fusion | `src/jobradar/retrieval/search.py` |
| Reranking with a cross-encoder | `src/jobradar/retrieval/rerank.py` |
| Retrieval evaluation (precision@k) | `evals/` |

## Code layout

Few top-level packages, one per area of the system:

```
src/jobradar/
  cli.py  config.py  db.py
  models/      ORM tables, one module per area (split from models.py in s1)
    runs.py  postings.py  enrichment.py  retrieval.py
  ingest/      Phase 1
  llm/         provider interface and adapters (Claude, Gemini, fake)
  enrich/      schema, prompt, enrich pipeline
  retrieval/   chunk, embed, indexer (the `index` pipeline), search, rerank
```

`models/__init__.py` imports every module, so `from jobradar.models import Posting` keeps working and Alembic sees every table.

## Part A: enrichment

### Which postings

**Active** postings whose enrichment is missing or **stale**. Stale means the posting's `content_hash` differs from the one the enrichment was computed from, or `PROMPT_VERSION` changed. Every copy in a cross-source group is enriched, since copies can differ and the representative can change. Inactive postings are skipped; one that comes back becomes stale and is picked up.

### What is extracted (`EnrichmentOut`)

One Pydantic model, sent to the provider as the JSON schema and used to validate the answer.

| Field | Type | Notes |
|---|---|---|
| `company`, `title` | text or null | Mainly for HN, where the source has none (Phase 1 decision D1). |
| `seniority` | `intern`, `junior`, `mid`, `senior`, `lead`, `principal`, `unknown` | Normalized, so it can be filtered on. The source's own free-text value stays in `postings`. |
| `role_family` | `backend`, `frontend`, `fullstack`, `mobile`, `data_engineering`, `data_science`, `analytics`, `ml_ai_engineering`, `ai_training`, `devops_platform_cloud`, `qa`, `security`, `embedded`, `product`, `design`, `management`, `support`, `other` | Checked against the sources' own categories. `ai_training` is data-labelling and RLHF contract work (Himalayas tags 177 jobs `AI-Trainer`/`AI-Training`), a distinct kind of job worth filtering in or out. |
| `employment_type` | `full_time`, `part_time`, `contract`, `freelance`, `internship`, `unknown` | |
| `remote` | `remote`, `hybrid`, `onsite`, `unknown` | |
| `latam_hiring` | `yes`, `no`, `unknown` | `yes`: open to Argentina, LATAM, the Americas, or worldwide with no exclusion. `no`: restricted to regions that exclude LATAM (e.g. "US only"). Otherwise `unknown`. |
| `latam_evidence` | text or null | The sentence that supports `latam_hiring`, **quoted from the posting** (see grounding below). |
| `location_restrictions` | list of text | Countries or regions the job is limited to. |
| `skills_required`, `skills_nice` | lists of text | Normalized: lowercase, plus a small alias table (`k8s` → `kubernetes`, `postgres` → `postgresql`, `golang` → `go`). |
| `years_experience_min` | integer or null | |
| `languages` | list of text | Spoken languages required, e.g. `english`. |
| `salary_min`, `salary_max`, `salary_currency`, `salary_period` | as in `postings` | Only when stated in the text, never estimated. |
| `salary_basis` | `gross`, `net`, `unknown` | Before or after taxes. Chilean postings often quote "sueldo líquido" (net). Applies to the source's structured salary too, when the text says which it is. |
| `summary` | text | One sentence, used in search results. |

### Prompt and provider

- Chat APIs take messages with **roles**: `system` (standing instructions), `user` (the input) and `assistant` (the model's reply). The system message states the rules: extract only what the posting says, use `unknown` or null when it doesn't say, never estimate salaries, and the LATAM definition above. The user message holds the title, company, location, remote and tags fields, plus the description. The longest posting is about 3,300 tokens, so nothing is truncated.
- `PROMPT_VERSION` (a constant) changes whenever the prompt or the schema changes. That makes every enrichment stale, so changing it costs a full pass (about $1), and is done on purpose.
- **Provider interface** (`llm/base.py`): `extract(instructions, posting_text, schema) → (parsed, usage)`; the adapter turns `instructions` into the system message and `posting_text` into the user message. Implementations: Claude (`anthropic` SDK, `messages.parse` with the Pydantic model), Gemini (`google-genai`, `response_schema`), and a fake one for tests. `LLM_PROVIDER` picks one.
- Claude Haiku 5.5 thinks before answering by default; extraction doesn't need much, so `effort` is set to `low`. Current Claude models no longer accept `temperature`; the schema and low effort keep answers consistent.

### Validation and grounding

1. The provider constrains the answer to the schema; Pydantic validates it again (types, enums, `salary_min ≤ salary_max`).
2. **Grounding check:** `latam_evidence` must appear in the posting text (after normalizing whitespace and case). If it doesn't, the model made up its quote: `latam_hiring` becomes `unknown` and the call is logged as `ungrounded`. It's a cheap guard against hallucination on the field that matters most for this search.
3. Invalid output gets **one retry**, with the validation error appended to the prompt. If that fails too, the enrichment is stored with `status = failed`. After **3 failed attempts** a posting is skipped until its content or the prompt changes, so a hopeless posting doesn't cost money on every run.

### Source facts and LLM fields

The `enriched_postings` view returns `current_postings` joined to their enrichment. **The source wins when it has a value; the LLM fills the gaps** (`COALESCE(postings.company, enrichment.company)`, and the same for title and salary, which is resolved as a whole range). **Remote is the exception:** when the board's modality and the posting's own text disagree (27 GetOnBoard postings say `fully_remote` on the board and "híbrida" in the text), the posting counts as both: `remote_modes` holds both values, and filters test membership (`'remote' = ANY(remote_modes)`), so the job shows up in either search and the reader can ask the employer (migration `0007`). Filters use the enrichment's normalized enums (seniority, role family, LATAM), because source values are free text ("Semi Senior", "Sr.").

### HN and cross-source linking

Each enrichment stores a `dedup_key` built from its company and title with Phase 1's `dedup_key()`. Linking uses `COALESCE(postings.dedup_key, enrichment.dedup_key)`, so HN postings join the exact and fuzzy matching with the same rules (blocking, thresholds, union-find).

**Copies that contradict each other on hiring from LATAM are never grouped.** A US-only and a LATAM opening with the same company and title are different jobs for a candidate in LATAM. Union-find keeps each group's `latam_hiring` values and refuses a union that would put a "yes" and a "no" in one group (checking pairs alone isn't enough: an "unknown" copy matching both would join them transitively). "unknown" is compatible with both, since boards describe eligibility unevenly (the same job lists `{Brazil}` on one board and `{Argentina}` on another). Pairs are grouped exact-first, then by id, so the outcome is the same on every run; existing links are kept, as in Phase 1.

### Cost and rate controls

- A cost per call is computed from a price table in code and stored with the call.
- `jobradar enrich --dry-run` prints how many postings are stale and an estimated cost (characters / 4 ≈ tokens) without calling anything.
- `--limit N` caps the calls in a run; `ENRICH_MAX_CALLS` is the default cap (3,000).
- Calls run on a small thread pool (`ENRICH_CONCURRENCY`, default 4) behind a requests-per-minute limiter. A full backfill takes about 30 min. Threads rather than `asyncio`: with a handful of calls in flight they perform the same, and the rest of the code (SQLAlchemy, psycopg) is synchronous. Asyncio is learned in Phase 5, where FastAPI is built on it.

### Transactions and recovery

Each posting's enrichment and its `llm_calls` rows are committed together, one posting at a time. A crash loses at most the calls in flight. There is no `--resume`: staleness *is* the to-do list, so the next run continues exactly where the last one stopped.

## Part B: indexing

### What gets embedded

Each chunk is embedded with a short **contextual header**, so that a chunk about benefits still knows which job it belongs to:

```
Senior Data Engineer at Acme · senior · data_engineering · remote · LATAM: yes
Skills: python, spark, airflow
<chunk text>
```

The header comes from `enriched_postings`, so the index runs after enrichment. A posting whose enrichment failed gets a header from its source fields only.

### Chunking

The description is split on paragraphs, which are packed into chunks of at most ~300 tokens (counted with the embedding model's tokenizer). A paragraph that alone exceeds the limit is split on sentences. One paragraph overlaps between neighbouring chunks. The median posting (~800 tokens) gives about 3 chunks, and the whole corpus about 8,000. `CHUNKER_VERSION` is a constant, like `PROMPT_VERSION`.

### Embedding model

`google/embeddinggemma-300m` (D2) through `fastembed`, which runs ONNX models without PyTorch.

- **Intel-Mac constraint:** `onnxruntime` dropped Intel-macOS wheels after 1.23.2, so `pyproject.toml` pins `onnxruntime<1.24` (verified 2026-11-04 in a scratch environment).
- The model downloads from Hugging Face on first use (~1.2 GB) into `.cache/fastembed/` (git-ignored, set through `FASTEMBED_CACHE_PATH`).
- embeddinggemma expects different prefixes for queries and documents. Whether fastembed adds them is checked in s5, and if it doesn't, our code adds them.

### Staleness

Each chunk row stores an `index_fingerprint`: a hash of the posting's `content_hash`, the enrichment's version, the embedding model and `CHUNKER_VERSION`. A posting is re-indexed (its chunks deleted and rewritten in one transaction) when its fingerprint changes. Chunks of inactive postings are kept but excluded at query time, so a reactivated posting needs no re-embedding.

### Indexes

- A GIN index on the chunks' `tsvector`.
- **No vector index:** with ~8,000 vectors an exact scan takes milliseconds and never misses a neighbour. HNSW is the answer at around 100k+ vectors (ADR-0002 anticipated this).

## Part C: search

`jobradar search "<query>" [--remote …] [--seniority …] [--role …] [--latam] [--limit 10] [--no-rerank] [--explain]`

1. **Embed the query** with the same model (and the query prefix).
2. **One SQL query** with two CTEs, each returning the top 20 **chunks**:
   - keyword: `websearch_to_tsquery('simple', unaccent(query))`, ranked by `ts_rank`;
   - vector: ordered by `embedding <=> query_vector` (cosine distance);
   - **filters inside both CTEs**: active postings, group representatives only (joined to `current_postings`), plus the CLI filters. Filtering after the merge could leave almost nothing.
3. **Collapse chunks to postings** (best rank per posting), then **RRF**: score = Σ 1 / (60 + rank) over the two lists, with the CTEs combined through a `FULL OUTER JOIN` rather than a scan of the chunks table.
4. **Rerank** (D3): the top 20 postings, each as (query, best-matching chunk), scored by the cross-encoder and re-sorted.
5. **Output:** rank, title, company, source, LATAM, remote, salary if any, the RRF and rerank scores, and the URL. `--explain` adds each posting's keyword and vector ranks.

Search is a plain function (`search(conn, query, filters, …) → list[SearchHit]`), which Phase 3's agent will call as a tool.

## Data model (migration `0005`)

```
posting_enrichments   one row per posting: the latest enrichment
llm_calls             one row per LLM call: model, tokens, cost, latency, outcome
posting_chunks        chunks of each posting: text, tsvector, embedding
```

- **`posting_enrichments`:** `posting_id` (PK, FK to `postings`, `ON DELETE CASCADE`), `source_content_hash`, `prompt_version`, `provider`, `model`, `status` (`ok`, `failed`), `attempts`, `error`, the `EnrichmentOut` fields as typed columns (lists as `text[]`), `dedup_key`, `last_run_id` (FK), `enriched_at`.
- **`llm_calls`:** `id`, `run_id` (FK), `posting_id` (FK, `ON DELETE SET NULL`), `purpose` (`enrich`), `provider`, `model`, `prompt_version`, `status` (`ok`, `invalid_output`, `ungrounded`, `refused`, `error`), `input_tokens`, `output_tokens`, `cost_usd` (numeric), `latency_ms`, `error`, `response` (JSONB, the raw answer, for debugging), `created_at`. It is the history of every call; `posting_enrichments` holds only the latest result.
- **`posting_chunks`:** `id`, `posting_id` (FK, cascade), `chunk_index` (unique with `posting_id`), `text` (exactly what was embedded, header included), `tsv` (`tsvector`, written by the insert with `to_tsvector('simple', unaccent(text))`; `unaccent` isn't marked immutable, so Postgres doesn't allow it in a generated column), `embedding` (`vector(768)`), `embedding_model`, `index_fingerprint`, `last_run_id` (FK), `indexed_at`.
- **Extension:** `unaccent`.
- **`pipeline_runs`:** `pipeline` also allows `enrich` and `index`. **No stored counts**, following Phase 1's rule (totals are `SUM`s, never copies): calls, outcomes, tokens and cost come from `llm_calls.run_id`; postings enriched and indexed per run come from `last_run_id` on `posting_enrichments` and `posting_chunks`. The one number that can't be derived, how many postings were stale when the run started, goes in the existing `params` JSONB. Considered and rejected: a `stats` JSONB column (untyped, and copies derivable numbers) and an `enrich_stats` table (typed, but one table per pipeline and a migration per new counter).
- **View `enriched_postings`:** `current_postings` with enrichment columns, the source-first `resolved_*` columns and `remote_modes` (re-created in migration `0007`).

The vector dimension (768) is fixed by the model, so changing the model means a migration and a full re-index (vectors from different models can't be compared).

## Pipeline and run ledger

- `jobradar enrich` and `jobradar index` each create a `pipeline_runs` row (`enrich`, `index`) and take their **own** advisory lock: two enrich runs can't overlap, but an ingest can run alongside, since staleness reconciles on the next run.
- After `enrich`, linking runs again so that newly enriched HN postings can link.
- Scheduling: the README's crontab line becomes `ingest`, then `enrich`, then `index`, each only if the previous one exited 0.
- `jobradar runs` shows enrich and index runs with their counts (derived as above), and calls, tokens and cost for enrich runs.

## Configuration

New variables, all listed in `.env.example`: `ANTHROPIC_API_KEY` (secret), `LLM_PROVIDER` (`claude` or `gemini`, default `claude`), `ENRICH_CONCURRENCY` (4), `ENRICH_MAX_CALLS` (3000), `FASTEMBED_CACHE_PATH` (`.cache/fastembed`). `GEMINI_API_KEY` already exists.

New dependencies: `anthropic`, `google-genai`, `fastembed`, `onnxruntime<1.24`. All are pure Python except `onnxruntime` and `tokenizers`, whose Intel-macOS wheels were checked.

## Tests

- **Schema and validation:** `EnrichmentOut` (enums, salary order, skill aliases), and the grounding check (a quote that's in the text vs one that isn't).
- **Providers:** the request each adapter builds and how it parses a recorded response; no live calls in tests. Refusals and invalid JSON go to the right `llm_calls` status.
- **Enrich pipeline (integration, fake provider):** only stale postings are sent; a second run makes 0 calls; a `content_hash` change or a `PROMPT_VERSION` bump makes a posting stale; one retry and then `failed`; skipped after 3 attempts; a crash mid-run keeps what was committed.
- **Chunking:** paragraph packing, the token limit, overlap, and a header on every chunk.
- **Index pipeline (integration, fake embedder** that returns deterministic vectors**):** fingerprint staleness; re-index replaces a posting's chunks atomically.
- **Search (integration, hand-made vectors):** RRF order, filters applied inside both searches, one result per posting, only group representatives, keyword search matching Spanish words with and without accents.
- Tests that need the real models are marked `model` and skipped in CI (they would download ~1.8 GB).

## Out of scope

Answering questions with retrieved context and matching against a resume (Phase 3), the agent (Phase 3), tracing and the full eval framework (Phase 4), an API or UI (Phase 5), the Batch API, a vector index, fine-tuning, and embeddings for cross-source duplicate matching.

## Acceptance criteria

1. `jobradar enrich` enriches every active posting, with under 2% `failed`, and the backfill costs under $3 (from `llm_calls`).
2. Running it again immediately makes 0 LLM calls.
3. The owner reviews 20 random enrichments next to their postings: at least 90% of fields are correct, and `latam_hiring` is right in at least 18 of 20.
4. HN postings show company and title through `enriched_postings`, and at least one HN posting is linked to a copy on another board, or the report explains why none are.
5. `jobradar index` indexes every active posting; running it again embeds nothing.
6. `jobradar search` returns relevant results with scores, in under 15 s with reranking and under 3 s without. The mini-eval table reports precision@5 for keyword, vector, hybrid and hybrid + rerank.
7. `make check` passes locally and in CI.

## Steps

Commits name their step: `[jobradar][p2][s<N>] <description>`. Diagrams and ADRs are updated in the step that changes what they describe.

| Step | Scope | Status |
|---|---|---|
| s0 | Phase setup: pyright/Pylance points at `.venv` (`8c2f946`); architecture, data-flow, ERD and state-machine diagrams for Phase 1 (`c2c81a4`); this spec (`b54f460`) | Done |
| s1 | Split `models.py` into a `models/` package (its own commit, no behaviour change: `0789713`); migration `0005` (three tables, `unaccent`, a `pipeline` CHECK on `pipeline_runs`), the `enriched_postings` view, ORM models, schema tests; ERD | Done |
| s2 | LLM layer: provider interface, Claude, Gemini and fake adapters, rate limiter, price table, `llm_calls` logging; `EnrichmentOut`, prompt, grounding check; ADR-0003 (provider and structured outputs) | Done |
| s3 | Short introduction to evals. Enrich pipeline and CLI (`enrich`, `--dry-run`, `--limit`, `--source`); a 50-posting trial run and the owner's spot check; then the full backfill. Prompt caching and Gemini's schema fixed after the first live calls (`43e699a`) | Done (`823a392`). Owner spot check of v1: 97.0% of fields, LATAM 20/20; v3 re-check of the changed fields held. Backfill (run `6e0ecf06`): 2,659/2,659 ok, 0 failed, 13 ungrounded, $1.19 in 54 min; $1.25 for all enrichment so far. Re-run: 0 calls |
| s4 | HN linking through the enrichment's `dedup_key`; re-link after enrich (shared `relink()`, serialized by a transaction-level advisory lock); `jobradar link` | Done. On real data: 0 HN links. Only 2 of ~420 HN companies appear on another board; the one plausible duplicate (Perry Street Software, "Senior Mobile Developer (iOS)" vs "… (iOS/Android) - LATAM") scores 0.62 on title similarity, below 0.8 |
| s5 | Chunking, embeddings and the index pipeline (`index`), `onnxruntime` pin; ADR-0004 (local embeddings) | Built. Full index (run `b6807934`): 2,709 postings, 7,478 chunks (2.8 per posting, at most 9), 53 min on CPU; re-run: nothing to index. Done |
| s6 | Hybrid search with RRF, filters and reranking; `search` CLI | Not started |
| s7 | Mini retrieval eval, acceptance run on real data, crontab line, README, diagrams | Not started |

## Decisions

- D1–D6 as in [Key choices](#key-choices) (2026-11-05).
- Use `fastembed` (ONNX) for local models, with `onnxruntime` pinned below 1.24 for Intel macOS (2026-11-04). The pin is scoped to Intel Macs with an environment marker, and `requires-python` is now `>=3.12,<3.13` (as ADR-0001 already decided), so uv can resolve one lockfile for every platform (2026-11-07).
- embeddinggemma's query and document prefixes are added by our code: fastembed documents them but doesn't apply them (2026-11-07).
- Broad corpus; narrowing by filters at search time, not at ingestion (2026-11-05).
- Code layout: a `models/` package, and one `retrieval/` package for chunking, embeddings, indexing, search and reranking (2026-11-05).
- Enrichment calls run on threads, not `asyncio` (2026-11-05).
- No stored run counts for enrich and index; derived from `llm_calls` and `last_run_id` (2026-11-05).
- **Linking:** copies whose enrichments say LATAM "yes" and "no" are never grouped; "unknown" links with either (owner's design task; a stricter "known location on both sides" rule would have broken most of the 21 correct links) (2026-11-06).
- **Board vs text conflicts:** title, company and salary: the source wins. Remote: a conflict counts as both values (`remote_modes`), so the job appears in remote and hybrid searches (owner's design task) (2026-11-06).
- `salary_basis` (gross or net) extracted now. **Tech debt:** salaries in different currencies and periods aren't converted to one comparable figure (2026-11-05).
- **Inference policy** (owner, after the spot check): the prompt allows clear inferences for seniority (from requirements, or the most senior of several roles) and spoken languages (the posting's language when the job is limited to countries that speak it); every other field stays "stated facts only". A broader "or what it clearly implies" rule (prompt v2) made the model cautious about LATAM, so it was narrowed in v3 (2026-11-06).
- **Prompt evaluation loop:** each prompt version is trialled on the same 50 postings (10 per source) and compared with the previous one by a code eval against the boards' own structured fields (remote, seniority) plus a human check of the fields that changed, before any backfill (2026-11-06).
- **Later idea:** a `people_management` flag. Tech-lead roles that mentor, coordinate and design without much coding sit between `fullstack` (or `backend`…) and `management`, and one `role_family` value can't say both (owner's v3 review, posting 1042). Add it if such roles matter for the owner's searches (2026-11-06).
- **Later idea:** extract benefits (paid time off, paid vacations, bonus, equipment) as filter tags, for searches like "match my resume, but only companies that offer PTO". It would be a new `EnrichmentOut` field and a `PROMPT_VERSION` bump. GetOnBoard already sends structured `perks` and `benefits` fields (kept in `raw_postings`, not parsed yet). Not before the UI can take such filters (2026-11-05).

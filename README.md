# JobRadar

Job-market intelligence built like a data platform: it ingests job postings from public APIs into Postgres, enriches them with an LLM, indexes them for hybrid search, and runs a LangGraph agent that scores fit against a resume, finds skill gaps and drafts tailored resume bullets, with a human approving each step.

> **Status:** Phase 2 (enrichment and retrieval); Phase 1 (ingestion) is done. See the [project overview](specs/00-overview.md) for the phases and the target architecture.

## Stack

Python 3.12 · uv · Postgres 17 + pgvector · SQLAlchemy 2 · Alembic · Docker Compose · Ruff · pyright · pytest · pre-commit (gitleaks) · GitHub Actions

Coming in later phases: LangGraph, LLM structured outputs (Gemini, Groq), local embeddings, hybrid search and reranking, Langfuse, FastAPI, MCP, Streamlit, PySpark.

## Getting started

Requirements: Docker and [uv](https://docs.astral.sh/uv/).

```bash
cp -n .env.example .env   # local settings; never committed
make install              # Python 3.12, dependencies, pre-commit hooks
make up                   # Postgres + pgvector on localhost:5434
make migrate              # apply database migrations
make health               # check the database and pgvector
make check                # lint, format check, types and tests
```

`make help` lists every command.

## Usage

```bash
uv run jobradar ingest                      # fetch every source, load raw, transform, link duplicates
uv run jobradar ingest --source hn          # one source (repeatable)
uv run jobradar ingest --resume RUN_ID      # finish a failed run without fetching again
uv run jobradar transform --rebuild         # re-parse everything already fetched (after a parser fix)
uv run jobradar runs                        # recent runs, per-source status and counts
```

Some sources limit how often they may be fetched (Remotive: every 6 h; Himalayas: every 20 h, its data refreshes daily; Jobicy: every hour), so `ingest` skips a source fetched more recently than that; `--force` overrides it.

## Scheduling

Twice a day with `cron`, the Unix job scheduler. Edit your crontab with `crontab -e` and add:

```cron
# m h dom mon dow  command   (here: 08:00 and 20:00 every day)
0 8,20 * * * cd "$HOME/projects/jobradar" && "$HOME/.local/bin/uv" run jobradar ingest --trigger scheduled >> logs/ingest.log 2>&1
```

- `cd` first: settings are read from `.env` in the project folder.
- The full path to `uv`, because cron runs with a minimal `PATH` that doesn't include `~/.local/bin`.
- `--trigger scheduled` is recorded in the run ledger; `>> logs/ingest.log 2>&1` appends output and errors to a git-ignored log (create it once with `mkdir -p logs`).
- The database container must be running (`make up`). `ingest` exits non-zero unless the run succeeded, so failures show up in the log and in `jobradar runs`.
- On macOS, cron doesn't run while the machine is asleep and doesn't catch up afterwards (`launchd` does, at the cost of a more verbose setup). For this project a missed run is harmless: the next one fetches everything again.

## Documentation

| Document | Contents |
|---|---|
| [specs/](specs/) | Project overview and one spec per phase, written before the code |
| [docs/diagrams/](docs/diagrams/) | Architecture, data flow, ERD and state machines (Mermaid) |
| [docs/adr/](docs/adr/) | Architecture Decision Records |
| [CLAUDE.md](CLAUDE.md) | Working rules for AI coding agents in this repository |

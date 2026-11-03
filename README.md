# JobRadar

Job-market intelligence built like a data platform: it ingests job postings from public APIs into Postgres, enriches them with an LLM, indexes them for hybrid search, and runs a LangGraph agent that scores fit against a resume, finds skill gaps and drafts tailored resume bullets, with a human approving each step.

> **Status:** Phase 0 (foundation). See the [project overview](specs/00-overview.md) for the phases and the target architecture.

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

## Documentation

| Document | Contents |
|---|---|
| [specs/](specs/) | Project overview and one spec per phase, written before the code |
| [docs/adr/](docs/adr/) | Architecture Decision Records |
| [CLAUDE.md](CLAUDE.md) | Working rules for AI coding agents in this repository |

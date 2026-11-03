# Phase 0: Foundation

## Goal

A repository that every later phase can build on: one command starts the database, one command checks the code, and CI runs the same checks on every push.

## Scope

- **Python project** managed with uv: `src/` layout, package `jobradar`, Python 3.12, lockfile committed.
- **Configuration** from environment variables with pydantic-settings. `.env` is git-ignored; `.env.example` lists every variable.
- **Database:** Postgres 17 with the pgvector extension, in Docker Compose, on host port 5434. A second database, `jobradar_test`, for tests.
- **Migrations** with Alembic. The first migration enables the `vector` extension.
- **CLI** (`jobradar`) with a `health` command that reports the Postgres version and the pgvector version.
- **Quality tooling:** Ruff (lint and format), pyright (types), pytest.
- **pre-commit hooks:** Ruff, a secret scanner (gitleaks), and basic file checks.
- **CI** on GitHub Actions: the same checks as `make check`, against a Postgres + pgvector service container.
- **Docs:** README, this spec, the project overview, ADRs 0001 and 0002, CLAUDE.md.

## Out of scope

Any table besides Alembic's own, any data source, any LLM call. Those start in Phase 1.

## Acceptance criteria

1. `make install && make up && make migrate` works on a fresh clone with only Docker and uv installed.
2. `make health` prints the Postgres and pgvector versions.
3. `make check` (lint, format check, types, tests) passes.
4. The integration tests run the migrations against `jobradar_test` and compute a vector distance in SQL.
5. A commit containing a fake API key is blocked by the pre-commit hook.
6. CI passes on GitHub.

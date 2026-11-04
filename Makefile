# One entry point for common commands, used by humans and CI alike.
# `make <target>`; `make help` lists them. Each recipe line runs in its own shell.

# Targets that are commands, not files: Make shouldn't look for files with these names.
.PHONY: help install up down reset-db migrate health ingest runs lint fmt typecheck test check

help: ## List the available commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-10s %s\n", $$1, $$2}'

install: ## Install Python 3.12, the dependencies and the git hooks
	uv sync
	uv run pre-commit install

up: ## Start Postgres + pgvector and wait until it's healthy
	docker compose up -d --wait

down: ## Stop the containers (data is kept)
	docker compose down

reset-db: ## Delete all local data and start an empty database
	docker compose down -v
	docker compose up -d --wait

migrate: ## Apply database migrations
	uv run alembic upgrade head

health: ## Check the database and pgvector
	uv run jobradar health

ingest: ## Fetch postings from every source and load them (rate-limited sources are skipped when fetched recently)
	uv run jobradar ingest

runs: ## Show the most recent pipeline runs and their counts
	uv run jobradar runs

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Fix lint issues and format the code
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## Type-check with pyright
	uv run pyright

test: ## Run all tests (integration tests need `make up`)
	uv run pytest

check: lint typecheck test ## Everything CI runs

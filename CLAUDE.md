# CLAUDE.md

Working rules for AI coding agents in this repository.

## Project

JobRadar: ingestion, LLM enrichment, retrieval and a LangGraph agent over public job postings. It is also a learning project: the owner reviews every change to understand it.

- Overview and phases: [specs/00-overview.md](specs/00-overview.md)
- Current phase spec: [specs/phase-1-ingestion.md](specs/phase-1-ingestion.md) (Phase 0 is done)
- Decisions: [docs/adr/](docs/adr/)

## Rules

1. **Spec first.** Each phase starts with `specs/phase-N-*.md`, approved by the owner before code is written. Don't build features of later phases.
2. **Comment generously.** Explain the *why* and the concept behind non-obvious lines (what a library call does, why a setting matters). The owner learns from the code.
3. **No commits, pushes or deployments** unless the owner asks.
4. **No secrets in the repository.** Settings come from environment variables (`src/jobradar/config.py`); `.env` is git-ignored and `.env.example` lists every variable.
5. **Free tiers first.** Ask before adding anything that costs money.
6. New architectural decisions are written as an ADR in `docs/adr/`.

## Learning workflow

The owner is learning the AI/LLM stack through this project. Keep each of these brief and save them for important points, so the project doesn't stretch into weeks:

- **Concepts:** each phase introduces its AI/LLM concepts and tools (what they are, why they're used here) and points to the files where they are implemented or configured. AI/LLM concepts are new to the owner, so every introduction starts with a simple, plain-language explanation (ELI5) and only then goes technical, step by step. Never open with the technical version.
- **Quizzes:** from time to time, 4–5 questions in the chat (not in code) on the current phase's relevant code: what it does, how and why. The owner answers in the chat; check, correct, and move on. Never quiz on an AI/LLM concept before it has been introduced as above.
- **Design tasks:** occasionally, a couple of design problems. The owner proposes an approach; say whether it's good to go or recommend alternatives.

## Git workflow

- **One branch per phase**, cut from `main`: `jobradar-<gituser>-p<N>-<brief-desc>`, where `<gituser>` is the GitHub handle. Example: `jobradar-nachhh-p1-postings-ingestion`.
- **Steps:** each phase spec has a "Steps" section: the order the phase is built in, with a status per step. Keep it up to date. Steps (building the code) are not the pipeline's stages (what a run does).
- **Commit messages:** `[jobradar][p<N>][s<X>] <what was done>`, where `s<X>` is the step. A step can take several commits (fixes, follow-ups). Example: `[jobradar][p1][s4] Add ingest pipeline, run ledger and CLI`.
- Commits and pushes still happen only when the owner asks (rule 3).

## Environment

Intel Mac, 16 GB RAM, no GPU. Python 3.12 (ML libraries lag on newer versions on Intel macOS). Check that a library ships Intel-macOS builds before adding it.

## Commands

`make help` lists them. The main ones: `make install`, `make up`, `make migrate`, `make health`, `make check`.

Integration tests use the `jobradar_test` database and reset it on every run.

## Code style

Ruff (lint and format, line length 110), pyright, type hints on every function signature. `snake_case` functions and modules, `PascalCase` classes, `UPPER_CASE` constants.

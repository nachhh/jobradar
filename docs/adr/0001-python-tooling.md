# ADR-0001: Python tooling

- **Status:** Accepted
- **Date:** 2026-11-01

## Context

The project needs reproducible environments, fast feedback on mistakes, and the same checks locally and in CI. It is developed on an Intel Mac, where some ML libraries no longer ship builds for the newest Python versions.

## Decision

- **uv** for Python versions, virtual environments, dependencies and the lockfile (`uv.lock`).
- **Python 3.12**, pinned in `.python-version`. Newer versions are skipped because ML libraries (PyTorch, ONNX Runtime) lag behind them, especially on Intel macOS.
- **`src/` layout**: the package lives in `src/jobradar/`, so tests import the installed package, not files that happen to sit in the working directory.
- **Ruff** for linting and formatting, **pyright** for type checking, **pytest** for tests.
- **pre-commit** runs Ruff and **gitleaks** (secret scanning) before every commit.
- **Make** as the single entry point for common commands, so CI and humans run the same thing.

## Consequences

- One tool (uv) replaces pip, venv, pip-tools and pyenv. `uv sync --locked` gives the exact same environment everywhere, and fails if the lockfile is stale.
- Mistakes such as unused imports, type errors and committed secrets are caught before they reach GitHub.

## Alternatives considered

- **pip + venv + requirements.txt:** familiar, but no lockfile by default and slower. Rejected.
- **Poetry:** mature, but slower and does not manage Python versions. Rejected.
- **Black + isort + Flake8:** three tools where Ruff is one, and much slower. Rejected.

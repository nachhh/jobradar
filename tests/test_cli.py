"""Unit tests for the CLI. The database check is replaced by a stub, so no database is needed:
these tests check how the CLI reacts to each health result, not the database itself."""

import pytest
from typer.testing import CliRunner

from jobradar import cli
from jobradar.db import DatabaseHealth

runner = CliRunner()


def test_health_reports_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    # Replace check_database *where cli.py looks it up* (jobradar.cli), not where it's defined.
    monkeypatch.setattr(cli, "check_database", lambda engine: DatabaseHealth("17.6", "0.8.1"))
    result = runner.invoke(cli.app, ["health"])
    assert result.exit_code == 0
    assert "pgvector: 0.8.1" in result.output


def test_health_fails_without_pgvector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "check_database", lambda engine: DatabaseHealth("17.6", None))
    result = runner.invoke(cli.app, ["health"])
    assert result.exit_code == 1
    assert "make migrate" in result.output

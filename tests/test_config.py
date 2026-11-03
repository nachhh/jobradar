"""Unit tests for settings: no database, no network."""

import pytest

from jobradar.config import Settings


def test_environment_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # monkeypatch sets env vars for this test only and restores them afterwards.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db.example:5432/other")
    # _env_file=None ignores any local .env, so the test behaves the same on every machine.
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.database_url.endswith("db.example:5432/other")


def test_api_keys_are_hidden_when_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    # Logging or printing the settings must never leak the key...
    assert "super-secret-value" not in repr(settings)
    # ...while code that needs it can still read it explicitly.
    assert settings.gemini_api_key is not None
    assert settings.gemini_api_key.get_secret_value() == "super-secret-value"

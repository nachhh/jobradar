"""Application settings, read from environment variables and a local `.env` file.

This follows the "config in the environment" rule of twelve-factor apps: the same code runs
on a laptop, in CI and in a container, and only the environment changes. Secrets (database
passwords, API keys) therefore never live in the code or in git.

Precedence, highest first: real environment variables > `.env` file > defaults below.
That is why CI can set DATABASE_URL without any `.env` file existing.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every setting the app reads. Field names map to env vars case-insensitively:
    `database_url` is read from DATABASE_URL."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Ignore variables in .env that this class doesn't declare (e.g. ones used only by Docker).
        extra="ignore",
    )

    # SQLAlchemy URL format: dialect+driver://user:password@host:port/database
    # "postgresql+psycopg" = Postgres through the psycopg 3 driver.
    # Port 5434 is the host side of the Docker port mapping (see docker-compose.yml).
    database_url: str = "postgresql+psycopg://jobradar:jobradar@localhost:5434/jobradar"

    # Used by the integration tests; a separate database so tests can wipe it freely.
    test_database_url: str = "postgresql+psycopg://jobradar:jobradar@localhost:5434/jobradar_test"

    # LLM providers (Phase 2). SecretStr hides the value in logs, reprs and tracebacks:
    # print(settings) shows "**********"; the real value needs .get_secret_value().
    anthropic_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    # Which provider the enrich pipeline calls (decision D1: Claude by default, Gemini as the
    # alternative). Literal makes pydantic reject any other value at startup, not mid-run.
    llm_provider: Literal["claude", "gemini"] = "claude"
    # Enrichment calls in flight at once (threads), and the most calls a single run may make:
    # a guard against a bug or a prompt change spending more than intended.
    enrich_concurrency: int = 4
    enrich_max_calls: int = 3000

    # Job sources (Phase 1). Optional: without it, Himalayas requests share a public allowance with
    # every other keyless caller. Free key: https://himalayas.app/api-keys
    himalayas_api_key: SecretStr | None = None

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Build Settings once and reuse it (lru_cache makes this a cheap singleton).

    Tests that change env vars call get_settings.cache_clear() so the next call re-reads them.
    """
    return Settings()

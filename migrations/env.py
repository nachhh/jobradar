"""Alembic environment: how migrations connect to the database.

Alembic runs this file for every command (upgrade, downgrade, revision --autogenerate).
It's trimmed from Alembic's template to "online" mode only: always run against a live database.
"""

from logging.config import fileConfig

from alembic import context

from jobradar.db import Base, get_engine

# Apply the [loggers] sections of alembic.ini, so commands print lines like
# "Running upgrade  -> 0001, Enable the pgvector extension."
# Skipped when the caller has no ini file (config_file_name is None).
if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)

# Importing models registers their tables on Base.metadata (Phase 1 adds `import jobradar.models`).
# Autogenerate diffs this metadata against the live schema to write new migration files.
target_metadata = Base.metadata


def run_migrations() -> None:
    # A caller can pass an explicit URL (the tests do, to target jobradar_test);
    # otherwise use DATABASE_URL from the app settings.
    url = context.config.attributes.get("database_url")
    engine = get_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        # Each migration runs inside a transaction. Postgres supports transactional DDL,
        # so a failed migration rolls back completely instead of leaving a half-changed schema.
        with context.begin_transaction():
            context.run_migrations()


run_migrations()

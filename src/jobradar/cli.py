"""Command-line entry point: `uv run jobradar <command>`.

Typer turns type-annotated functions into CLI commands, with --help generated from docstrings.
Pipeline runs (Phase 1) and agent runs (Phase 3) will be added here as commands.
"""

import typer
from sqlalchemy.exc import OperationalError

from jobradar.db import check_database, get_engine

app = typer.Typer(help="JobRadar: job-market intelligence over public job postings.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Group the commands below under `jobradar`. Without a callback, a single command would
    become the whole CLI and `jobradar health` would fail."""


@app.command()
def health() -> None:
    """Check that Postgres is reachable and pgvector is installed."""
    try:
        status = check_database(get_engine())
    except OperationalError as exc:
        # OperationalError = connection-level failure (DB down, wrong port, bad password).
        typer.secho(f"Database unreachable: {exc.orig}", fg="red", err=True)
        typer.echo("Is it running? Try `make up`.", err=True)
        # A non-zero exit code makes scripts, Make and CI treat this as a failure.
        raise typer.Exit(code=1) from exc

    typer.echo(f"Postgres: {status.postgres_version}")
    if status.pgvector_version is None:
        typer.secho("pgvector: not installed. Run `make migrate`.", fg="yellow")
        raise typer.Exit(code=1)
    typer.echo(f"pgvector: {status.pgvector_version}")

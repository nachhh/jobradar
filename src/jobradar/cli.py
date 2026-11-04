"""Command-line entry point: `uv run jobradar <command>`.

Typer turns type-annotated functions into CLI commands, with --help generated from docstrings.
The commands are thin: they parse options, call a pipeline function, and print the outcome.
"""

import logging
import uuid
from typing import Annotated

import typer
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from jobradar.config import get_settings
from jobradar.db import check_database, get_engine
from jobradar.ingest import pipeline
from jobradar.models import PipelineRun, PipelineRunSource, RunStatus, Source, Trigger

app = typer.Typer(help="JobRadar: job-market intelligence over public job postings.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Group the commands below under `jobradar`. Without a callback, a single command would
    become the whole CLI and `jobradar health` would fail."""
    # Log lines like "12:00:01 INFO jobradar.ingest.pipeline: getonboard: fetched 602 …".
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request at INFO; keep those for debugging only.
    logging.getLogger("httpx").setLevel(logging.WARNING)


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


@app.command()
def ingest(
    # Annotated[type, typer.Option(...)] declares an option; a list type makes it repeatable:
    # --source getonboard --source hn. Typer validates the values against the Source enum.
    source: Annotated[list[Source] | None, typer.Option(help="Only these sources (repeatable).")] = None,
    force: Annotated[bool, typer.Option(help="Fetch Remotive even within 6 h of its last fetch.")] = False,
    resume: Annotated[str | None, typer.Option(help="Finish this run instead of starting a new one.")] = None,
    trigger: Annotated[Trigger, typer.Option(help="Recorded in the ledger; cron passes 'scheduled'.")] = (
        Trigger.MANUAL
    ),
) -> None:
    """Fetch job postings from the sources and load them (raw, then clean)."""
    engine = get_engine()
    try:
        if resume:
            result = pipeline.resume_ingest(engine, uuid.UUID(resume), force=force)
        else:
            result = pipeline.run_ingest(engine, source or list(Source), trigger=trigger, force=force)
    except (pipeline.RunLockedError, pipeline.NothingToRunError, pipeline.ResumeError, ValueError) as exc:
        typer.secho(str(exc), fg="yellow", err=True)
        raise typer.Exit(code=1) from exc
    _print_runs(limit=1, run_id=result.run_id)
    if result.status != RunStatus.SUCCEEDED:
        raise typer.Exit(code=1)  # partial or failed: let cron/CI notice


@app.command()
def transform(
    rebuild: Annotated[bool, typer.Option("--rebuild", help="Re-parse all raw postings.")] = False,
) -> None:
    """Re-run the transform without calling any API."""
    if not rebuild:
        # A plain transform only happens inside `ingest` (or `ingest --resume`); keep one way in.
        typer.echo("Nothing to do: pass --rebuild, or use `jobradar ingest --resume RUN_ID`.")
        raise typer.Exit(code=2)
    try:
        result = pipeline.rebuild(get_engine())
    except pipeline.RunLockedError as exc:
        typer.secho(str(exc), fg="yellow", err=True)
        raise typer.Exit(code=1) from exc
    _print_runs(limit=1, run_id=result.run_id)


@app.command()
def runs(limit: Annotated[int, typer.Option(help="How many runs to show.")] = 10) -> None:
    """The most recent runs, with per-source status and counts."""
    _print_runs(limit=limit)


STATUS_COLORS = {"succeeded": "green", "partial": "yellow", "failed": "red", "running": "cyan"}


def _print_runs(limit: int, run_id: uuid.UUID | None = None) -> None:
    engine = get_engine()
    query = select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(limit)
    if run_id is not None:
        query = query.where(PipelineRun.run_id == run_id)
    with engine.connect() as conn:
        run_rows = conn.execute(query).all()
        source_rows = conn.execute(
            select(PipelineRunSource)
            .where(PipelineRunSource.run_id.in_([r.run_id for r in run_rows]))
            .order_by(PipelineRunSource.started_at)
        ).all()

    if not run_rows:
        typer.echo("No runs yet. Try `jobradar ingest`.")
        return
    for run in run_rows:
        started = run.started_at.astimezone().strftime("%Y-%m-%d %H:%M")
        took = f"{(run.finished_at - run.started_at).total_seconds():.0f}s" if run.finished_at else "-"
        status = typer.style(f"{run.status:<9}", fg=STATUS_COLORS.get(run.status))
        typer.echo(f"{run.run_id}  {started}  {status}  {run.pipeline:<7} {run.trigger:<9} {took:>5}")
        skipped = run.params.get("skipped") or []
        if skipped:
            typer.echo(f"    skipped: {', '.join(skipped)}")
        for src in (s for s in source_rows if s.run_id == run.run_id):
            line = (
                f"    {src.source:<10} {src.status:<9} {src.stage or '-':<11} "
                f"fetched {src.fetched:>4}  inserted {src.inserted:>4}  updated {src.updated:>4}  "
                f"unchanged {src.unchanged:>4}  rejected {src.rejected:>3}"
            )
            typer.secho(line, fg="red" if src.status == "failed" else None)
            if src.error_type:
                typer.secho(f"      {src.error_type}: {src.error_message}", fg="red")

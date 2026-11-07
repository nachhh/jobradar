"""Command-line entry point: `uv run jobradar <command>`.

Typer turns type-annotated functions into CLI commands, with --help generated from docstrings.
The commands are thin: they parse options, call a pipeline function, and print the outcome.
"""

import logging
import uuid
from typing import Annotated

import typer
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from jobradar.config import get_settings
from jobradar.db import check_database, get_engine
from jobradar.enrich import pipeline as enrich_pipeline
from jobradar.ingest import pipeline
from jobradar.ingest.link import link_duplicates
from jobradar.ledger import RunLockedError
from jobradar.llm import MissingApiKeyError, make_provider
from jobradar.models import LlmCall, PipelineRun, PipelineRunSource, RunStatus, Source, Trigger

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
    # httpx (and httpx2, its fork used by the Anthropic SDK) log every request at INFO; keep those
    # for debugging only.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)


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
    except (RunLockedError, pipeline.NothingToRunError, pipeline.ResumeError, ValueError) as exc:
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
    except RunLockedError as exc:
        typer.secho(str(exc), fg="yellow", err=True)
        raise typer.Exit(code=1) from exc
    _print_runs(limit=1, run_id=result.run_id)


@app.command()
def enrich(
    limit: Annotated[
        int | None, typer.Option(help="At most this many postings (per source with --source).")
    ] = None,
    source: Annotated[list[Source] | None, typer.Option(help="Only these sources (repeatable).")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Count stale postings and estimate the cost.")
    ] = False,
    trigger: Annotated[Trigger, typer.Option(help="Recorded in the ledger; cron passes 'scheduled'.")] = (
        Trigger.MANUAL
    ),
) -> None:
    """Extract LLM fields for active postings whose enrichment is missing or out of date."""
    settings = get_settings()
    engine = get_engine()
    if dry_run:
        stale = enrich_pipeline.find_stale(engine, limit, source)
        # Only the model name is needed for the estimate, so no API key (and no call) is involved.
        model = {"claude": "claude-haiku-5-5", "gemini": "gemini-3.5-flash"}[settings.llm_provider]
        cost = enrich_pipeline.estimate_cost(stale, model)
        typer.echo(f"{len(stale)} postings to enrich with {model}; estimated cost ${cost or 0:.2f}")
        return
    try:
        provider = make_provider(settings)
        result = enrich_pipeline.run_enrich(
            engine,
            provider,
            limit=limit,
            max_calls=settings.enrich_max_calls,
            concurrency=settings.enrich_concurrency,
            sources=source,
            trigger=trigger,
        )
    except enrich_pipeline.NothingToEnrichError as exc:
        typer.echo(str(exc))
        return  # nothing to do is a success: exit 0, so a cron chain continues
    except (RunLockedError, MissingApiKeyError) as exc:
        typer.secho(str(exc), fg="yellow", err=True)
        raise typer.Exit(code=1) from exc
    _print_runs(limit=1, run_id=result.run_id)
    if result.status != RunStatus.SUCCEEDED:
        raise typer.Exit(code=1)


@app.command()
def link() -> None:
    """Re-run cross-source linking over all postings (also runs after every ingest and enrich)."""
    with get_engine().begin() as conn:
        counts = link_duplicates(conn)
    typer.echo(
        f"{counts.exact_pairs} exact and {counts.fuzzy_pairs} fuzzy pairs, {counts.groups} groups, "
        f"{counts.changed} postings (re)linked, {counts.blocked} pairs blocked (LATAM yes vs no)"
    )


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
        # LLM totals per run: SUMs over llm_calls (never stored on the run itself).
        llm_rows = {
            row.run_id: row
            for row in conn.execute(
                select(
                    LlmCall.run_id,
                    func.count().label("calls"),
                    func.count().filter(LlmCall.status.in_(["ok", "ungrounded"])).label("ok"),
                    func.coalesce(func.sum(LlmCall.input_tokens), 0).label("input_tokens"),
                    func.coalesce(func.sum(LlmCall.cache_read_tokens), 0).label("cached_tokens"),
                    func.coalesce(func.sum(LlmCall.output_tokens), 0).label("output_tokens"),
                    func.coalesce(func.sum(LlmCall.cost_usd), 0).label("cost"),
                )
                .where(LlmCall.run_id.in_([r.run_id for r in run_rows]))
                .group_by(LlmCall.run_id)
            ).all()
        }

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
        if (llm := llm_rows.get(run.run_id)) is not None:
            typer.echo(
                f"    {run.params.get('stale', '?')} stale  {llm.calls} calls ({llm.ok} ok)  "
                f"tokens in {llm.input_tokens:,} + cached {llm.cached_tokens:,}, out {llm.output_tokens:,}  "
                f"cost ${llm.cost:.4f}"
            )
        for src in (s for s in source_rows if s.run_id == run.run_id):
            line = (
                f"    {src.source:<10} {src.status:<9} {src.stage or '-':<11} "
                f"fetched {src.fetched:>4}  inserted {src.inserted:>4}  updated {src.updated:>4}  "
                f"unchanged {src.unchanged:>4}  rejected {src.rejected:>3}"
            )
            typer.secho(line, fg="red" if src.status == "failed" else None)
            if src.error_type:
                typer.secho(f"      {src.error_type}: {src.error_message}", fg="red")

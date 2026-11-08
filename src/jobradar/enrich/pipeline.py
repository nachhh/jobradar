"""The enrich pipeline: LLM fields for every active posting whose enrichment is missing or stale.

    run_enrich()      a run: find stale postings, call the LLM for each, store the results
    find_stale()      the to-do list
    estimate_cost()   what a run would cost, without calling anything (`enrich --dry-run`)

Concepts:
- **Staleness is the to-do list.** A posting needs (re-)enriching when it has no enrichment, its
  content changed (content_hash), or the prompt changed (PROMPT_VERSION). Each posting's result is
  committed on its own, so an interrupted run loses at most the calls in flight, and the next run
  simply picks up what is still stale. No --resume needed, unlike ingestion.
- **What counts as an attempt.** Only a bad *answer* (invalid output, refusal) counts towards
  MAX_ATTEMPTS: after that, a posting the model can't handle is skipped until its content or the
  prompt changes. A network or API error doesn't count (the posting stays stale and is retried next
  run), and an error every call would hit (bad key, unknown model) stops the run at once.
- **Threads.** LLM calls spend almost all their time waiting on the network, so a few run at once
  (ENRICH_CONCURRENCY), paced by a shared rate limiter. Each worker writes its own results through
  its own connection from the engine's pool.
"""

import logging
import threading
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, RowMapping, and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from jobradar.enrich.grounding import apply_grounding
from jobradar.enrich.prompt import INSTRUCTIONS, PROMPT_VERSION, build_posting_text
from jobradar.enrich.schema import EnrichmentOut
from jobradar.ingest.link import relink
from jobradar.ingest.text import dedup_key
from jobradar.ledger import advisory_lock, finish_run, lock_id, run_ledger
from jobradar.llm.base import (
    FatalProviderError,
    InvalidOutputError,
    LlmError,
    LlmProvider,
    LlmResponse,
    Usage,
)
from jobradar.llm.log import record_call
from jobradar.llm.pricing import cost_usd
from jobradar.llm.ratelimit import RateLimiter
from jobradar.models import (
    EnrichmentStatus,
    LlmCallStatus,
    LlmPurpose,
    Pipeline,
    Posting,
    PostingEnrichment,
    RunStatus,
    Source,
    Trigger,
)

logger = logging.getLogger(__name__)

LOCK_ID = lock_id("jobradar.enrich")
MAX_ATTEMPTS = 3  # failed answers on the same content and prompt before a posting is skipped
MAX_CONSECUTIVE_ERRORS = 5  # API errors in a row before the run gives up (the provider is down)

# Rough per-posting figures for --dry-run, measured on the full backfill (2,659 postings, prompt v3):
# the cached prefix (instructions + schema) and the answer, including some thinking.
EST_CACHED_TOKENS = 3700
EST_OUTPUT_TOKENS = 545

# The columns find_stale() reads: what build_posting_text() needs, plus the staleness inputs.
_POSTING_COLUMNS = (
    Posting.id,
    Posting.source,
    Posting.title,
    Posting.company,
    Posting.location_text,
    Posting.remote,
    Posting.tags,
    Posting.salary_min,
    Posting.salary_max,
    Posting.salary_currency,
    Posting.salary_period,
    Posting.salary_text,
    Posting.seniority,
    Posting.description,
    Posting.content_hash,
)


class NothingToEnrichError(Exception):
    """No active posting needs enriching."""


@dataclass
class EnrichCounts:
    """What a run did. Mutable and lock-protected: the worker threads update it."""

    stale: int = 0
    calls: int = 0
    ok: int = 0
    ungrounded: int = 0  # ok, but latam_hiring downgraded to unknown
    failed: int = 0  # bad answer even after the retry (counts as an attempt)
    errors: int = 0  # API errors (not an attempt; the posting stays stale)
    cost_usd: Decimal = Decimal(0)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass(frozen=True)
class EnrichResult:
    run_id: uuid.UUID
    status: RunStatus
    counts: EnrichCounts


def _is_stale() -> Any:
    """The WHERE condition for "needs enriching", as a SQLAlchemy expression.

    e.* is NULL when the LEFT JOIN found no enrichment. A failed enrichment on the same content
    and prompt is retried until it reaches MAX_ATTEMPTS.
    """
    e = PostingEnrichment
    same_input = and_(e.source_content_hash == Posting.content_hash, e.prompt_version == PROMPT_VERSION)
    return or_(
        e.posting_id.is_(None),
        ~same_input,
        and_(e.status == EnrichmentStatus.FAILED, e.attempts < MAX_ATTEMPTS),
    )


def find_stale(
    engine: Engine, limit: int | None = None, sources: Sequence[Source] | None = None
) -> Sequence[RowMapping]:
    """Active postings that need enriching, oldest first (by id, so runs are deterministic).

    `limit` applies per source when `sources` is given, so a trial run can sample every board.
    """
    stmt = (
        select(*_POSTING_COLUMNS)
        .outerjoin(PostingEnrichment, PostingEnrichment.posting_id == Posting.id)
        .where(Posting.is_active, _is_stale())
    )
    if sources:
        # row_number() OVER (PARTITION BY source …) numbers each source's postings 1, 2, 3…;
        # keeping numbers <= limit takes the first `limit` of each source.
        ranked = stmt.add_columns(
            func.row_number().over(partition_by=Posting.source, order_by=Posting.id).label("n")
        ).where(Posting.source.in_(sources))
        sub = ranked.subquery()
        stmt = select(*(sub.c[c.key] for c in _POSTING_COLUMNS))
        if limit is not None:
            stmt = stmt.where(sub.c.n <= limit)
        stmt = stmt.order_by(sub.c.id)
    else:
        stmt = stmt.order_by(Posting.id).limit(limit)
    with engine.connect() as conn:
        return conn.execute(stmt).mappings().all()


def estimate_cost(postings: Sequence[RowMapping], model: str) -> Decimal | None:
    """Estimated cost of enriching `postings`: ~4 characters per token for the posting text."""
    total = Decimal(0)
    for posting in postings:
        usage = Usage(
            input_tokens=len(build_posting_text(posting)) // 4,
            output_tokens=EST_OUTPUT_TOKENS,
            cache_read_tokens=EST_CACHED_TOKENS,
        )
        cost = cost_usd(model, usage)
        if cost is None:
            return None
        total += cost
    return total


def run_enrich(
    engine: Engine,
    provider: LlmProvider,
    *,
    limit: int | None = None,
    max_calls: int = 3000,
    concurrency: int = 4,
    sources: Sequence[Source] | None = None,
    trigger: Trigger = Trigger.MANUAL,
) -> EnrichResult:
    """Enrich stale postings (at most `limit`, per source if `sources` is given), making at most
    `max_calls` LLM calls."""
    with advisory_lock(engine, LOCK_ID, "enrich"):
        postings = find_stale(engine, limit, sources)
        if not postings:
            raise NothingToEnrichError("no active posting needs enriching")
        params = {
            "stale": len(postings),
            "limit": limit,
            "sources": list(sources or []),
            "max_calls": max_calls,
            "model": provider.model,
        }
        with run_ledger(engine, Pipeline.ENRICH, trigger, params) as run_id:
            logger.info("enrich run %s: %d postings, %s", run_id, len(postings), provider.model)
            worker = _Worker(engine, provider, run_id, max_calls)
            worker.counts.stale = len(postings)
            _run_workers(worker, postings, concurrency)
            return _finish(engine, run_id, worker, len(postings))


def _run_workers(worker: "_Worker", postings: Sequence[RowMapping], concurrency: int) -> None:
    """Run worker.enrich over the postings on `concurrency` threads; stop early if interrupted."""
    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="enrich")
    try:
        # list() consumes the results, so an exception in any worker surfaces here.
        list(pool.map(worker.enrich, postings))
    except BaseException:
        # Ctrl-C or a kill lands in this (main) thread; signals never reach worker threads. Without
        # this, shutting the pool down would wait for every queued posting, i.e. the whole run.
        # With the abort flag set, queued postings return at once; calls already in flight finish
        # and commit, so no answer that was paid for is lost.
        with worker.counts.lock:
            worker.abort_reason = worker.abort_reason or "interrupted"
        raise
    finally:
        # cancel_futures drops postings no thread has started; wait=True lets in-flight ones finish.
        pool.shutdown(wait=True, cancel_futures=True)


def _finish(engine: Engine, run_id: uuid.UUID, worker: "_Worker", stale: int) -> EnrichResult:
    """The run's final status from its counts, then re-link and log."""
    counts = worker.counts
    if worker.abort_reason:
        status = RunStatus.FAILED
        logger.error("enrich run %s stopped: %s", run_id, worker.abort_reason)
    elif counts.failed or counts.errors or counts.ok < stale:
        status = RunStatus.PARTIAL if counts.ok else RunStatus.FAILED
    else:
        status = RunStatus.SUCCEEDED
    finish_run(engine, run_id, status)
    if counts.ok:
        # New enrichments can carry dedup keys (HN company and title): link them across boards.
        relink(engine)
    logger.info(
        "enrich run %s: %s (ok %d, ungrounded %d, failed %d, errors %d, %d calls, $%s)",
        run_id,
        status,
        counts.ok,
        counts.ungrounded,
        counts.failed,
        counts.errors,
        counts.calls,
        round(counts.cost_usd, 4),
    )
    return EnrichResult(run_id, status, counts)


class _Worker:
    """Enriches one posting at a time; shared by the pool's threads.

    Shared state (counters, the abort flag, consecutive errors) is only touched under counts.lock.
    """

    def __init__(self, engine: Engine, provider: LlmProvider, run_id: uuid.UUID, max_calls: int) -> None:
        self.engine = engine
        self.provider = provider
        self.run_id = run_id
        self.max_calls = max_calls
        self.limiter = RateLimiter(provider.requests_per_minute)
        self.counts = EnrichCounts()
        self.abort_reason: str | None = None
        self._consecutive_errors = 0

    def enrich(self, posting: RowMapping) -> None:
        text = build_posting_text(posting)
        first = self._call(text)
        if first is None:  # budget spent or run aborted: leave the posting stale for the next run
            return
        outcomes = [first]
        # An invalid answer gets one retry that tells the model what was wrong with it. Refusals
        # and API errors don't: the same input would get the same refusal, and the SDK has already
        # retried transient API errors.
        if isinstance(first, InvalidOutputError):
            retry = self._call(
                f"{text}\n\nYour previous answer was invalid: {str(first)[:500]}\nAnswer again."
            )
            if retry is not None:
                outcomes.append(retry)
        self._store(posting, text, outcomes)

    def _call(self, prompt_text: str) -> LlmResponse[EnrichmentOut] | LlmError | None:
        with self.counts.lock:
            if self.abort_reason or self.counts.calls >= self.max_calls:
                return None
            self.counts.calls += 1
        self.limiter.acquire()
        try:
            return self.provider.extract(INSTRUCTIONS, prompt_text, EnrichmentOut)
        except LlmError as exc:
            return exc

    def _store(
        self, posting: RowMapping, text: str, outcomes: list[LlmResponse[EnrichmentOut] | LlmError]
    ) -> None:
        """Record the calls and the result in one transaction: either all of it lands, or none."""
        final = outcomes[-1]
        grounded = True
        enrichment: EnrichmentOut | None = None
        if isinstance(final, LlmResponse):
            enrichment, grounded = apply_grounding(final.parsed, text)

        with self.engine.begin() as conn:
            for i, outcome in enumerate(outcomes):
                status = LlmCallStatus.UNGROUNDED if (i == len(outcomes) - 1 and not grounded) else None
                record_call(
                    conn,
                    outcome,
                    run_id=self.run_id,
                    posting_id=posting["id"],
                    purpose=LlmPurpose.ENRICH,
                    provider=self.provider.name,
                    prompt_version=PROMPT_VERSION,
                    status=status,
                )
            if enrichment is not None:
                conn.execute(_upsert_ok(posting, enrichment, self.provider, self.run_id))
            elif isinstance(final, LlmError) and not _is_api_error(final):
                # A bad answer: recorded as a failed attempt. (An API error writes nothing here:
                # the posting stays stale and is simply tried again next run.)
                conn.execute(_upsert_failed(posting, final, self.provider, self.run_id))

        with self.counts.lock:
            for outcome in outcomes:
                self.counts.cost_usd += cost_usd(outcome.model, outcome.usage) or Decimal(0)
            if enrichment is not None:
                self.counts.ok += 1
                self.counts.ungrounded += not grounded
                self._consecutive_errors = 0
            elif isinstance(final, LlmError) and _is_api_error(final):
                self.counts.errors += 1
                self._consecutive_errors += 1
                if isinstance(final, FatalProviderError):
                    self.abort_reason = str(final)
                elif self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    self.abort_reason = f"{MAX_CONSECUTIVE_ERRORS} API errors in a row; last: {final}"
            else:
                self.counts.failed += 1
                self._consecutive_errors = 0


def _is_api_error(outcome: LlmError) -> bool:
    """True for errors that say nothing about the posting (network, API): not an attempt."""
    return outcome.status == LlmCallStatus.ERROR


def _upsert_ok(posting: RowMapping, out: EnrichmentOut, provider: LlmProvider, run_id: uuid.UUID) -> Any:
    """INSERT … ON CONFLICT (posting_id) DO UPDATE: the new enrichment replaces the old one."""
    values = out.model_dump(mode="json")  # enums as their string values
    values |= {
        "posting_id": posting["id"],
        "source_content_hash": posting["content_hash"],
        "prompt_version": PROMPT_VERSION,
        "provider": provider.name,
        "model": provider.model,
        "status": EnrichmentStatus.OK,
        "attempts": 0,
        "error": None,
        # The same key as postings.dedup_key, from the LLM's company and title when the source
        # had none (HN): it lets linking (step s4) match those postings across boards.
        "dedup_key": dedup_key(posting["company"] or out.company, posting["title"] or out.title),
        "last_run_id": run_id,
        "enriched_at": func.now(),
    }
    stmt = pg_insert(PostingEnrichment).values(values)
    return stmt.on_conflict_do_update(
        index_elements=[PostingEnrichment.posting_id],
        set_={k: stmt.excluded[k] for k in values if k != "posting_id"},
    )


# Every extracted field: a failed enrichment clears them, so no field from an older answer
# survives next to a "failed" status.
_EXTRACTED = tuple(EnrichmentOut.model_fields)
_LIST_FIELDS = {"location_restrictions", "skills_required", "skills_nice", "languages"}


def _upsert_failed(posting: RowMapping, error: LlmError, provider: LlmProvider, run_id: uuid.UUID) -> Any:
    """Record a failed answer. attempts counts failures on the *same* content and prompt: it goes up
    by one when the previous row is a failure for the same input, and restarts at 1 otherwise."""
    cleared = {name: ([] if name in _LIST_FIELDS else None) for name in _EXTRACTED}
    values = cleared | {
        "posting_id": posting["id"],
        "source_content_hash": posting["content_hash"],
        "prompt_version": PROMPT_VERSION,
        "provider": provider.name,
        "model": provider.model,
        "status": EnrichmentStatus.FAILED,
        "attempts": 1,
        "error": f"{error.status}: {str(error)[:2000]}",
        "dedup_key": None,
        "last_run_id": run_id,
        "enriched_at": func.now(),
    }
    stmt = pg_insert(PostingEnrichment).values(values)
    e = PostingEnrichment
    repeat_failure = and_(
        e.status == EnrichmentStatus.FAILED,
        e.source_content_hash == stmt.excluded.source_content_hash,
        e.prompt_version == stmt.excluded.prompt_version,
    )
    set_: dict[str, Any] = {k: stmt.excluded[k] for k in values if k not in ("posting_id", "attempts")}
    # In ON CONFLICT DO UPDATE, the table's columns refer to the existing row and `excluded` to the
    # row we tried to insert; CASE picks between incrementing and restarting the count.
    set_["attempts"] = case((repeat_failure, e.attempts + 1), else_=1)
    return stmt.on_conflict_do_update(index_elements=[e.posting_id], set_=set_)

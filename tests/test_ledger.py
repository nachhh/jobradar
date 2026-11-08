"""Tests for the run ledger's end-of-run guarantees: every run row ends with a final status.

Each pipeline's own interruption test (test_pipeline, test_enrich_pipeline, test_indexer) checks
what happens to its data; these check the shared mechanism.
"""

import os
import signal
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import Engine, insert, select

from jobradar.ledger import run_ledger
from jobradar.models import Pipeline, PipelineRun, PipelineRunSource, RunStatus, Trigger


@pytest.mark.usefixtures("signal_handlers")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_kill_signals_raise_keyboard_interrupt(sig: signal.Signals) -> None:
    with pytest.raises(KeyboardInterrupt):
        os.kill(os.getpid(), sig)  # the test process signals itself
        time.sleep(5)  # the handler runs here, in the main thread, and interrupts the sleep


def run_row(engine: Engine, run_id: uuid.UUID) -> Any:
    with engine.connect() as conn:
        return conn.execute(select(PipelineRun).where(PipelineRun.run_id == run_id)).one()


@pytest.mark.integration
def test_a_stopped_run_records_interrupted_and_a_crashed_one_failed(clean_db: Engine) -> None:
    ids: list[uuid.UUID] = []
    with pytest.raises(KeyboardInterrupt), run_ledger(clean_db, Pipeline.INDEX, Trigger.MANUAL, {}) as run_id:
        ids.append(run_id)
        raise KeyboardInterrupt
    with pytest.raises(RuntimeError), run_ledger(clean_db, Pipeline.INDEX, Trigger.MANUAL, {}) as run_id:
        ids.append(run_id)
        raise RuntimeError("bug")

    stopped, crashed = (run_row(clean_db, i) for i in ids)
    assert stopped.status == RunStatus.INTERRUPTED and stopped.finished_at is not None
    assert crashed.status == RunStatus.FAILED


@pytest.mark.integration
def test_next_run_marks_dead_runs_of_its_lock_group_interrupted(clean_db: Engine) -> None:
    """Rows left `running` by a kill -9: the next run under the same lock sweeps them."""
    with clean_db.begin() as conn:
        dead_ingest = conn.execute(
            insert(PipelineRun).values(pipeline="ingest", trigger="manual").returning(PipelineRun.run_id)
        ).scalar_one()
        conn.execute(insert(PipelineRunSource).values(run_id=dead_ingest, source="hn"))  # mid-fetch
        other_lock = conn.execute(
            insert(PipelineRun).values(pipeline="enrich", trigger="manual").returning(PipelineRun.run_id)
        ).scalar_one()

    sweep = [Pipeline.INGEST, Pipeline.REBUILD]
    with run_ledger(clean_db, Pipeline.REBUILD, Trigger.MANUAL, {}, sweep=sweep):
        pass

    dead = run_row(clean_db, dead_ingest)
    assert dead.status == RunStatus.INTERRUPTED
    assert dead.finished_at is None  # unknown: nobody saw it die
    with clean_db.connect() as conn:
        source = conn.execute(select(PipelineRunSource).where(PipelineRunSource.run_id == dead_ingest)).one()
    assert (source.status, source.error_type) == ("failed", "Interrupted")
    # An enrich run holds a different lock: it may be alive, so it's left alone.
    assert run_row(clean_db, other_lock).status == RunStatus.RUNNING

# State machines

Four things have a lifecycle worth drawing: a source within an ingest run, a run, a posting, and a posting's enrichment.

## A source within a run

Stored in `pipeline_run_sources` as two columns: `status` (running, succeeded, failed) and `stage` (NULL, extracted, transformed). Each state below is one combination. Every arrow is a committed transaction, so a crash (even `kill -9`) leaves the row in the last state it reached, never in between.

```mermaid
stateDiagram-v2
    direction LR
    state "running · stage NULL<br/>fetching and loading raw" as Fetching
    state "running · extracted<br/>raw committed, transforming" as Extracted
    state "succeeded · transformed" as Succeeded
    state "failed · stage NULL<br/>nothing loaded" as FailedFetch
    state "failed · extracted<br/>raw kept, postings untouched" as FailedTransform

    [*] --> Fetching : run starts the source
    Fetching --> Extracted : raw rows + stage committed
    Fetching --> FailedFetch : HTTP error, bad shape,<br/>0 records, load error
    Extracted --> Succeeded : postings + counts committed
    Extracted --> FailedTransform : transform error,<br/>or more than 20% rejected
    FailedFetch --> Fetching : --resume (fetches again)
    FailedTransform --> Extracted : --resume (no fetch)
    Fetching --> Fetching : crash, then --resume
    Extracted --> Extracted : crash, then --resume
    Succeeded --> [*]
```

Not drawn: a source **inside its minimum fetch interval** (Remotive 6 h, Himalayas 20 h, Jobicy 1 h) is skipped before the run creates its row, and the run records it in `params.skipped`.

## A run

`pipeline_runs.status`, computed from its sources when the run ends.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> running : lock acquired, run row created
    running --> succeeded : every source succeeded
    running --> partial : some sources succeeded
    running --> failed : no source succeeded
    partial --> succeeded : --resume finishes the rest
    failed --> succeeded : --resume
    failed --> partial : --resume, some still fail
    running --> succeeded : crash, then --resume
    succeeded --> [*]
```

Only the **latest** ingest run can be resumed: a newer run has re-stamped the raw rows, so an older one would see an incomplete batch.

## A posting

Two independent dimensions: whether the job is still open (`is_active`), and how it relates to copies on other boards (`canonical_id`).

**Open or closed** (`is_active`):

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Active : first seen (inserted)
    Active --> Active : seen again (unchanged,<br/>or edited, so updated)
    Active --> Inactive : closed (see rules below)
    Inactive --> Active : seen again
```

**Cross-source identity** (`canonical_id`):

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Unlinked
    Unlinked --> Canonical : a copy on another board<br/>links to it (it's the oldest)
    Unlinked --> Duplicate : links to an older copy
    Canonical --> Duplicate : two groups join,<br/>the older canonical wins
```

**Closing rules** (the `Active → Inactive` arrow):

- GetOnBoard, Remotive, Himalayas, Jobicy: not returned by a **successful** extract of the source. These boards drop closed jobs from their APIs.
- Hacker News: the comment is more than 45 days old, or it was deleted from a thread fetched in that run.

**Identity rules:** links are never removed automatically, and a canonical stays canonical after it closes. Which copy represents the job is decided at read time by the `current_postings` view (the canonical if active, else the oldest active copy).

Code: [pipeline.py](../../src/jobradar/ingest/pipeline.py), [transform.py](../../src/jobradar/ingest/transform.py) (`_deactivate`), [link.py](../../src/jobradar/ingest/link.py).

## A posting's enrichment

The row in `posting_enrichments` (none at first), as `jobradar enrich` sees it. Only active postings are considered.

```mermaid
stateDiagram-v2
    direction LR
    state "no enrichment" as None
    state "ok · up to date" as Ok
    state "failed · retried next run" as Failed
    state "failed · skipped<br/>3 attempts on this input" as Skipped
    state "stale<br/>content or prompt changed" as Stale

    [*] --> None
    None --> Ok : valid answer
    None --> Failed : invalid twice, or refused
    None --> None : API error (not an attempt)
    Failed --> Ok : valid answer
    Failed --> Failed : fails again, attempts below 3
    Failed --> Skipped : third failed attempt
    Ok --> Stale : content_hash or PROMPT_VERSION changes
    Skipped --> Stale : content_hash or PROMPT_VERSION changes
    Stale --> Ok : valid answer
    Stale --> Failed : invalid twice, or refused
```

"ok" includes answers whose LATAM quote wasn't found in the posting: they are kept, with `latam_hiring` set to unknown (`llm_calls.status` = ungrounded).

Code: [enrich/pipeline.py](../../src/jobradar/enrich/pipeline.py) (`_is_stale`, `_upsert_failed`).

# Data flow

How postings travel from an API response to what search reads: ingestion, LLM enrichment, then indexing. Boxes are data; arrows are the code that moves it.

## Ingestion (one source in a run)

```mermaid
flowchart TB
    API[/"API response (JSON)"/]
    REC["RawRecord per posting<br/>src_posting_id · payload as received<br/>payload_hash (volatile fields ignored)"]
    STG[("stg_raw_postings<br/>TEMP table, ON COMMIT DROP")]
    RAW[("raw_postings<br/>one row per version")]
    SEEN["raw rows with last_run_id = this run<br/>(includes unchanged postings)"]
    VALID{"parse() →<br/>PostingIn valid?"}
    REJ[("rejected_postings<br/>payload + reason")]
    ROW["clean row<br/>+ content_hash · dedup_key"]
    POST[("postings<br/>one row per source + src_posting_id")]
    LINK["canonical_id set on duplicates"]
    VIEW[["current_postings<br/>one row per job"]]

    API -- "extract(): page, check shape,<br/>drop repeats within the batch" --> REC
    REC -- "COPY (bulk, one round trip)" --> STG
    STG -- "INSERT … ON CONFLICT:<br/>new version → new row<br/>known version → stamp last_run_id" --> RAW
    RAW -- "commit (stage: extracted)" --> SEEN
    SEEN --> VALID
    VALID -- "no" --> REJ
    VALID -- "yes" --> ROW
    ROW -- "upsert: insert new,<br/>rewrite only if content_hash changed" --> POST
    POST -- "stamp everything seen (active),<br/>deactivate what closed" --> POST
    POST -- "link: blocking + similarity<br/>+ union-find" --> LINK
    LINK --> VIEW
```

## Two hashes, two questions

| Hash | Computed over | Answers |
|---|---|---|
| `payload_hash` | the raw payload, minus volatile fields (e.g. GetOnBoard's `applications_count`) | "Is this a new *version* of the posting?" → a new row in `raw_postings` |
| `content_hash` | the parsed `PostingIn` fields | "Did anything we *keep* change?" → rewrite the row in `postings` |

A logo URL change makes a new raw version but no update in `postings`; an edited description changes both.

## Checks along the way

- **0 records** from a source fails it *before* the transform, so "not seen → inactive" can't close every posting.
- **More than 20% rejected** fails the source; the rejections are kept, `postings` is not touched, and `--resume` re-transforms after a fix.
- **Rejected new version of a known posting**: it was still listed, so its previous clean row stays active.

Code: [sources/base.py](../../src/jobradar/ingest/sources/base.py) (`RawRecord`, `payload_hash`), [pipeline.py](../../src/jobradar/ingest/pipeline.py) (`load_raw`), [posting.py](../../src/jobradar/ingest/posting.py) (`PostingIn`), [transform.py](../../src/jobradar/ingest/transform.py), [link.py](../../src/jobradar/ingest/link.py).

## Enrichment

What `jobradar enrich` does with each active posting whose enrichment is missing or stale.

```mermaid
flowchart TB
    POST[("postings<br/>active, enrichment missing or stale")]
    TEXT["posting text<br/>board fields + description"]
    LLM[/"LLM answer (JSON)<br/>constrained by the EnrichmentOut schema"/]
    VALID{"Pydantic<br/>valid?"}
    RETRY["one retry,<br/>with the validation error"]
    GROUND{"LATAM quote<br/>in the posting?"}
    OK[("posting_enrichments<br/>status ok")]
    FAIL[("posting_enrichments<br/>status failed, attempts + 1")]
    CALLS[("llm_calls<br/>every call: tokens, cost, outcome")]
    VIEW[["enriched_postings<br/>source values first, LLM fills gaps"]]

    POST -- "build_posting_text()" --> TEXT
    TEXT -- "instructions (cached) + posting" --> LLM
    LLM --> VALID
    VALID -- "no, first time" --> RETRY
    RETRY --> LLM
    VALID -- "no, after the retry" --> FAIL
    VALID -- "yes" --> GROUND
    GROUND -- "yes, or latam unknown" --> OK
    GROUND -- "no: latam set to unknown" --> OK
    LLM -. "logged" .-> CALLS
    OK --> VIEW
    OK -- "dedup_key (HN company + title):<br/>relink at the end of the run" --> LINKS["canonical_id<br/>on postings"]
```

- **Stale** means: no enrichment yet, the posting's `content_hash` changed, or `PROMPT_VERSION` changed. A failed enrichment is retried on later runs until it has 3 failed attempts on the same content and prompt.
- **API errors** (network, provider down) write no enrichment: the posting stays stale and the next run tries again. They're still logged in `llm_calls`.
- Each posting's calls and result are committed in one transaction.

Code: [enrich/pipeline.py](../../src/jobradar/enrich/pipeline.py), [enrich/schema.py](../../src/jobradar/enrich/schema.py), [enrich/grounding.py](../../src/jobradar/enrich/grounding.py), [llm/](../../src/jobradar/llm/).

## Indexing

What `jobradar index` does with each active posting whose chunks are missing or out of date.

```mermaid
flowchart TB
    POST[("postings + posting_enrichments<br/>active postings")]
    HEAD["header<br/>title at company · seniority · role · remote · LATAM<br/>Skills: …"]
    FP{"fingerprint changed?<br/>model, chunker version,<br/>content_hash, header"}
    SKIP["skip: chunks up to date"]
    CHUNKS["chunks of ≤ 300 tokens<br/>lines, then sentences; one line of overlap"]
    EMB[/"embeddinggemma (local)<br/>title: … | text: header + chunk"/]
    STORE[("posting_chunks<br/>text · tsv (simple + unaccent) · vector(768)")]

    POST -- "source values first, LLM fills gaps" --> HEAD
    HEAD --> FP
    FP -- "no" --> SKIP
    FP -- "yes" --> CHUNKS
    CHUNKS --> EMB
    EMB -- "delete old chunks, insert new:<br/>one transaction per batch" --> STORE
```

- Every active posting is indexed, not only each group's representative; search de-duplicates through `current_postings` at query time.
- Inactive postings keep their chunks (search filters them out), so a posting that reopens needs no re-embedding.

Code: [retrieval/indexer.py](../../src/jobradar/retrieval/indexer.py), [retrieval/chunk.py](../../src/jobradar/retrieval/chunk.py), [retrieval/embed.py](../../src/jobradar/retrieval/embed.py).

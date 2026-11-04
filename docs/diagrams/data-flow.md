# Data flow

How one source's postings travel through a run, from the API response to the view that later phases read. Boxes are data; arrows are the code that moves it.

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

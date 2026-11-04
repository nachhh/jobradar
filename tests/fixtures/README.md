# Test fixtures

API responses saved as files, so tests run offline and always see the same data. `respx` serves them in place of the real APIs.

| Folder | Origin |
|---|---|
| `getonboard/` | Recorded on 2026-11-03 from `categories/machine-learning-ai/jobs` with `expand[]`, trimmed to five postings. Split into two pages to exercise pagination; page 2 repeats one posting from page 1, to exercise in-batch deduplication. `jobs-empty.json` is the real shape of a category with no jobs. |
| `hn/` | Recorded on 2026-11-03 from the Algolia HN API: the `whoishiring` stories (with `_highlightResult` and `story_text` removed) and the November and October 2026 "Who is hiring?" threads, trimmed to a few top-level comments. The comment with id `50399999` is synthetic: it has the shape of a deleted comment. |
| `remotive/` | **Hand-written** from Remotive's API documentation, to spare its limit of 4 requests a day. To be replaced with real payloads from the first ingest run (`raw_postings`). |

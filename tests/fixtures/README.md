# Test fixtures

API responses saved as files, so tests run offline and always see the same data. `respx` serves them in place of the real APIs.

| Folder | Origin |
|---|---|
| `getonboard/` | Recorded on 2026-11-03 from `categories/machine-learning-ai/jobs` with `expand[]`, trimmed to five postings. Split into two pages to exercise pagination; page 2 repeats one posting from page 1, to exercise in-batch deduplication. `jobs-empty.json` is the real shape of a category with no jobs. |
| `hn/` | Recorded on 2026-11-03 from the Algolia HN API: the `whoishiring` stories (with `_highlightResult` and `story_text` removed) and the November and October 2026 "Who is hiring?" threads, trimmed to a few top-level comments. The comment with id `50399999` is synthetic: it has the shape of a deleted comment. |
| `remotive/` | **Hand-written** from Remotive's API documentation, to spare its limit of 4 requests a day. Checked against the first live response on 2026-11-04: same fields (the live one adds an unused `company_logo_url`). Kept hand-written on purpose, to cover cases such as an empty salary. |
| `himalayas/` | Recorded on 2026-11-04 from `jobs/api/search?country=AR`, pages 1 and 2, trimmed to three and two jobs; `totalCount` edited to 25 (two pages of 20) so the extractor stops after page 2. Mixes tech jobs with non-tech ones (Marketing, no category) to exercise the category filter. |
| `jobicy/` | Recorded on 2026-11-04 from `api/v2/remote-jobs?geo=latam` for `engineering` and `data-science`, trimmed. The engineering response is split into two pages linked by a made-up cursor (`CURSOR-PAGE-2`) to exercise cursor paging. `empty.json` keeps the real envelope with no jobs. Note: Jobicy omits the salary keys entirely when a job has no salary. |

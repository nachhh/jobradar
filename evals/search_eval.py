"""Mini retrieval eval (decision D6): which search variant puts relevant postings in its top 5?

    uv run python evals/search_eval.py pool    run the 4 variants, write the judgment file
    uv run python evals/search_eval.py score   read the marks, report precision@5 per variant

Concepts:
- **Pooling.** Judging every posting for every query is impossible (2,700 × 8), so only the
  postings some variant actually returned in its top 5 are judged: the union ("pool") of the four
  top-5 lists. A posting no variant found can't change any variant's precision@5, so judging the
  pool is enough to compare them. That's how search engines have been evaluated since the 1990s
  (TREC).
- **Precision@5:** of a variant's top 5, the share the human marked relevant. 0.8 means 4 of 5.
- **The variants:** keyword only, vector only, hybrid (both, fused with RRF), and hybrid + rerank.
  Comparing them is what turns "we added RRF and a reranker" into a measured result.

The queries (the dataset) are in evals/search_queries.json. The human's marks are saved to
evals/search_judgments.json (committed): a seed for Phase 4's golden set. The judgment file itself
goes to evals/review/ (git-ignored), like the enrichment spot check.
"""

import argparse
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import text

from jobradar.config import get_settings
from jobradar.db import get_engine
from jobradar.retrieval.embed import FastEmbedder
from jobradar.retrieval.rerank import FastReranker
from jobradar.retrieval.search import SearchFilters, search

HERE = Path(__file__).parent
QUERIES = HERE / "search_queries.json"
POOL = HERE / "review" / "search-pool.json"  # which variant returned what, for scoring
REVIEW = HERE / "review" / "search-judgments.md"
JUDGMENTS = HERE / "search_judgments.json"

K = 5
VARIANTS = ("keyword", "vector", "hybrid", "hybrid+rerank")


def pool() -> None:
    settings = get_settings()
    engine = get_engine()
    embedder = FastEmbedder(cache_dir=settings.fastembed_cache_path)
    reranker = FastReranker(cache_dir=settings.fastembed_cache_path)
    queries = json.loads(QUERIES.read_text())
    results: list[dict[str, Any]] = []
    for entry in queries:
        filters = SearchFilters(latam=entry.get("latam", False), remote=entry.get("remote"))
        ranked = {
            "keyword": search(engine, entry["query"], embedder, filters=filters, limit=K, mode="keyword"),
            "vector": search(engine, entry["query"], embedder, filters=filters, limit=K, mode="vector"),
            "hybrid": search(engine, entry["query"], embedder, filters=filters, limit=K),
            "hybrid+rerank": search(
                engine, entry["query"], embedder, filters=filters, limit=K, reranker=reranker
            ),
        }
        results.append(
            {
                "query": entry["query"],
                "ranked": {v: [h.posting_id for h in hits] for v, hits in ranked.items()},
            }
        )
        pooled = {i for ids in results[-1]["ranked"].values() for i in ids}
        print(f"{entry['query']!r}: {len(pooled)} postings to judge")
    POOL.parent.mkdir(exist_ok=True)
    POOL.write_text(json.dumps(results, indent=2))
    _write_review(engine, results)


def _write_review(engine: Any, results: list[dict[str, Any]]) -> None:
    ids = sorted({i for r in results for ranked in r["ranked"].values() for i in ranked})
    with engine.connect() as conn:
        info = {
            row.id: row
            for row in conn.execute(
                text("""
                    SELECT p.id, p.source, COALESCE(p.title, e.title) AS title,
                           COALESCE(p.company, e.company) AS company, e.summary, e.seniority, p.url
                    FROM postings p LEFT JOIN posting_enrichments e ON e.posting_id = p.id
                    WHERE p.id = ANY(:ids)
                """),
                {"ids": ids},
            )
        }
    lines = [
        "# Search judgments",
        "",
        "For each query, put an `x` in **Relevant** for every posting that is a good result for it",
        "(you'd want to see it for that search). Leave the others blank. The order is by id, not by",
        "rank, so you can't tell which variant found what.",
        "",
    ]
    for n, result in enumerate(results, 1):
        pooled = sorted({i for ranked in result["ranked"].values() for i in ranked})
        lines += [
            f"## {n}. {result['query']}",
            "",
            "| Posting | Job | Summary | Relevant |",
            "|---|---|---|---|",
        ]
        for posting_id in pooled:
            row = info[posting_id]
            job = f"[{row.title or '(untitled)'}]({row.url}) at {row.company or '?'}"
            job += f" ({row.source}, {row.seniority})"
            summary = (row.summary or "").replace("|", "/")
            lines.append(f"| {posting_id} | {job.replace('|', '/')} | {summary} |  |")
        lines.append("")
    REVIEW.write_text("\n".join(lines))
    print(f"judgment file: {REVIEW}")


# A table row: | posting_id | job | summary | mark |
ROW = re.compile(r"^\| (\d+) \| .* \| *(x?) *\|$", re.IGNORECASE)
QUERY = re.compile(r"^## \d+\. (.+)$")


def score() -> None:
    results = json.loads(POOL.read_text())
    marks: dict[str, dict[int, bool]] = {}
    query = None
    for line in REVIEW.read_text().splitlines():
        if match := QUERY.match(line):
            query = match.group(1)
            marks[query] = {}
        elif (match := ROW.match(line)) and query is not None:
            marks[query][int(match.group(1))] = match.group(2).lower() == "x"

    totals = dict.fromkeys(VARIANTS, 0.0)
    print(f"{'query':<45}" + "".join(f"{v:>15}" for v in VARIANTS))
    for result in results:
        relevant = marks[result["query"]]
        row = {v: sum(relevant.get(i, False) for i in result["ranked"][v][:K]) / K for v in VARIANTS}
        for v in VARIANTS:
            totals[v] += row[v]
        print(f"{result['query'][:44]:<45}" + "".join(f"{row[v]:>15.2f}" for v in VARIANTS))
    print(f"{'mean precision@5':<45}" + "".join(f"{totals[v] / len(results):>15.2f}" for v in VARIANTS))

    judgments = [
        {"query": result["query"], "posting_id": posting_id, "relevant": is_relevant}
        for result in results
        for posting_id, is_relevant in sorted(marks[result["query"]].items())
    ]
    JUDGMENTS.write_text(json.dumps(judgments, indent=2) + "\n")
    print(f"{len(judgments)} judgments saved to {JUDGMENTS}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["pool", "score"])
    args = parser.parse_args()
    pool() if args.command == "pool" else score()

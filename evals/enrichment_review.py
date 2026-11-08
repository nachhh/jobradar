"""Human spot check of LLM enrichments: a small eval with a person as the judge.

    uv run python evals/enrichment_review.py sample [--per-source 4]   write the review file
    uv run python evals/enrichment_review.py score                      score the marked file

`sample` picks random enriched postings (the same number from each source) and writes a Markdown
file with each posting's text next to the extracted fields. The reviewer puts an `x` in the
"Wrong" column of every field the LLM got wrong (and may add a note). `score` reads the marks back
and reports accuracy overall, per field, and for latam_hiring.

The review file is written to evals/review/, which is git-ignored: it contains posting texts, and
HN posts include people's email addresses. Only the scores are recorded in the spec.
"""

import argparse
import re
from pathlib import Path

from sqlalchemy import text

from jobradar.db import get_engine
from jobradar.enrich.prompt import build_posting_text

REVIEW_FILE = Path(__file__).parent / "review" / "enrichment-spot-check.md"

# Fields shown for grading, in reading order. Salary fields are graded together, as one figure.
FIELDS = (
    "company",
    "title",
    "seniority",
    "role_family",
    "employment_type",
    "remote",
    "latam_hiring",
    "location_restrictions",
    "skills_required",
    "skills_nice",
    "years_experience_min",
    "languages",
    "salary",
    "salary_basis",
    "summary",
)

# Random sample, the same number per source: row_number() over a random order within each source.
SAMPLE_SQL = text("""
    SELECT * FROM (
        SELECT p.*, e.company AS e_company, e.title AS e_title, e.seniority AS e_seniority,
               e.role_family, e.employment_type, e.remote AS e_remote, e.latam_hiring,
               e.latam_evidence, e.location_restrictions, e.skills_required, e.skills_nice,
               e.years_experience_min, e.languages, e.salary_min AS e_salary_min,
               e.salary_max AS e_salary_max, e.salary_currency AS e_salary_currency,
               e.salary_period AS e_salary_period, e.salary_basis, e.summary,
               row_number() OVER (PARTITION BY p.source ORDER BY random()) AS n
        FROM posting_enrichments e JOIN postings p ON p.id = e.posting_id
        WHERE e.status = 'ok'
    ) s
    WHERE n <= :per_source
    ORDER BY source, id
""")


def _value(row: dict[str, object], field: str) -> str:
    """The extracted value as shown to the reviewer."""
    if field == "salary":
        low, high = row["e_salary_min"], row["e_salary_max"]
        if low is None and high is None:
            return "—"
        unit = " ".join(str(v) for v in (row["e_salary_currency"], row["e_salary_period"]) if v)
        return f"{low}–{high} {unit}"
    if field == "latam_hiring":
        evidence = row["latam_evidence"]
        return f'{row["latam_hiring"]} (quote: "{evidence}")' if evidence else str(row["latam_hiring"])
    key = f"e_{field}" if f"e_{field}" in row else field
    value = row[key]
    if isinstance(value, list):
        return ", ".join(value) or "—"
    return "—" if value is None else str(value).replace("|", "/")


def sample(per_source: int) -> None:
    with get_engine().connect() as conn:
        rows = [dict(r) for r in conn.execute(SAMPLE_SQL, {"per_source": per_source}).mappings()]
    lines = [
        "# Enrichment spot check",
        "",
        "For each posting, read the text, then put an `x` in **Wrong** for every field the LLM got",
        'wrong. Leave correct fields blank. A short note helps (e.g. `x` + "should be senior").',
        'Judge against the posting only: "unknown" or "—" is correct when the posting doesn\'t say.',
        "",
    ]
    for i, row in enumerate(rows, 1):
        title = row["title"] or row["e_title"] or "(no title)"
        lines += [
            f"## {i}. {row['source']}: {title} (posting {row['id']})",
            "",
            "<details><summary>Posting text</summary>",
            "",
            "```text",
            build_posting_text(row).replace("```", "'''"),
            "```",
            "",
            "</details>",
            "",
            "| Field | Extracted | Wrong | Note |",
            "|---|---|---|---|",
        ]
        lines += [f"| {f} | {_value(row, f)} |  |  |" for f in FIELDS]
        lines.append("")
    REVIEW_FILE.parent.mkdir(exist_ok=True)
    REVIEW_FILE.write_text("\n".join(lines))
    print(f"{len(rows)} postings written to {REVIEW_FILE}")


# A table row of the review file: | field | extracted | wrong | note |
ROW = re.compile(r"^\| (\w+) \| .* \| *(x?) *\| *(.*?) *\|$", re.IGNORECASE)


def score() -> None:
    graded = wrong = 0
    per_field: dict[str, list[int]] = {f: [0, 0] for f in FIELDS}  # field -> [wrong, total]
    notes: list[str] = []
    for line in REVIEW_FILE.read_text().splitlines():
        match = ROW.match(line)
        if not match or match.group(1) not in per_field:
            continue
        field, mark, note = match.group(1), match.group(2).lower() == "x", match.group(3)
        graded += 1
        wrong += mark
        per_field[field][0] += mark
        per_field[field][1] += 1
        if mark and note:
            notes.append(f"{field}: {note}")
    postings = per_field["summary"][1]
    accuracy = 100 * (graded - wrong) / graded
    print(f"{postings} postings, {graded} fields graded, {wrong} wrong: {accuracy:.1f}% correct")
    for field, (bad, total) in per_field.items():
        if bad:
            print(f"  {field:<22} {bad}/{total} wrong")
    latam_bad, latam_total = per_field["latam_hiring"]
    print(f"latam_hiring correct: {latam_total - latam_bad}/{latam_total}")
    for note in notes:
        print(f"  note: {note}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sample_cmd = sub.add_parser("sample")
    sample_cmd.add_argument("--per-source", type=int, default=4)
    sub.add_parser("score")
    args = parser.parse_args()
    if args.command == "sample":
        sample(args.per_source)
    else:
        score()

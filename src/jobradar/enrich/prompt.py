"""The enrichment prompt: the instructions (system message) and the posting text (user message).

PROMPT_VERSION identifies this prompt *and* the EnrichmentOut schema. Changing either one without
bumping it would leave old enrichments looking up to date; bumping it makes every enrichment stale,
so the next run re-enriches everything (a full pass, about $1 with Claude Haiku). Bump on purpose.
"""

from collections.abc import Mapping
from typing import Any

PROMPT_VERSION = "v1"

# Written as rules, one per field where it matters. The model also sees the field descriptions in
# the schema; these instructions cover the judgement calls the schema can't express.
INSTRUCTIONS = """\
You extract structured facts from one job posting, for a job-search tool.

General rules:
- Use only what the posting states. When it doesn't say, answer "unknown", null or an empty list.
  Don't infer from the company's reputation or from what is typical for such roles.
- Postings may be in Spanish, English or both. Keep company and title as written; write the
  summary in English.
- The board's fields (title, location, tags, salary) appear above the description and are part
  of the posting.

Fields:
- company, title: as written. Hacker News posts usually start with "Company | Role | Location | ...".
- seniority: from the title or explicit wording ("Semi Senior"/"SSr" = mid, "Sr" = senior,
  "Tech Lead"/"Lead" = lead, "Staff"/"Principal" = principal, "Trainee"/"Pasante" = intern).
  Otherwise unknown.
- role_family: the main function. ai_training is labelling data or writing and rating examples to
  train AI models (often contract work titled "AI Trainer" or "Coding Expert for AI"), not building
  AI systems; building ML or LLM systems is ml_ai_engineering.
- remote: remote = can work fully remotely; hybrid = some days in an office; onsite = office only.
- latam_hiring: can people located in Latin America be hired?
  yes: open to Argentina, Latin America (LATAM), South America, the Americas, or anywhere/worldwide
  without excluding Latin America; or the job is based in a Latin American country.
  no: restricted to places that exclude Latin America (e.g. "US only", "must be based in the EU",
  "must be authorized to work in the US").
  unknown: anything else.
- latam_evidence: copy, character for character, the sentence or phrase that supports latam_hiring
  (it is checked against the posting). null when latam_hiring is unknown.
- location_restrictions: countries or regions candidates must be in, if any.
- skills_required, skills_nice: technologies, languages, tools and methods named in the posting
  (e.g. python, spark, airflow, aws, kubernetes, sql, dbt), lowercase, common names, no soft
  skills, at most 15 each. Nice to have: "deseable", "plus", "bonus", "nice to have".
- years_experience_min: the minimum years of experience required, if stated.
- languages: spoken languages required (english, spanish, portuguese...).
- salary_*: only amounts the posting states, as written (no conversion), with the ISO currency
  code and the period. salary_basis: net if after taxes ("líquido", "neto", "net"), gross if
  before taxes ("bruto", "gross"), else unknown.
- summary: one sentence, at most 25 words: what the role does and its main stack.
"""


def _salary_line(posting: Mapping[str, Any]) -> str | None:
    """The board's structured salary, e.g. "4000-5000 USD per month", or its free text."""
    low, high = posting.get("salary_min"), posting.get("salary_max")
    if low is None and high is None:
        return posting.get("salary_text")
    amount = (
        f"{low}-{high}" if low is not None and high is not None else str(low if low is not None else high)
    )
    unit = " ".join(str(v) for v in (posting.get("salary_currency"), posting.get("salary_period")) if v)
    return f"{amount} {unit}".strip()


def build_posting_text(posting: Mapping[str, Any]) -> str:
    """The user message: the board's fields that are present, then the description.

    `posting` is a row of `postings` (any mapping with its column names). Empty fields are left out
    rather than written as "None", which the model could mistake for a value.
    """
    tags = ", ".join(posting.get("tags") or [])
    fields = {
        "Board": posting.get("source"),
        "Title": posting.get("title"),
        "Company": posting.get("company"),
        "Location": posting.get("location_text"),
        "Remote (board)": None if posting.get("remote") in (None, "unknown") else posting.get("remote"),
        "Tags": tags or None,
        "Salary (board)": _salary_line(posting),
    }
    header = "\n".join(f"{label}: {value}" for label, value in fields.items() if value)
    return f"{header}\n\nDescription:\n{posting['description']}"

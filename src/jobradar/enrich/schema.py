"""EnrichmentOut: the "form" the LLM fills in for each posting, and the rules its answer must pass.

The same Pydantic class does two jobs:
1. Its JSON schema is sent to the provider as the required output format (structured output), so
   the model answers with exactly these fields and these allowed values. Field descriptions are
   part of that schema: the model reads them, so they work as per-field instructions.
2. It validates the answer that comes back. The schema sent to the API can't express every rule
   (e.g. salary_min <= salary_max), and models make mistakes, so nothing is trusted unvalidated.

Validators here *normalize* where a fix is unambiguous ("K8s" → "kubernetes", "usd" → "USD") and
*reject* only what can't be repaired (an inverted salary range). A rejection costs a retry.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jobradar.models import (
    EmploymentType,
    LatamHiring,
    Remote,
    RoleFamily,
    SalaryBasis,
    SalaryPeriod,
    Seniority,
)

# Different spellings of the same skill, mapped to one name, so that search and filters treat them
# as one. Keys are lowercase. A small hand-made table: it covers the common variants seen in the
# postings; the prompt also asks for common names, which handles most of the rest.
SKILL_ALIASES: dict[str, str] = {
    "k8s": "kubernetes",
    "postgres": "postgresql",
    "psql": "postgresql",
    "golang": "go",
    "js": "javascript",
    "ts": "typescript",
    "node": "node.js",
    "nodejs": "node.js",
    "react.js": "react",
    "reactjs": "react",
    "vue.js": "vue",
    "vuejs": "vue",
    "next": "next.js",
    "nextjs": "next.js",
    "py": "python",
    "python3": "python",
    "amazon web services": "aws",
    "google cloud": "gcp",
    "google cloud platform": "gcp",
    "microsoft azure": "azure",
    "apache spark": "spark",
    "pyspark": "spark",
    "apache airflow": "airflow",
    "apache kafka": "kafka",
    "scikit learn": "scikit-learn",
    "sklearn": "scikit-learn",
    "ci/cd": "ci-cd",
    "cicd": "ci-cd",
    "llms": "llm",
    "large language models": "llm",
    "ms sql": "sql server",
    "mssql": "sql server",
}


def _normalize_skills(values: list[str]) -> list[str]:
    """Lowercase, trim, apply aliases, drop empties and duplicates (keeping the first order)."""
    seen: dict[str, None] = {}  # a dict keeps insertion order, unlike a set
    for value in values:
        skill = " ".join(value.lower().split())
        skill = SKILL_ALIASES.get(skill, skill)
        if skill:
            seen[skill] = None
    return list(seen)


def _clean_list(values: list[str]) -> list[str]:
    """Trim and drop empties and duplicates; case kept (country names, as written)."""
    seen: dict[str, None] = {}
    for value in values:
        if cleaned := " ".join(value.split()):
            seen[cleaned] = None
    return list(seen)


# Annotated attaches extra rules to a type; ge=0 means "greater than or equal to 0". Constraints
# like this are dropped from the schema sent to Claude (its API can't enforce them) but still
# checked by our validation.
NonNegativeInt = Annotated[int, Field(ge=0)]


class EnrichmentOut(BaseModel):
    """The fields extracted from one posting. Every field is required in the answer; "not stated"
    is expressed as null, an empty list or "unknown", never by leaving a field out."""

    # extra="forbid": unknown fields are an error. It also puts additionalProperties: false in the
    # JSON schema, which structured output requires.
    model_config = ConfigDict(extra="forbid")

    company: str | None = Field(description="Hiring company as written in the posting; null if not stated.")
    title: str | None = Field(description="Job title as written in the posting; null if not stated.")
    seniority: Seniority
    role_family: RoleFamily = Field(description="The main function of the role.")
    employment_type: EmploymentType
    remote: Remote
    latam_hiring: LatamHiring = Field(
        description="Can people located in Latin America be hired for this job?"
    )
    latam_evidence: str | None = Field(
        description="The exact sentence or phrase from the posting that supports latam_hiring, copied "
        "character for character; null when latam_hiring is unknown."
    )
    location_restrictions: list[str] = Field(description="Countries or regions candidates must be in.")
    skills_required: list[str] = Field(
        description="Technologies and tools required, lowercase, common names."
    )
    skills_nice: list[str] = Field(description="Technologies and tools listed as nice to have.")
    years_experience_min: NonNegativeInt | None = Field(description="Minimum years of experience required.")
    languages: list[str] = Field(description="Spoken languages required, lowercase (e.g. english).")
    salary_min: NonNegativeInt | None
    salary_max: NonNegativeInt | None
    salary_currency: str | None = Field(description="ISO 4217 code, e.g. USD, CLP, ARS.")
    salary_period: SalaryPeriod | None
    salary_basis: SalaryBasis = Field(
        description="Whether the salary is before (gross) or after (net) taxes."
    )
    summary: str = Field(description="One sentence in English: what the role does and its main stack.")

    # mode="after": these run on values that already passed the type checks above.
    @field_validator("company", "title", "latam_evidence")
    @classmethod
    def _blank_is_null(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return " ".join(value.split()) or None

    @field_validator("skills_required", "skills_nice")
    @classmethod
    def _skills(cls, values: list[str]) -> list[str]:
        return _normalize_skills(values)

    @field_validator("languages")
    @classmethod
    def _languages(cls, values: list[str]) -> list[str]:
        return _clean_list([v.lower() for v in values])  # lowercase first, so duplicates collapse

    @field_validator("location_restrictions")
    @classmethod
    def _locations(cls, values: list[str]) -> list[str]:
        return _clean_list(values)

    @field_validator("salary_currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        """Uppercase a 3-letter code; anything else ("dollars", "$") isn't a code, so it becomes null."""
        if value is None:
            return None
        code = value.strip().upper()
        return code if len(code) == 3 and code.isalpha() else None

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: str) -> str:
        summary = " ".join(value.split())
        if not summary:
            raise ValueError("summary is empty")
        return summary

    @model_validator(mode="after")
    def _salary_range(self) -> "EnrichmentOut":
        """An inverted range can't be repaired safely (which number is wrong?), so it's rejected."""
        if self.salary_min is not None and self.salary_max is not None and self.salary_min > self.salary_max:
            raise ValueError(f"salary_min {self.salary_min} > salary_max {self.salary_max}")
        return self

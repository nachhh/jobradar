"""Unit tests for the enrichment schema, the grounding check and the prompt text."""

from typing import Any

import pytest
from pydantic import ValidationError

from jobradar.enrich.grounding import apply_grounding, is_grounded
from jobradar.enrich.prompt import build_posting_text
from jobradar.enrich.schema import EnrichmentOut
from jobradar.models import LatamHiring, RoleFamily, Seniority


def answer(**overrides: Any) -> dict[str, Any]:
    """A complete, valid answer as the LLM would return it, with optional overrides."""
    base: dict[str, Any] = {
        "company": "Acme",
        "title": "Senior Data Engineer",
        "seniority": "senior",
        "role_family": "data_engineering",
        "employment_type": "full_time",
        "remote": "remote",
        "latam_hiring": "yes",
        "latam_evidence": "Open to candidates in LATAM.",
        "location_restrictions": [],
        "skills_required": ["Python", "Spark"],
        "skills_nice": [],
        "years_experience_min": 5,
        "languages": ["English"],
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "salary_period": None,
        "salary_basis": "unknown",
        "summary": "Builds batch pipelines with Spark.",
    }
    return base | overrides


def test_valid_answer_parses_into_enums() -> None:
    out = EnrichmentOut.model_validate(answer())
    assert out.seniority is Seniority.SENIOR
    assert out.role_family is RoleFamily.DATA_ENGINEERING


def test_skills_are_normalized() -> None:
    out = EnrichmentOut.model_validate(
        answer(skills_required=["K8s", " Postgres ", "kubernetes", "PySpark", ""])
    )
    # Aliases applied, case and spaces normalized, duplicates ("k8s" = "kubernetes") and empties dropped.
    assert out.skills_required == ["kubernetes", "postgresql", "spark"]


def test_currency_is_normalized_or_dropped() -> None:
    assert EnrichmentOut.model_validate(answer(salary_currency=" usd ")).salary_currency == "USD"
    assert EnrichmentOut.model_validate(answer(salary_currency="dollars")).salary_currency is None


def test_blank_text_becomes_null() -> None:
    out = EnrichmentOut.model_validate(answer(company="  ", languages=["English", "english "]))
    assert out.company is None
    assert out.languages == ["english"]


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"salary_min": 5000, "salary_max": 3000}, "salary_min 5000 > salary_max 3000"),
        ({"seniority": "rockstar"}, "seniority"),  # not one of the allowed values
        ({"salary_min": -1}, "greater than or equal to 0"),
        ({"summary": "   "}, "summary is empty"),
        ({"bonus": "yes"}, "Extra inputs are not permitted"),  # extra="forbid"
    ],
)
def test_invalid_answers_are_rejected(overrides: dict[str, Any], error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        EnrichmentOut.model_validate(answer(**overrides))


def test_missing_field_is_rejected() -> None:
    """Every field is required: "not stated" must be said explicitly (null, [] or "unknown")."""
    data = answer()
    del data["salary_basis"]
    with pytest.raises(ValidationError, match="salary_basis"):
        EnrichmentOut.model_validate(data)


POSTING = (
    "Title: Data Engineer\n\nDescription:\n"
    "We hire “anywhere in the Americas” — fully remote.\nPython required."
)


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ('We hire "anywhere in the Americas" - fully remote.', True),  # plain quotes and dash still match
        ("WE HIRE   anywhere in the americas", True),  # case and spacing don't matter
        ("We hire … fully remote", True),  # each fragment around an ellipsis is present
        ("We hire anywhere in LATAM", False),  # sounds plausible, isn't in the posting
        ("", False),
        (None, False),
    ],
)
def test_is_grounded(evidence: str | None, expected: bool) -> None:
    assert is_grounded(evidence, POSTING) is expected


def test_ungrounded_answer_is_downgraded_to_unknown() -> None:
    out = EnrichmentOut.model_validate(answer(latam_evidence="Open to LATAM."))
    kept, grounded = apply_grounding(out, POSTING)
    assert grounded is False
    assert kept.latam_hiring is LatamHiring.UNKNOWN
    assert out.latam_hiring is LatamHiring.YES  # the original isn't modified


def test_unknown_needs_no_evidence() -> None:
    out = EnrichmentOut.model_validate(answer(latam_hiring="unknown", latam_evidence=None))
    assert apply_grounding(out, POSTING) == (out, True)


def test_posting_text_lists_present_fields_only() -> None:
    posting = {
        "source": "getonboard",
        "title": "Data Engineer",
        "company": None,  # left out, not written as "None"
        "location_text": "Santiago",
        "remote": "unknown",  # "unknown" adds nothing: left out
        "seniority": "Semi Senior",
        "tags": ["python", "spark"],
        "salary_min": 2000,
        "salary_max": 3000,
        "salary_currency": "USD",
        "salary_period": "month",
        "description": "Build pipelines.",
    }
    assert build_posting_text(posting) == (
        "Board: getonboard\nTitle: Data Engineer\nLocation: Santiago\nSeniority (board): Semi Senior\n"
        "Tags: python, spark\n"
        "Salary (board): 2000-3000 USD month\n\nDescription:\nBuild pipelines."
    )

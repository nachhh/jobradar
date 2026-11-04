"""PostingIn: the one shape every source's parser must produce (a data contract).

Pydantic validates on construction: a wrong type, a missing required field or a broken rule raises
ValidationError, which the transform catches and sends to rejected_postings with the message.
So whatever reaches the postings table has passed these checks, whichever source it came from.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from jobradar.ingest.text import dedup_key
from jobradar.models import Remote, Source


class PostingIn(BaseModel):
    model_config = ConfigDict(
        frozen=True,  # immutable after validation: nothing downstream can quietly change a field
        extra="forbid",  # a misspelled field name is an error, not silently ignored
        str_strip_whitespace=True,
    )

    source: Source
    src_posting_id: str = Field(min_length=1)
    url: str
    title: str | None = None
    company: str | None = None
    location_text: str | None = None
    remote: Remote = Remote.UNKNOWN
    seniority: str | None = None
    # ge=0: "greater or equal to 0". GetOnBoard salaries are USD per month; the other sources only
    # have free-text salaries (salary_text), parsed by the LLM in Phase 2.
    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)
    salary_text: str | None = None
    # AwareDatetime rejects a datetime without a time zone: "10:15" alone is ambiguous, and the
    # column is TIMESTAMPTZ. Parsers attach UTC when the source omits it.
    posted_at: AwareDatetime | None = None
    description: str = Field(min_length=1)
    tags: tuple[str, ...] = ()  # a tuple, not a list, because the model is frozen

    @field_validator("title", "company", "location_text", "seniority", "salary_text")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        # "" and None mean the same thing ("not given"); storing one of them keeps queries simple.
        return value or None

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("url must start with http:// or https://")
        return value

    @model_validator(mode="after")
    def _salary_range(self) -> Self:
        # mode="after": runs once every field has been validated, so it can compare two of them.
        if self.salary_min is not None and self.salary_max is not None and self.salary_min > self.salary_max:
            raise ValueError(f"salary_min {self.salary_min} > salary_max {self.salary_max}")
        return self

    def content_hash(self) -> str:
        """SHA-256 of the parsed fields: the upsert rewrites a posting only when this changes.

        It hashes the *parsed* fields, unlike raw's payload_hash: a payload change in a field we
        don't keep (a logo URL) is a new raw version but not a change to the clean posting.
        """
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_row(self) -> dict[str, object]:
        """Column values for the postings table, including the derived ones."""
        return {
            **self.model_dump(),
            "tags": list(self.tags),
            "content_hash": self.content_hash(),
            "dedup_key": dedup_key(self.company, self.title),
        }


def utc_from_timestamp(seconds: int | float) -> datetime:
    """A Unix timestamp (seconds since 1970-01-01 UTC) as a time-zone-aware datetime."""
    return datetime.fromtimestamp(seconds, UTC)

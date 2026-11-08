"""Helpers shared by the model modules. The leading underscore marks the module as internal."""

from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime


def one_of(column: str, values: type[StrEnum]) -> CheckConstraint:
    """CHECK (column IN ('a', 'b', …)), built from the enum so the two never disagree.

    The values come from our own enums, never from user input, so formatting them into SQL is safe.
    A CHECK passes when its expression is NULL, so on a nullable column NULL stays allowed.
    """
    allowed = ", ".join(f"'{v.value}'" for v in values)
    # `name` fills %(constraint_name)s in the naming convention: ck_<table>_<column>.
    return CheckConstraint(f"{column} IN ({allowed})", name=column)


# Columns reused by several tables. server_default=now() makes Postgres fill the value, so rows
# inserted with plain SQL (or by hand in psql) get it too, not only rows inserted through the ORM.
# timezone=True → TIMESTAMPTZ: stored as UTC and unambiguous; plain TIMESTAMP has no time zone.
TIMESTAMPTZ = DateTime(timezone=True)

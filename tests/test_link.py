"""Integration tests for cross-source linking (stage 3) and the current_postings view."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Connection, insert, select, text, update

from jobradar.ingest.link import UnionFind, link_duplicates
from jobradar.ingest.text import dedup_key
from jobradar.models import PipelineRun, Posting, Source

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def run_id(db_conn: Connection) -> uuid.UUID:
    stmt = insert(PipelineRun).values(pipeline="ingest", trigger="manual").returning(PipelineRun.run_id)
    return db_conn.execute(stmt).scalar_one()


def add(
    conn: Connection,
    run_id: uuid.UUID,
    source: Source,
    company: str | None,
    title: str,
    *,
    days: int = 0,
    active: bool = True,
) -> int:
    """Insert a posting first seen `days` after T0; return its id."""
    stmt = (
        insert(Posting)
        .values(
            source=source,
            src_posting_id=uuid.uuid4().hex,
            url="https://example.com/job",
            company=company,
            title=title,
            description="…",
            content_hash="x",
            dedup_key=dedup_key(company, title),
            first_seen_at=T0 + timedelta(days=days),
            last_run_id=run_id,
            is_active=active,
        )
        .returning(Posting.id)
    )
    return conn.execute(stmt).scalar_one()


def canonical_of(conn: Connection, posting_id: int) -> int | None:
    return conn.execute(select(Posting.canonical_id).where(Posting.id == posting_id)).scalar_one()


def current(conn: Connection) -> list[int]:
    return list(conn.execute(text("SELECT id FROM current_postings ORDER BY group_id")).scalars())


# --- union-find (pure Python) -----------------------------------------------------------------


def test_union_find_is_transitive() -> None:
    sets = UnionFind()
    sets.union(1, 2)
    sets.union(2, 3)
    sets.union(7, 8)
    assert sorted(sorted(g) for g in sets.groups()) == [[1, 2, 3], [7, 8]]


# --- which pairs link -------------------------------------------------------------------------


def test_exact_match_across_sources_links_to_the_oldest(db_conn: Connection, run_id: uuid.UUID) -> None:
    newer = add(db_conn, run_id, Source.REMOTIVE, "Acme Analytics Inc.", "Senior Data Engineer", days=5)
    older = add(db_conn, run_id, Source.GETONBOARD, "ACME Analytics", "Senior Data Engineer", days=1)

    counts = link_duplicates(db_conn)

    assert (counts.exact_pairs, counts.groups, counts.changed) == (1, 1, 1)
    assert canonical_of(db_conn, newer) == older
    assert canonical_of(db_conn, older) is None


def test_fuzzy_match_links_spelling_variants(db_conn: Connection, run_id: uuid.UUID) -> None:
    a = add(db_conn, run_id, Source.GETONBOARD, "Nimbus Labs", "Senior Data Engineer")
    b = add(db_conn, run_id, Source.REMOTIVE, "Nimbus Lab", "Data Engineer, Senior", days=1)

    counts = link_duplicates(db_conn)

    assert (counts.exact_pairs, counts.fuzzy_pairs) == (0, 1)
    assert canonical_of(db_conn, b) == a


@pytest.mark.parametrize(
    ("company_b", "title_b", "source_b"),
    [
        ("Umbrella Corp", "Senior Data Engineer", Source.REMOTIVE),  # same title, other company
        ("Acme Analytics", "Senior Data Analyst", Source.REMOTIVE),  # same company, other role
        ("Acme Analytics", "Senior Data Engineer", Source.GETONBOARD),  # same source
        (None, "Senior Data Engineer", Source.HN),  # no company (HN before Phase 2)
    ],
)
def test_non_duplicates_are_not_linked(
    db_conn: Connection, run_id: uuid.UUID, company_b: str | None, title_b: str, source_b: Source
) -> None:
    add(db_conn, run_id, Source.GETONBOARD, "Acme Analytics", "Senior Data Engineer")
    add(db_conn, run_id, source_b, company_b, title_b, days=1)

    assert link_duplicates(db_conn).groups == 0


def test_linking_twice_changes_nothing(db_conn: Connection, run_id: uuid.UUID) -> None:
    add(db_conn, run_id, Source.GETONBOARD, "Acme", "Data Engineer")
    add(db_conn, run_id, Source.REMOTIVE, "Acme", "Data Engineer", days=1)
    link_duplicates(db_conn)

    assert link_duplicates(db_conn).changed == 0


# --- canonical is fixed identity --------------------------------------------------------------


def test_new_copy_joins_the_existing_canonical_even_if_closed(db_conn: Connection, run_id: uuid.UUID) -> None:
    canonical = add(db_conn, run_id, Source.GETONBOARD, "Acme", "Data Engineer")
    first_copy = add(db_conn, run_id, Source.REMOTIVE, "Acme", "Data Engineer", days=1)
    link_duplicates(db_conn)
    db_conn.execute(update(Posting).where(Posting.id == canonical).values(is_active=False))

    later_copy = add(db_conn, run_id, Source.HN, "Acme", "Data Engineer", days=2)
    link_duplicates(db_conn)

    assert canonical_of(db_conn, later_copy) == canonical  # not re-pointed to an active member
    assert canonical_of(db_conn, first_copy) == canonical


def test_joining_two_groups_keeps_the_older_canonical(db_conn: Connection, run_id: uuid.UUID) -> None:
    old_canon = add(db_conn, run_id, Source.GETONBOARD, "Acme", "Data Engineer")
    old_member = add(db_conn, run_id, Source.REMOTIVE, "Acme", "Data Engineer", days=1)
    # A second, separate group that will turn out to be the same job.
    young_canon = add(db_conn, run_id, Source.GETONBOARD, "Acme Robotics", "Platform Engineer", days=3)
    young_member = add(db_conn, run_id, Source.HN, "Acme Robotics", "Platform Engineer", days=4)
    assert link_duplicates(db_conn).groups == 2
    db_conn.execute(  # make the two groups' titles match, as an edit or parser fix might
        update(Posting)
        .where(Posting.id.in_([young_canon, young_member]))
        .values(dedup_key=dedup_key("Acme", "Data Engineer"))
    )

    link_duplicates(db_conn)

    assert {canonical_of(db_conn, i) for i in (old_member, young_canon, young_member)} == {old_canon}


# --- current_postings view --------------------------------------------------------------------


def test_view_shows_one_row_per_group(db_conn: Connection, run_id: uuid.UUID) -> None:
    canonical = add(db_conn, run_id, Source.GETONBOARD, "Acme", "Data Engineer")
    copy_a = add(db_conn, run_id, Source.REMOTIVE, "Acme", "Data Engineer", days=1)
    copy_b = add(db_conn, run_id, Source.HN, "Acme", "Data Engineer", days=2)
    alone = add(db_conn, run_id, Source.HN, None, "Something else", days=3)
    link_duplicates(db_conn)

    assert current(db_conn) == [canonical, alone]  # canonical active: it represents the group

    db_conn.execute(update(Posting).where(Posting.id == canonical).values(is_active=False))
    assert current(db_conn) == [copy_a, alone]  # closed canonical: oldest active copy instead

    db_conn.execute(update(Posting).where(Posting.id.in_([copy_a, copy_b])).values(is_active=False))
    assert current(db_conn) == [alone]  # every copy closed: the job is gone

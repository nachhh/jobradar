"""Integration tests for cross-source linking (stage 3) and the current_postings view."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Connection, insert, select, text, update

from jobradar.ingest.link import UnionFind, link_duplicates
from jobradar.ingest.text import dedup_key
from jobradar.models import PipelineRun, Posting, PostingEnrichment, Source

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


def _enrich(
    conn: Connection,
    run_id: uuid.UUID,
    posting_id: int,
    company: str,
    title: str,
    status: str = "ok",
    latam: str = "unknown",
) -> None:
    """A minimal enrichment row carrying the LLM's company and title as a dedup key."""
    fields = {
        "seniority": "senior",
        "role_family": "backend",
        "employment_type": "full_time",
        "remote": "remote",
    }
    fields |= {"latam_hiring": latam, "salary_basis": "unknown", "summary": "…"}
    if status != "ok":
        fields = {"seniority": None, "role_family": None, "summary": None}
    conn.execute(
        insert(PostingEnrichment).values(
            posting_id=posting_id,
            source_content_hash="x",
            prompt_version="v3",
            provider="fake",
            model="fake-model",
            status=status,
            dedup_key=dedup_key(company, title),
            last_run_id=run_id,
            **fields,
        )
    )


def test_hn_posting_links_through_its_enrichment(db_conn: Connection, run_id: uuid.UUID) -> None:
    """HN has no company/title in the source; the enrichment's key lets it link to another board."""
    board = add(db_conn, run_id, Source.GETONBOARD, "Acme", "Data Engineer", days=0)
    hn = add(db_conn, run_id, Source.HN, None, "", days=1)
    _enrich(db_conn, run_id, hn, "Acme Inc.", "Data Engineer")

    counts = link_duplicates(db_conn)

    assert counts.exact_pairs == 1
    assert canonical_of(db_conn, hn) == board


def test_failed_enrichment_key_is_ignored(db_conn: Connection, run_id: uuid.UUID) -> None:
    add(db_conn, run_id, Source.GETONBOARD, "Acme", "Data Engineer")
    hn = add(db_conn, run_id, Source.HN, None, "", days=1)
    _enrich(db_conn, run_id, hn, "Acme", "Data Engineer", status="failed")

    assert link_duplicates(db_conn).exact_pairs == 0


def test_contradicting_latam_eligibility_is_never_grouped(db_conn: Connection, run_id: uuid.UUID) -> None:
    """Same company and title, but US-only vs LATAM: different jobs for a candidate in LATAM.

    The "unknown" copy matches both. Without the group check, union-find would join all three
    through it (A~B, B~C); with it, the unknown copy joins one side and the other stays alone.
    """
    us = add(db_conn, run_id, Source.HN, "Acme", "Data Engineer")
    latam = add(db_conn, run_id, Source.HIMALAYAS, "Acme", "Data Engineer", days=1)
    unknown = add(db_conn, run_id, Source.JOBICY, "Acme", "Data Engineer", days=2)
    _enrich(db_conn, run_id, us, "Acme", "Data Engineer", latam="no")
    _enrich(db_conn, run_id, latam, "Acme", "Data Engineer", latam="yes")
    _enrich(db_conn, run_id, unknown, "Acme", "Data Engineer", latam="unknown")

    counts = link_duplicates(db_conn)

    # 3 matching pairs; 2 refused: yes~no directly, and the unknown copy's pair with the second
    # side once it has joined the first.
    assert (counts.exact_pairs, counts.groups, counts.blocked) == (3, 1, 2)
    group_of = {p: canonical_of(db_conn, p) or p for p in (us, latam, unknown)}
    assert group_of[us] != group_of[latam]
    assert link_duplicates(db_conn).changed == 0  # deterministic: a second run agrees


def test_source_key_wins_over_the_enrichment_key(db_conn: Connection, run_id: uuid.UUID) -> None:
    """A posting whose source gives company and title keeps that key, whatever the LLM extracted."""
    board = add(db_conn, run_id, Source.GETONBOARD, "Acme", "Data Engineer")
    other = add(db_conn, run_id, Source.JOBICY, "Globex", "ML Engineer", days=1)
    _enrich(db_conn, run_id, other, "Acme", "Data Engineer")  # LLM disagrees with the source: ignored

    assert link_duplicates(db_conn).exact_pairs == 0
    assert canonical_of(db_conn, other) is None
    assert canonical_of(db_conn, board) is None

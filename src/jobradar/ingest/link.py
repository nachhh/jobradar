"""Stage 3 of the pipeline: link the same job posted on different boards (entity resolution).

Rows are never merged or deleted: a duplicate only gets `canonical_id` pointing at its group's
canonical posting, so a wrong link is undone by setting it back to NULL.

How it works:
1. **Candidate pairs (SQL).** Postings from *different* sources that share the first word of their
   normalized company ("blocking": it keeps comparisons few, and two companies that don't even
   share a first word are not the same employer). A pair matches when the normalized company and
   title are equal, or similar enough by trigram similarity (pg_trgm).
2. **Groups (Python, union-find).** Matching is transitive: if A~B and B~C, then A, B and C are one
   job, even if A and C alone fall below the thresholds. Union-find computes those groups.
3. **Canonical (fixed).** A group that already has a canonical keeps it, even if it has closed
   (design decision: canonical = identity; current_postings picks the representative at read time).
   A new group gets its oldest posting. If a new pair joins two existing groups, the older
   canonical wins and the other group's members move under it: the one case where links change.

The key comes from the source when it has company and title, otherwise from the posting's LLM
enrichment (Phase 2): most HN postings name neither in structured form, and the enrichment fills
them in. Postings with neither key are never linked, and neither are two copies whose enrichments
contradict each other on hiring from LATAM (one "yes", one "no"): for a candidate there they are
different jobs.

Linking runs after every ingest and every enrich, which hold different run locks, so it takes its
own lock (LINK_LOCK_ID) to keep two linkings from rewriting canonical_id at the same time.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, text

from jobradar.ledger import lock_id

logger = logging.getLogger(__name__)

LINK_LOCK_ID = lock_id("jobradar.link")

# Thresholds from the spec, to be tuned on real data. Trigram similarity is 0..1: the share of
# 3-character chunks two strings have in common.
COMPANY_MIN_SIMILARITY = 0.7
TITLE_MIN_SIMILARITY = 0.8

# The normalized company and title are the two halves of dedup_key ("company|title"); the parser
# removed all punctuation, so "|" can't appear inside either half. COALESCE takes the source's key
# when it has one and the enrichment's otherwise (only successful enrichments carry a key).
CANDIDATE_PAIRS = text("""
    WITH keyed AS (
        SELECT p.id, p.source, COALESCE(p.dedup_key, e.dedup_key) AS dedup_key, e.latam_hiring
        FROM postings p
        LEFT JOIN posting_enrichments e ON e.posting_id = p.id AND e.status = 'ok'
    ),
    p AS (
        SELECT id, source, latam_hiring,
               split_part(dedup_key, '|', 1) AS company,
               split_part(dedup_key, '|', 2) AS title
        FROM keyed
        WHERE dedup_key IS NOT NULL
    )
    SELECT a.id AS a_id, b.id AS b_id, a.latam_hiring AS a_latam, b.latam_hiring AS b_latam,
           (a.company = b.company AND a.title = b.title) AS exact
    FROM p a
    JOIN p b
      ON a.source < b.source                                        -- different sources, each pair once
     AND split_part(a.company, ' ', 1) = split_part(b.company, ' ', 1)  -- blocking
    WHERE (a.company = b.company AND a.title = b.title)
       OR (similarity(a.company, b.company) >= :company_min
           AND similarity(a.title, b.title) >= :title_min)
    -- Exact pairs first, then by id: the order pairs are grouped in decides which of two
    -- incompatible unions wins (see link_duplicates), so it must be the same on every run.
    ORDER BY exact DESC, a.id, b.id
""")

# A group may not hold copies that say "yes" and "no" to hiring from LATAM: for a candidate there
# they are different jobs (e.g. a US-only and a LATAM opening of the same title). "unknown" is
# compatible with both, since boards describe eligibility unevenly.
INCOMPATIBLE_LATAM = frozenset({"yes", "no"})


@dataclass(frozen=True)
class LinkCounts:
    exact_pairs: int
    fuzzy_pairs: int
    groups: int  # groups of two or more postings, after this run
    changed: int  # postings whose canonical_id was set or moved by this run
    blocked: int = 0  # matching pairs not joined: copies disagree on hiring from LATAM


class UnionFind:
    """Disjoint sets: which items are connected, directly or through others.

    Each item points at a parent; following parents leads to the set's root, which names the set.
    union(a, b) joins two sets by pointing one root at the other. "Path compression" (re-pointing
    items straight at the root while searching) keeps later searches short.

    Each set also keeps the labels of its members (here: their latam_hiring values). A union whose
    merged labels would include all of `incompatible` is refused, so labels can veto a merge that a
    chain of otherwise-fine pairs would make (A~B and B~C joining a "yes" A with a "no" C).
    """

    def __init__(self, incompatible: frozenset[str] = frozenset()) -> None:
        self.parent: dict[int, int] = {}
        self.labels: dict[int, set[str]] = {}  # root -> labels of the set's members
        self.incompatible = incompatible

    def find(self, item: int) -> int:
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:  # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def add_label(self, item: int, label: str | None) -> None:
        if label is not None:
            self.labels.setdefault(self.find(item), set()).add(label)

    def union(self, a: int, b: int, *, force: bool = False) -> bool:
        """Join a's and b's sets; False (and no change) if their labels are incompatible.

        force=True joins regardless: used for links that already exist, which are never undone
        automatically (spec: a wrong link is fixed by hand).
        """
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return True
        merged = self.labels.get(root_a, set()) | self.labels.get(root_b, set())
        if not force and self.incompatible and self.incompatible <= merged:
            return False
        self.parent[root_a] = root_b
        self.labels[root_b] = merged
        self.labels.pop(root_a, None)
        return True

    def groups(self) -> Iterable[list[int]]:
        by_root: dict[int, list[int]] = {}
        for item in self.parent:
            by_root.setdefault(self.find(item), []).append(item)
        return by_root.values()


def relink(engine: Engine) -> None:
    """Link duplicates in a transaction of its own, after an ingest or an enrich run.

    A failure is logged but doesn't fail the run that called it: the postings are loaded (or
    enriched) correctly either way, and the next run links again from scratch (it's idempotent).
    """
    try:
        with engine.begin() as conn:
            counts = link_duplicates(conn)
        logger.info(
            "linking: %d exact and %d fuzzy pairs, %d groups, %d postings (re)linked",
            counts.exact_pairs,
            counts.fuzzy_pairs,
            counts.groups,
            counts.changed,
        )
    except Exception:
        logger.exception("linking failed; links will be retried next run")


def link_duplicates(conn: Connection) -> LinkCounts:
    """Find cross-source duplicates and set canonical_id. Idempotent: a second call changes nothing."""
    # pg_advisory_xact_lock *waits* for the lock (unlike the run locks, which fail fast) and holds
    # it until this transaction ends: a linking started meanwhile simply runs after this one.
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LINK_LOCK_ID})
    pairs = conn.execute(
        CANDIDATE_PAIRS, {"company_min": COMPANY_MIN_SIMILARITY, "title_min": TITLE_MIN_SIMILARITY}
    ).all()

    sets = UnionFind(incompatible=INCOMPATIBLE_LATAM)
    # Existing links first, forced: they are part of the groups (a new copy of a known job joins
    # its group) and are never undone automatically. Their members' LATAM values become the
    # group's labels, so a new pair can't add a contradicting copy to them.
    existing = conn.execute(
        text("""
            SELECT p.id, p.canonical_id, e.latam_hiring
            FROM postings p
            LEFT JOIN posting_enrichments e ON e.posting_id = p.id AND e.status = 'ok'
            WHERE p.canonical_id IS NOT NULL
               OR p.id IN (SELECT canonical_id FROM postings WHERE canonical_id IS NOT NULL)
        """)
    ).all()
    for row in existing:
        sets.add_label(row.id, row.latam_hiring)
    for row in existing:
        if row.canonical_id is not None:
            sets.union(row.id, row.canonical_id, force=True)
    blocked = 0
    for pair in pairs:
        sets.add_label(pair.a_id, pair.a_latam)
        sets.add_label(pair.b_id, pair.b_latam)
        blocked += not sets.union(pair.a_id, pair.b_id)

    exact = sum(1 for p in pairs if p.exact)
    groups = [members for members in sets.groups() if len(members) > 1]
    if not groups:
        return LinkCounts(exact, len(pairs) - exact, 0, 0, blocked)

    # What we need to know about every grouped posting to choose canonicals.
    ids = [i for members in groups for i in members]
    info = {
        row.id: row
        for row in conn.execute(
            text("SELECT id, canonical_id, first_seen_at FROM postings WHERE id = ANY(:ids)"), {"ids": ids}
        )
    }
    current_canonicals = {row.canonical_id for row in existing if row.canonical_id is not None}

    updates: list[dict[str, int | None]] = []
    for members in groups:
        # Prefer canonicals that already exist (fixed identity); otherwise any member can be one.
        candidates = [m for m in members if m in current_canonicals] or members
        canonical = min(candidates, key=lambda m: (info[m].first_seen_at, m))  # oldest; id breaks ties
        for member in members:
            wanted = None if member == canonical else canonical
            if info[member].canonical_id != wanted:
                updates.append({"id": member, "canonical_id": wanted})

    if updates:
        conn.execute(text("UPDATE postings SET canonical_id = :canonical_id WHERE id = :id"), updates)

    return LinkCounts(exact, len(pairs) - exact, len(groups), len(updates), blocked)

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

Postings without a dedup_key (no company or no title, e.g. HN before Phase 2) are never linked.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Connection, text

# Thresholds from the spec, to be tuned on real data. Trigram similarity is 0..1: the share of
# 3-character chunks two strings have in common.
COMPANY_MIN_SIMILARITY = 0.7
TITLE_MIN_SIMILARITY = 0.8

# The normalized company and title are the two halves of dedup_key ("company|title"); the parser
# removed all punctuation, so "|" can't appear inside either half.
CANDIDATE_PAIRS = text("""
    WITH p AS (
        SELECT id, source,
               split_part(dedup_key, '|', 1) AS company,
               split_part(dedup_key, '|', 2) AS title
        FROM postings
        WHERE dedup_key IS NOT NULL
    )
    SELECT a.id AS a_id, b.id AS b_id,
           (a.company = b.company AND a.title = b.title) AS exact
    FROM p a
    JOIN p b
      ON a.source < b.source                                        -- different sources, each pair once
     AND split_part(a.company, ' ', 1) = split_part(b.company, ' ', 1)  -- blocking
    WHERE (a.company = b.company AND a.title = b.title)
       OR (similarity(a.company, b.company) >= :company_min
           AND similarity(a.title, b.title) >= :title_min)
""")


@dataclass(frozen=True)
class LinkCounts:
    exact_pairs: int
    fuzzy_pairs: int
    groups: int  # groups of two or more postings, after this run
    changed: int  # postings whose canonical_id was set or moved by this run


class UnionFind:
    """Disjoint sets: which items are connected, directly or through others.

    Each item points at a parent; following parents leads to the set's root, which names the set.
    union(a, b) joins two sets by pointing one root at the other. "Path compression" (re-pointing
    items straight at the root while searching) keeps later searches short.
    """

    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, item: int) -> int:
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:  # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)

    def groups(self) -> Iterable[list[int]]:
        by_root: dict[int, list[int]] = {}
        for item in self.parent:
            by_root.setdefault(self.find(item), []).append(item)
        return by_root.values()


def link_duplicates(conn: Connection) -> LinkCounts:
    """Find cross-source duplicates and set canonical_id. Idempotent: a second call changes nothing."""
    pairs = conn.execute(
        CANDIDATE_PAIRS, {"company_min": COMPANY_MIN_SIMILARITY, "title_min": TITLE_MIN_SIMILARITY}
    ).all()

    sets = UnionFind()
    for pair in pairs:
        sets.union(pair.a_id, pair.b_id)
    # Existing links are part of the groups too, so a new copy of a known job joins its group.
    existing = conn.execute(
        text("SELECT id, canonical_id FROM postings WHERE canonical_id IS NOT NULL")
    ).all()
    for row in existing:
        sets.union(row.id, row.canonical_id)

    groups = [members for members in sets.groups() if len(members) > 1]
    if not groups:
        return LinkCounts(0, 0, 0, 0)

    # What we need to know about every grouped posting to choose canonicals.
    ids = [i for members in groups for i in members]
    info = {
        row.id: row
        for row in conn.execute(
            text("SELECT id, canonical_id, first_seen_at FROM postings WHERE id = ANY(:ids)"), {"ids": ids}
        )
    }
    current_canonicals = {row.canonical_id for row in existing}

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

    exact = sum(1 for p in pairs if p.exact)
    return LinkCounts(exact, len(pairs) - exact, len(groups), len(updates))

"""What every extractor returns, and the helpers they share.

An extractor's only job is to fetch: it returns each posting's payload as received plus the two
things the raw layer needs, the posting's ID and a content hash. Parsing into clean fields is the
transform's job (stage 2), so a parser bug can never cost another API call.
"""

import copy
import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from jobradar.ingest.http import Fetcher
from jobradar.models import Source

Payload = dict[str, Any]


class ExtractError(Exception):
    """The response doesn't have the shape we rely on (e.g. a posting without an ID).

    That usually means the API changed. It fails the source instead of loading bad data.
    """


@dataclass(frozen=True)
class RawRecord:
    src_posting_id: str
    payload: Payload
    payload_hash: str


# The signature every source module's `extract` function has. A Callable type alias instead of a
# class hierarchy: three plain functions are all the pipeline needs.
Extractor = Callable[[Fetcher], list[RawRecord]]


def payload_hash(payload: Payload, volatile: Iterable[str] = ()) -> str:
    """SHA-256 of the payload's canonical JSON, ignoring `volatile` fields.

    - Canonical JSON (sorted keys, no extra whitespace) makes the hash depend on the content only:
      the same object with its keys in a different order hashes the same.
    - Volatile fields change without the posting changing, e.g. GetOnBoard's applications_count
      goes up every time someone applies. Hashing them would store a new "version" in raw_postings
      on almost every run. They are given as dotted paths ("attributes.applications_count") and
      removed from a copy before hashing; the stored payload keeps them, exactly as received.
    """
    stable = copy.deepcopy(payload)  # deep: nested dicts are removed from, and must not be shared
    for path in volatile:
        *parents, leaf = path.split(".")
        node: Any = stable
        for key in parents:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(leaf, None)
    # ensure_ascii=False keeps "á" as is instead of "á": same hash either way, just shorter.
    canonical = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_record(source: Source, raw_id: object, payload: Payload, volatile: Iterable[str] = ()) -> RawRecord:
    """Build a RawRecord, refusing payloads without an ID: the ID is the posting's natural key."""
    if raw_id is None or str(raw_id).strip() == "":
        raise ExtractError(f"{source}: posting without an id: {str(payload)[:200]}")
    return RawRecord(str(raw_id), payload, payload_hash(payload, volatile))


def dedupe(records: Iterable[RawRecord]) -> list[RawRecord]:
    """Drop repeated postings within one batch, keeping the first.

    Pagination can overlap: a posting published while we page shifts the others down, so the last
    item of page 1 shows up again on page 2. One posting can also appear in two categories.
    """
    seen: set[str] = set()
    unique: list[RawRecord] = []
    for record in records:
        if record.src_posting_id not in seen:
            seen.add(record.src_posting_id)
            unique.append(record)
    return unique

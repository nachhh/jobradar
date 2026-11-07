"""Chunking: split a posting into pieces small enough to embed well, each with a context header.

Concepts:
- One vector for a whole posting averages all its topics (stack, responsibilities, benefits) into
  one point on the map, and matches nothing sharply. Smaller pieces keep each topic distinct.
- Too small loses context ("5+ years required"… of what?). The *contextual header* fixes that:
  every chunk starts with the job's title, company, seniority, role, remote and main skills, so a
  chunk about benefits still says which job it belongs to.
- Splitting follows the text's own structure (lines, then sentences), packing whole units up to
  MAX_CHUNK_TOKENS, with one unit of overlap so an idea cut at a boundary appears whole once.

CHUNKER_VERSION works like PROMPT_VERSION: change it whenever the rules here change, and every
posting is re-indexed on the next run (free: the model is local, it only costs CPU time).
"""

import re
from collections.abc import Callable, Mapping
from typing import Any

CHUNKER_VERSION = "c1"

# Well under the model's 2,048-token limit; small enough for a focused vector. The header (~30-60
# tokens) comes on top.
MAX_CHUNK_TOKENS = 300

# A unit carried over into the next chunk only if it's short; a long overlap would mostly repeat.
MAX_OVERLAP_TOKENS = MAX_CHUNK_TOKENS // 3

_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")

TokenCounter = Callable[[str], int]


def build_header(posting: Mapping[str, Any]) -> str:
    """Two lines of context from the posting and its enrichment, leaving out what's unknown.

    Senior Data Engineer at Acme · senior · data_engineering · remote · LATAM: yes
    Skills: python, spark, airflow
    """
    title = posting.get("title") or "Job"
    company = posting.get("company")
    first = [f"{title} at {company}" if company else title]
    for key in ("seniority_level", "role_family"):
        if posting.get(key) not in (None, "unknown"):
            first.append(posting[key])
    modes = [m for m in posting.get("remote_modes") or [] if m != "unknown"]
    if modes:
        first.append("/".join(modes))
    if posting.get("latam_hiring") not in (None, "unknown"):
        first.append(f"LATAM: {posting['latam_hiring']}")
    lines = [" · ".join(first)]
    skills = posting.get("skills_required") or []
    if skills:
        lines.append("Skills: " + ", ".join(skills[:12]))
    return "\n".join(lines)


def _units(text: str, count: TokenCounter, max_tokens: int) -> list[str]:
    """The text as units no longer than max_tokens: lines, then sentences, then word runs."""
    units: list[str] = []
    for line in (line.strip() for line in text.splitlines()):
        if not line:
            continue
        if count(line) <= max_tokens:
            units.append(line)
            continue
        for sentence in _SENTENCE_END.split(line):
            if count(sentence) <= max_tokens:
                units.append(sentence)
                continue
            # A single "sentence" longer than a chunk (a wall of text): cut it by words.
            words, piece = sentence.split(), []
            for word in words:
                if piece and count(" ".join([*piece, word])) > max_tokens:
                    units.append(" ".join(piece))
                    piece = []
                piece.append(word)
            if piece:
                units.append(" ".join(piece))
    return units


def split_chunks(text: str, count: TokenCounter, max_tokens: int = MAX_CHUNK_TOKENS) -> list[str]:
    """Pack the text's units into chunks of at most max_tokens, overlapping by one short unit.

    Always returns at least one chunk (possibly empty), so every posting gets indexed: its header
    alone still carries the title, company and skills.
    """
    units = _units(text, count, max_tokens)
    if not units:
        return [""]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for unit in units:
        unit_size = count(unit)
        if current and size + unit_size > max_tokens:
            chunks.append("\n".join(current))
            # Start the next chunk with the previous one's last unit, if it's short enough.
            last = current[-1]
            current = [last] if count(last) <= MAX_OVERLAP_TOKENS else []
            size = sum(count(u) for u in current)
            if current and size + unit_size > max_tokens:  # the overlap doesn't leave room
                current, size = [], 0
        current.append(unit)
        size += unit_size
    chunks.append("\n".join(current))
    return chunks

"""Text helpers for the parsers: HTML → plain text, and name normalization for deduplication.

Plain-text descriptions matter beyond looks: Phase 2 embeds them and sends them to an LLM, where
tags are wasted tokens and noise for the embedding model.
"""

import re
import unicodedata
from html.parser import HTMLParser

# Tags that start a new line of text. Everything else (<a>, <strong>, <span>…) is inline: its text
# flows into the surrounding sentence.
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "pre"}
)


class _TextExtractor(HTMLParser):
    """Collects the text of an HTML fragment, turning block tags into line breaks.

    html.parser is in the standard library and tolerant of broken HTML (HN's <p> tags are never
    closed). convert_charrefs=True (the default) decodes entities: "&amp;" → "&", "&#x2F;" → "/".
    """

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(html: str | None) -> str:
    """Plain text from an HTML fragment: entities decoded, one line per block, no blank-line runs."""
    if not html:
        return ""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    text = "".join(extractor.parts).replace("\xa0", " ")  # \xa0 = non-breaking space (&nbsp;)
    # Collapse spaces inside each line, drop empty lines, and keep paragraphs one line apart.
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


# Legal-form suffixes dropped from company names, so "Acme Inc." and "ACME" normalize alike.
# Compared after normalization (lowercase, no punctuation), hence "sa" for "S.A." and "spa" for "SpA".
_LEGAL_SUFFIXES = frozenset(
    {"inc", "llc", "ltd", "ltda", "sa", "sas", "spa", "srl", "sl", "gmbh", "corp", "corporation", "co", "bv"}
)


def normalize_name(value: str | None, *, drop_legal_suffixes: bool = False) -> str:
    """Lowercase, accents and punctuation removed, whitespace collapsed: "Datafy S.A." → "datafy sa".

    NFKD ("compatibility decomposition") splits "é" into "e" plus a combining accent mark, and the
    marks are then dropped, so "Ingeniería" and "Ingenieria" compare equal.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value.casefold())  # casefold: lower() for all scripts
    no_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # \w keeps letters, digits and "_"; everything else becomes a space. Dots are removed outright
    # first, so "S.A." becomes "sa" rather than "s a".
    words = re.sub(r"[^\w]+", " ", no_accents.replace(".", "")).split()
    while drop_legal_suffixes and len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def dedup_key(company: str | None, title: str | None) -> str | None:
    """Normalized "company|title", the exact-match key for cross-source duplicates.

    None when either part is missing (e.g. HN postings before Phase 2's LLM extraction): two
    postings that only share a title are not evidence of the same job.
    """
    company_norm = normalize_name(company, drop_legal_suffixes=True)
    title_norm = normalize_name(title)
    if not company_norm or not title_norm:
        return None
    return f"{company_norm}|{title_norm}"

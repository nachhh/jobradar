"""The grounding check: is the LLM's evidence for latam_hiring really in the posting?

Models sometimes "hallucinate": they produce text that sounds right but isn't in the source. For
the field this project filters on most, the model must quote the sentence that supports its answer,
and we check the quote is really there. If it isn't, the answer can't be trusted and becomes
"unknown". It's a cheap, deterministic guard: a substring search, no second LLM call.
"""

import re
import unicodedata

from jobradar.enrich.schema import EnrichmentOut
from jobradar.models import LatamHiring

# Typographic dashes and non-breaking spaces mapped to plain ones, and quote marks (straight or
# curly) deleted (None): a model quoting `We hire "anywhere"` as `We hire anywhere` still quotes
# faithfully, and the comparison ignores them on both sides.
_PLAIN = str.maketrans(
    {"–": "-", "—": "-", "\u00a0": " ", '"': None, "'": None, "‘": None, "’": None, "“": None, "”": None}
)

# A quote may skip words with an ellipsis ("Open to … LATAM"); each fragment must then appear.
_ELLIPSIS = re.compile(r"\.\.\.|…")


def _normalize(text: str) -> str:
    """Comparable form: Unicode-normalized, plain dashes, no quote marks, lowercase, single spaces.

    NFKC normalization makes visually identical strings equal (e.g. "ﬁ" and "fi", or an accented
    letter written as one character vs letter + accent mark).
    """
    text = unicodedata.normalize("NFKC", text).translate(_PLAIN).lower()
    return " ".join(text.split())


def is_grounded(evidence: str | None, posting_text: str) -> bool:
    """True if every fragment of `evidence` appears in `posting_text`."""
    if not evidence:
        return False
    haystack = _normalize(posting_text)
    fragments = [f.strip(" .,;:") for f in _ELLIPSIS.split(_normalize(evidence))]
    fragments = [f for f in fragments if f]
    return bool(fragments) and all(f in haystack for f in fragments)


def apply_grounding(out: EnrichmentOut, posting_text: str) -> tuple[EnrichmentOut, bool]:
    """Return the answer to keep and whether it was grounded.

    "unknown" needs no evidence. A "yes" or "no" without a quote found in the posting is downgraded
    to "unknown" (model_copy returns a modified copy; the original isn't changed).
    """
    if out.latam_hiring == LatamHiring.UNKNOWN or is_grounded(out.latam_evidence, posting_text):
        return out, True
    return out.model_copy(update={"latam_hiring": LatamHiring.UNKNOWN}), False

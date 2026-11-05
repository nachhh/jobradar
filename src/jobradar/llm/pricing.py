"""What a call costs, from the tokens it used and a price table.

Prices are per million tokens ("MTok"), the unit providers publish. They change over time, so the
cost of each call is computed when it's made and stored in llm_calls.cost_usd: later price changes
don't rewrite history.
"""

from dataclasses import dataclass
from decimal import Decimal

from jobradar.llm.base import Usage


@dataclass(frozen=True)
class Price:
    input_per_mtok: Decimal
    output_per_mtok: Decimal


# Decimal, not float: 0.1 has no exact binary float representation, and the sums of thousands of
# tiny amounts would drift. Strings in Decimal("0.10") keep the value exact.
PRICES: dict[str, Price] = {
    # Anthropic list price (checked 2026-11-05), for prompts up to 100K tokens; ours are ~1,500.
    "claude-haiku-5-5": Price(Decimal("0.10"), Decimal("0.50")),
    # Gemini's free tier charges nothing (it limits requests per day instead).
    "gemini-3.5-flash": Price(Decimal("0"), Decimal("0")),
    "fake-model": Price(Decimal("0"), Decimal("0")),
}

_MILLION = Decimal(1_000_000)


def cost_usd(model: str, usage: Usage | None) -> Decimal | None:
    """Cost of one call in USD, or None when unknown (no usage reported, or a model not in PRICES)."""
    price = PRICES.get(model)
    if price is None or usage is None:
        return None
    return (
        usage.input_tokens * price.input_per_mtok + usage.output_tokens * price.output_per_mtok
    ) / _MILLION

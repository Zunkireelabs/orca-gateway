"""A cost TABLE, not a cost service (S5 brief §3.3). Hardcoded OpenAI per-token prices for the
models Zunkiree's backend is known to use. Prices change; this is a documented maintenance item,
not a bug — check https://openai.com/api/pricing/ and update PRICES_USD_PER_TOKEN with a new date
comment when they do.

When `model` isn't in the table, `cost_usd` returns None and logs a warning rather than guessing:
an unpriced model is "unknown," never "free."
"""

from __future__ import annotations

import logging

logger = logging.getLogger("orca_gateway.cost")

# (prompt $/token, completion $/token). Checked against openai.com/api/pricing/ on 2026-09-20.
PRICES_USD_PER_TOKEN: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15 / 1_000_000, 0.60 / 1_000_000),
    "gpt-4o": (2.50 / 1_000_000, 10.00 / 1_000_000),
    "gpt-4.1": (2.00 / 1_000_000, 8.00 / 1_000_000),
    "gpt-4.1-mini": (0.40 / 1_000_000, 1.60 / 1_000_000),
}


def cost_usd(
    model: str | None, prompt_tokens: int | None, completion_tokens: int | None
) -> float | None:
    """None in, None out: a null token count means 'unknown,' not zero cost. A model absent from
    the table also yields None (logged), never a guessed number."""
    if model is None or prompt_tokens is None or completion_tokens is None:
        return None
    prices = PRICES_USD_PER_TOKEN.get(model)
    if prices is None:
        logger.warning("no price table entry for model=%s; leaving llm_cost_usd null", model)
        return None
    prompt_price, completion_price = prices
    return round(prompt_tokens * prompt_price + completion_tokens * completion_price, 6)

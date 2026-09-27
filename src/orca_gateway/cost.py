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


# P6 brief §3.2: telephony is zero BY DESIGN (no connector exists yet -- K1 open), never estimated.
# Named so `all_in_cost_usd` reads as the real formula (LLM + platform + telephony) rather than a
# silent omission, and so the one line that changes when K1 lands is obvious.
TELEPHONY_RATE_USD_PER_MINUTE = 0.0


def all_in_cost_usd(
    llm_cost_usd: float | None,
    elevenlabs_cost_fiat: float | None,
    telephony_minutes: float | None = None,
) -> float | None:
    """LLM (ours, OpenAI, already billed to our account and captured in `llm_cost_usd`) + the
    vendor's own non-LLM platform price (`elevenlabs_cost_fiat`, trusted as-is per the P6 brief
    §6 Q2 -- the double-count trap this guards against is adding an LLM figure ElevenLabs' own
    `cost_fiat` might also carry under a Custom LLM; the 10-call reconciliation in
    docs/metering-reconciliation.md is what confirms it does not) + telephony (0 until K1).

    `telephony_minutes is None` means "zero by design" (0002_calls.sql: no connector exists),
    NOT unknown -- it is the one component allowed to default to zero, so the all-in formula
    already reads correctly once K1 lands and starts populating it.

    `llm_cost_usd is None` or `elevenlabs_cost_fiat is None` -> None out: a call not yet
    reconciled with ElevenLabs, or with no priced LLM usage, has an UNKNOWN all-in cost, never a
    partial total presented as the whole story. Chat calls, which never touch ElevenLabs, pass
    `elevenlabs_cost_fiat=0.0` (never None) so their all-in cost is exactly their LLM cost --
    see reporting.py, which is the only caller and decides which zero is real per channel.
    """
    if llm_cost_usd is None or elevenlabs_cost_fiat is None:
        return None
    return round(
        llm_cost_usd
        + elevenlabs_cost_fiat
        + (telephony_minutes or 0.0) * TELEPHONY_RATE_USD_PER_MINUTE,
        6,
    )

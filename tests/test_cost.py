from orca_gateway.cost import PRICES_USD_PER_TOKEN, cost_usd


def test_priced_model_computes_from_the_table():
    prompt_price, completion_price = PRICES_USD_PER_TOKEN["gpt-4o-mini"]
    got = cost_usd("gpt-4o-mini", 1000, 200)
    assert got == round(1000 * prompt_price + 200 * completion_price, 6)


def test_unknown_model_is_null_not_a_guess(caplog):
    assert cost_usd("some-future-model", 1000, 200) is None


def test_null_token_counts_are_null_not_zero_cost():
    assert cost_usd("gpt-4o-mini", None, 200) is None
    assert cost_usd("gpt-4o-mini", 1000, None) is None
    assert cost_usd(None, 1000, 200) is None


def test_zero_tokens_is_a_real_zero_not_null():
    # A model that IS priced with 0 usage (e.g. a cached/free response) is a real $0, distinct
    # from "unknown."
    assert cost_usd("gpt-4o-mini", 0, 0) == 0.0

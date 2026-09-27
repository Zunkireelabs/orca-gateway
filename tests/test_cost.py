from orca_gateway.cost import PRICES_USD_PER_TOKEN, all_in_cost_usd, cost_usd


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


# ---- P6: the all-in formula (LLM + ElevenLabs platform + telephony) ------------------------


def test_all_in_cost_sums_llm_and_elevenlabs():
    assert all_in_cost_usd(0.05, 0.02) == 0.07


def test_all_in_cost_is_null_when_llm_cost_is_unknown():
    assert all_in_cost_usd(None, 0.02) is None


def test_all_in_cost_is_null_when_elevenlabs_cost_is_not_yet_reconciled():
    # The double-count-avoidance trap's flip side: a voice call whose ElevenLabs meters haven't
    # been pulled yet must show an UNKNOWN all-in cost, never llm_cost_usd alone presented as if
    # it were the whole story.
    assert all_in_cost_usd(0.05, None) is None


def test_telephony_minutes_none_means_zero_by_design_not_unknown():
    # Unlike llm_cost_usd/elevenlabs_cost_fiat, a null telephony_minutes does not blank the total:
    # 0002_calls.sql defines it as zero by design (no connector exists yet, K1 open).
    assert all_in_cost_usd(0.05, 0.02, None) == all_in_cost_usd(0.05, 0.02, 0.0) == 0.07

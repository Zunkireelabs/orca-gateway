"""P4 brief A3: the phone-number guard, as a pure function and through both adapters (flag on and
off). The logs must carry counts only, never a digit."""

import json
import logging

import pytest

from orca_gateway import deps
from orca_gateway.channels import elevenlabs_llm, widget_chat
from orca_gateway.coalescer import TurnCoalescer
from orca_gateway.config import get_settings
from orca_gateway.messages import spoken_message
from orca_gateway.phone_guard import guard_phone_numbers, same_number
from orca_gateway.rate_limit import CallerRateLimiter
from orca_gateway.seam import TurnEvent
from orca_gateway.tenant_concurrency import TenantConcurrencyLimiter
from orca_gateway.tenants import TenantStore
from tests import test_voice_adapter as voice_t
from tests import test_widget_chat_adapter as chat_t
from tests.tenant_fixtures import InMemoryRepo, chat, dental_city, voice

REPL = "<REPLACED>"
OURS = ["+977-1-4444444"]


def g(text, allowed=(), repl=REPL):
    return guard_phone_numbers(text, list(allowed), repl)


# ---- the pure function -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Call 9801234567 now.",
        "Call 980-123-4567 now.",
        "Call +977 980 123 4567 now.",
        "Call (01) 5555555 now.",
        "Call 01-5555555.",
        "कल गर्नुहोस् ९८०१२३४५६७ मा।",  # Devanagari digits
    ],
)
def test_a_phone_shaped_span_that_is_not_allowed_is_replaced(text):
    r = g(text, OURS)
    assert r.replaced == 1 and REPL in r.text
    assert not any(c.isdigit() for c in r.text.replace(REPL, "")) or "कल" in text


def test_the_surrounding_sentence_survives_and_each_span_is_replaced():
    r = g("Call 9801234567, or 9811111111.", OURS)
    assert r.text == f"Call {REPL}, or {REPL}." and r.replaced == 2


@pytest.mark.parametrize(
    "written",
    ["014444444", "01-4444444", "+977 1 4444444", "+977-1-4444444", "1 4444444", "१-४४४४४४४"],
)
def test_the_configured_number_passes_however_it_is_written(written):
    assert g(f"Call {written}.", OURS).replaced == 0


def test_a_different_number_that_only_shares_a_tail_is_replaced():
    # same last digits, but the extra leading digits are not a +country code on the long side
    assert g("Call 99 4444444.", OURS).replaced == 1
    assert g("Call 5 4444444.", OURS).replaced == 1


def test_same_number_needs_a_plus_to_forgive_a_country_code():
    assert same_number("+9779801234567", "9801234567")
    assert same_number("9801234567", "+9779801234567")
    assert not same_number("9779801234567", "9801234567")  # no '+': not forgiven
    assert not same_number("+97798012345670", "9801234567")  # a tail-only match is not


@pytest.mark.parametrize(
    "text",
    [
        "Your visit is on 2026-10-20.",
        "It is 20-10-2026 today.",
        "मिति २०८३-०६-२०",
        "The fee is Rs 1500000.",
        "The fee is रु १५००००० हो।",
        "The fee is 1500000 rupees.",
        "That is 1,500,000 in total.",
        "Pi is 3.1415926.",
        "Open 10:00 to 17:30.",
        "We have 12 rooms and 3 doctors.",
        "Ask for code 123456.",  # under 7 digits
    ],
)
def test_dates_amounts_times_and_short_numbers_are_not_phone_numbers(text):
    assert g(text, []).replaced == 0 and g(text, []).text == text


def test_empty_allowlist_fails_closed():
    assert g("Call 01-4444444.", []).replaced == 1


def test_no_span_means_the_same_object_back():
    r = g("No numbers here.", OURS)
    assert r.text == "No numbers here." and r.replaced == 0


def test_allowed_numbers_must_be_numbers():
    with pytest.raises(ValueError):
        voice(allowed_phone_numbers=["call us"])
    assert voice(allowed_phone_numbers=["+977-1-4444444"]).allowed_phone_numbers == OURS


def test_default_phone_message_is_generic_and_localised():
    en = voice(default_language="en", spoken_brand_name="Quiet Spa")
    assert spoken_message("phone_guard", en) == (
        "please check the Quiet Spa website for our contact number"
    )
    assert "Quiet Spa" in spoken_message("phone_guard", voice(spoken_brand_name="Quiet Spa"))
    assert spoken_message("phone_guard", voice(phone_guard_message="see {brand}")).startswith(
        "see "
    )


# ---- voice adapter ------------------------------------------------------------------------------

ANSWER = "Please call 9801234567 or our line 01-4444444 today."


def _voice_world(monkeypatch, **over):
    monkeypatch.setenv("ORCA_VOICE_SHARED_SECRET", voice_t.SECRET)
    get_settings.cache_clear()
    backend = voice_t._Backend(events=[TurnEvent(type="done", data={"answer": ANSWER})])
    tenant = dental_city()
    for k, v in over.items():
        setattr(tenant.channels["voice"], k, v)
    monkeypatch.setattr(deps, "get_backend", lambda: backend)
    monkeypatch.setattr(deps, "get_tenant_store", lambda: TenantStore(InMemoryRepo(tenant)))
    monkeypatch.setattr(elevenlabs_llm, "_coalescer", TurnCoalescer(debounce_s=0.0))
    return tenant


def _voice_spoken(r):
    return json.loads(voice_t._sse(r.text)[0])["choices"][0]["delta"]["content"]


async def test_voice_flag_off_passes_numbers_through_as_before(monkeypatch):
    _voice_world(monkeypatch)
    spoken = _voice_spoken(await voice_t._post())
    assert "PHONE" not in spoken and "website" not in spoken
    assert "nine eight zero" in spoken  # number speech still read the digits out


async def test_voice_flag_on_replaces_the_unlisted_number_and_keeps_the_listed_one(
    monkeypatch, caplog
):
    tenant = _voice_world(monkeypatch, phone_guard=True, allowed_phone_numbers=["01-4444444"])
    caplog.set_level(logging.INFO)
    spoken = _voice_spoken(await voice_t._post())
    replacement = spoken_message("phone_guard", tenant.channels["voice"])
    assert replacement in spoken  # in place of the invented number
    assert "नौ आठ शून्य" not in spoken  # 980... never reaches TTS, not even as words
    assert "शून्य एक, चार चार चार चार चार चार चार" in spoken  # the listed one, read out as before
    lines = [r.getMessage() for r in caplog.records if "[PHONE-GUARD]" in r.getMessage()]
    assert len(lines) == 1 and "tenant=dental-city channel=voice count=1" in lines[0]
    assert "9801234567" not in caplog.text and "4444444" not in caplog.text


async def test_voice_flag_on_with_an_empty_list_replaces_every_number(monkeypatch):
    tenant = _voice_world(monkeypatch, phone_guard=True)
    spoken = _voice_spoken(await voice_t._post())
    replacement = spoken_message("phone_guard", tenant.channels["voice"])
    assert spoken.count(replacement) == 2 and "शून्य" not in spoken


async def test_voice_guard_still_applies_with_number_speech_switched_off(monkeypatch):
    monkeypatch.setenv("ORCA_VOICE_NUMBER_SPEECH", "false")
    _voice_world(monkeypatch, phone_guard=True, allowed_phone_numbers=["01-4444444"])
    get_settings.cache_clear()
    spoken = _voice_spoken(await voice_t._post())
    assert "9801234567" not in spoken and "01-4444444" in spoken


async def test_voice_guard_applies_to_a_spoken_fallback_message_too(monkeypatch):
    # every spoken byte leaves through speak(): a tenant-authored message with an unlisted number
    _voice_world(
        monkeypatch,
        phone_guard=True,
        spoken_kill_switch=True,
        kill_switch=True,
        kill_switch_message="Call 9801234567.",
    )
    assert "9801234567" not in _voice_spoken(await voice_t._post())


# ---- chat adapter -------------------------------------------------------------------------------


def _chat_world(monkeypatch, suggestions=("Call 9801234567?", "More?"), **over):
    tenant = dental_city()
    tenant.channels["chat"] = chat(agent_id="front-desk", **over)
    backend = chat_t._Backend(
        events=[
            TurnEvent(
                type="done",
                data={"answer": ANSWER, "sources": [], "suggestions": list(suggestions)},
            )
        ]
    )
    monkeypatch.setattr(deps, "get_backend", lambda: backend)
    monkeypatch.setattr(deps, "get_tenant_store", lambda: TenantStore(InMemoryRepo(tenant)))
    monkeypatch.setattr(elevenlabs_llm, "_coalescer", TurnCoalescer(debounce_s=0.0))
    monkeypatch.setattr(elevenlabs_llm, "_tenant_limiter", TenantConcurrencyLimiter())
    monkeypatch.setattr(widget_chat, "_rate_limiter", CallerRateLimiter(window_s=60.0))
    return tenant


async def test_chat_flag_off_passes_the_answer_through_unchanged(monkeypatch):
    _chat_world(monkeypatch)
    frames = chat_t._sse((await chat_t._post()).text)
    assert frames[0]["data"] == ANSWER and frames[1]["answer"] == ANSWER
    assert frames[1]["suggestions"] == ["Call 9801234567?", "More?"]


async def test_chat_flag_on_guards_the_token_frame_the_done_frame_and_the_suggestions(
    monkeypatch, caplog
):
    tenant = _chat_world(monkeypatch, phone_guard=True, allowed_phone_numbers=["01-4444444"])
    caplog.set_level(logging.INFO)
    frames = chat_t._sse((await chat_t._post()).text)
    replacement = spoken_message("phone_guard", tenant.channels["chat"])
    expected = f"Please call {replacement} or our line 01-4444444 today."
    assert frames[0]["data"] == expected and frames[1]["answer"] == expected
    assert frames[1]["suggestions"] == [f"Call {replacement}?", "More?"]
    assert frames[1]["session_id"] == "sess-1"  # the rest of the wire shape is untouched
    lines = [r.getMessage() for r in caplog.records if "[PHONE-GUARD]" in r.getMessage()]
    assert len(lines) == 1 and "tenant=dental-city channel=chat count=2" in lines[0]
    assert "9801234567" not in caplog.text and "4444444" not in caplog.text


async def test_chat_flag_on_with_an_empty_list_replaces_every_number(monkeypatch):
    _chat_world(monkeypatch, phone_guard=True)
    frames = chat_t._sse((await chat_t._post()).text)
    assert "01-4444444" not in frames[1]["answer"] and "9801234567" not in frames[1]["answer"]


async def test_the_guard_flag_is_per_channel_voice_on_does_not_guard_chat(monkeypatch):
    tenant = _chat_world(monkeypatch)
    tenant.channels["voice"].phone_guard = True
    frames = chat_t._sse((await chat_t._post()).text)
    assert frames[1]["answer"] == ANSWER

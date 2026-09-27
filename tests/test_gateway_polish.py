"""P4 gateway-polish brief (A3 + A4): built-in messages follow the CALLER's language, and a
silence turn ("...") is answered by the gateway itself. Both flags default off; each is proven
with the flag on AND off, and the over-trigger cases (a real short turn, a bare "?") are proven to
still reach the backend."""

from orca_gateway.messages import caller_language, spoken_message
from orca_gateway.seam import TurnEvent
from tests import test_phone_guard as phone_t
from tests import test_voice_adapter as voice_t
from tests.tenant_fixtures import voice
from tests.test_spoken_fallbacks import _spoken, _tenant, _wire

# ---- caller_language() --------------------------------------------------------------------------


def test_devanagari_text_is_nepali_regardless_of_default_language():
    en_default = voice(languages=["ne", "en"], default_language="en")
    assert caller_language("के हुन्छ?", en_default) == "ne"


def test_latin_text_is_english_when_the_channel_serves_it():
    ch = voice(languages=["ne", "en"], default_language="ne")
    assert caller_language("hello there", ch) == "en"


def test_latin_text_falls_back_to_default_when_english_is_not_served():
    ch = voice(languages=["ne"], default_language="ne")
    assert caller_language("hello there", ch) == "ne"


def test_empty_text_is_treated_as_latin_script():
    ch = voice(languages=["ne", "en"], default_language="ne")
    assert caller_language("", ch) == "en"


# ---- spoken_message(lang=...) ---------------------------------------------------------------


def test_spoken_message_lang_none_keeps_default_language_behaviour():
    ch = voice(default_language="ne", spoken_brand_name="Spa", languages=["ne", "en"])
    assert spoken_message("kill_switch", ch) == spoken_message("kill_switch", ch, lang=None)
    assert spoken_message("kill_switch", ch).startswith("Spa ले")


def test_spoken_message_lang_picks_the_given_language_over_the_default():
    ch = voice(default_language="ne", spoken_brand_name="Spa", languages=["ne", "en"])
    assert spoken_message("kill_switch", ch, lang="en").startswith("Spa can't take calls")


def test_spoken_message_override_wins_regardless_of_lang():
    ch = voice(kill_switch_message="{brand} is offline.", spoken_brand_name="Spa")
    assert spoken_message("kill_switch", ch, lang="ne") == "Spa is offline."


def test_spoken_message_unknown_lang_falls_back_to_english():
    ch = voice(spoken_brand_name="Spa")
    assert spoken_message("kill_switch", ch, lang="fr").startswith("Spa can't take calls")


# ---- A3: phone_guard language ------------------------------------------------------------------


async def test_phone_guard_replacement_follows_an_english_caller(monkeypatch):
    tenant = phone_t._voice_world(
        monkeypatch,
        phone_guard=True,
        caller_language_messages=True,
        allowed_phone_numbers=["9999999999"],  # positive control: not the number in the answer
    )
    r = await voice_t._post(body=voice_t._body(user="what's your phone number?"))
    spoken = phone_t._voice_spoken(r)
    expected = spoken_message("phone_guard", tenant.channels["voice"], lang="en")
    assert expected in spoken
    assert "कृपया" not in spoken  # no Nepali spliced into an English sentence


async def test_phone_guard_replacement_follows_a_devanagari_caller(monkeypatch):
    tenant = phone_t._voice_world(
        monkeypatch,
        phone_guard=True,
        caller_language_messages=True,
        allowed_phone_numbers=["9999999999"],
    )
    r = await voice_t._post(body=voice_t._body(user="फोन नम्बर के हो?"))
    spoken = phone_t._voice_spoken(r)
    expected = spoken_message("phone_guard", tenant.channels["voice"], lang="ne")
    assert expected in spoken


async def test_phone_guard_flag_off_still_follows_default_language(monkeypatch):
    tenant = phone_t._voice_world(
        monkeypatch, phone_guard=True, allowed_phone_numbers=["9999999999"]
    )
    r = await voice_t._post(body=voice_t._body(user="what's your phone number?"))
    spoken = phone_t._voice_spoken(r)
    # caller_language_messages is off: today's behaviour, default_language ("ne") wins even for
    # an English caller.
    assert spoken_message("phone_guard", tenant.channels["voice"]) in spoken


# ---- A3: error fallback language ----------------------------------------------------------------


async def test_error_fallback_follows_an_english_caller(monkeypatch):
    backend = voice_t._Backend(events=[TurnEvent(type="error", data={"message": "boom"})])
    tenant = _tenant(spoken_error_fallback=True, caller_language_messages=True)
    metering = _wire(monkeypatch, tenant, backend)
    r = await voice_t._post(body=voice_t._body(user="hello there"))
    assert _spoken(r) == spoken_message("error_fallback", tenant.channels["voice"], lang="en")
    (turn,) = metering.completed
    assert turn["ended_by"] == "error"


async def test_error_fallback_follows_a_devanagari_caller(monkeypatch):
    backend = voice_t._Backend(events=[TurnEvent(type="error", data={"message": "boom"})])
    tenant = _tenant(spoken_error_fallback=True, caller_language_messages=True)
    _wire(monkeypatch, tenant, backend)
    r = await voice_t._post(body=voice_t._body(user="के भयो?"))
    assert _spoken(r) == spoken_message("error_fallback", tenant.channels["voice"], lang="ne")


async def test_error_fallback_flag_off_still_follows_default_language(monkeypatch):
    backend = voice_t._Backend(events=[TurnEvent(type="error", data={"message": "boom"})])
    tenant = _tenant(spoken_error_fallback=True)
    _wire(monkeypatch, tenant, backend)
    r = await voice_t._post(body=voice_t._body(user="hello there"))
    assert _spoken(r) == spoken_message("error_fallback", tenant.channels["voice"])


# ---- A3: kill switch language (same wiring pattern) ---------------------------------------------


async def test_kill_switch_follows_the_caller_language(monkeypatch):
    tenant = _tenant(kill_switch=True, spoken_kill_switch=True, caller_language_messages=True)
    _wire(monkeypatch, tenant, voice_t._Backend())
    r = await voice_t._post(body=voice_t._body(user="hello there"))
    assert _spoken(r) == spoken_message("kill_switch", tenant.channels["voice"], lang="en")


# ---- A4: silence nudge ---------------------------------------------------------------------------


def _messages(*, prior: str | None, silence: str = "..."):
    msgs = [{"role": "system", "content": "sys"}]
    if prior is not None:
        msgs.append({"role": "user", "content": prior})
        msgs.append({"role": "assistant", "content": "ok"})
    msgs.append({"role": "user", "content": silence})
    return msgs


async def test_silence_flag_off_reaches_the_backend_like_any_other_turn(monkeypatch):
    backend = voice_t._Backend()
    _wire(monkeypatch, _tenant(), backend)
    await voice_t._post(body=voice_t._body(messages=_messages(prior=None)))
    assert backend.calls  # dispatched, unlike A4 on


async def test_silence_answered_by_the_gateway_in_english_no_dispatch(monkeypatch):
    backend = voice_t._Backend()
    tenant = _tenant(silence_nudge=True)
    metering = _wire(monkeypatch, tenant, backend)
    r = await voice_t._post(
        body=voice_t._body(messages=_messages(prior="I'd like some help please"))
    )
    assert r.status_code == 200
    assert backend.calls == []  # no dispatch leg
    assert _spoken(r) == spoken_message("silence_nudge", tenant.channels["voice"], lang="en")
    (turn,) = metering.completed
    assert turn["ended_by"] == "silence" and turn["usage"] is None


async def test_silence_answered_by_the_gateway_in_nepali_no_dispatch(monkeypatch):
    backend = voice_t._Backend()
    tenant = _tenant(silence_nudge=True)
    _wire(monkeypatch, tenant, backend)
    r = await voice_t._post(body=voice_t._body(messages=_messages(prior="के छ हजुर")))
    assert backend.calls == []
    assert _spoken(r) == spoken_message("silence_nudge", tenant.channels["voice"], lang="ne")


async def test_silence_with_no_prior_substantive_turn_uses_default_language(monkeypatch):
    backend = voice_t._Backend()
    tenant = _tenant(silence_nudge=True, default_language="ne")
    _wire(monkeypatch, tenant, backend)
    r = await voice_t._post(body=voice_t._body(messages=_messages(prior=None)))
    assert backend.calls == []
    assert _spoken(r) == spoken_message("silence_nudge", tenant.channels["voice"], lang="ne")


async def test_silence_does_not_over_trigger_on_a_real_short_word(monkeypatch):
    backend = voice_t._Backend()
    _wire(monkeypatch, _tenant(silence_nudge=True), backend)
    r = await voice_t._post(body=voice_t._body(user="Yes."))
    assert backend.calls  # dispatched, not treated as silence
    assert _spoken(r) == "final:Yes."


async def test_silence_does_not_over_trigger_on_a_bare_question_mark(monkeypatch):
    backend = voice_t._Backend()
    _wire(monkeypatch, _tenant(silence_nudge=True), backend)
    r = await voice_t._post(body=voice_t._body(user="?"))
    assert backend.calls
    assert _spoken(r) == "final:?"


async def test_silence_is_not_billable(monkeypatch):
    backend = voice_t._Backend()
    tenant = _tenant(silence_nudge=True)
    metering = _wire(monkeypatch, tenant, backend)
    await voice_t._post(body=voice_t._body(messages=_messages(prior="hi")))
    (turn,) = metering.completed
    assert turn["usage"] is None

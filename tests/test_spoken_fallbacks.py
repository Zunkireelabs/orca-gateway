"""P4 brief A1: a voice caller hears something instead of silence. Every guard is a per-channel
flag, default off; each is tested with the flag on AND off, and chat is proven unchanged."""

import json

import pytest

from orca_gateway import deps
from orca_gateway.channels import elevenlabs_llm
from orca_gateway.coalescer import TurnCoalescer
from orca_gateway.config import get_settings
from orca_gateway.messages import spoken_message
from orca_gateway.seam import TurnEvent
from orca_gateway.tenants import TenantStore
from tests.tenant_fixtures import InMemoryRepo, dental_city, quiet_spa, voice
from tests.test_voice_adapter import SECRET, _Backend, _headers, _post, _sse


class _Metering:
    """Just enough of PgCallsRepository for the voice handler; records what it is told."""

    def __init__(self):
        self.closed: list[tuple[str, str]] = []
        self.completed: list[dict] = []

    async def get_open_call(self, conversation_id):
        return None

    async def touch_call(self, **kw):
        return None

    async def close_call(self, conversation_id, reason):
        self.closed.append((conversation_id, reason))

    async def record_abandoned_run(self, conversation_id):
        return None

    async def complete_turn(self, **kw):
        self.completed.append(kw)


def _wire(monkeypatch, tenant, backend, *, run_timeout_s=None):
    monkeypatch.setenv("ORCA_VOICE_SHARED_SECRET", SECRET)
    get_settings.cache_clear()
    metering = _Metering()
    monkeypatch.setattr(deps, "get_backend", lambda: backend)
    monkeypatch.setattr(deps, "get_calls_repo", lambda: metering)
    monkeypatch.setattr(deps, "get_tenant_store", lambda: TenantStore(InMemoryRepo(tenant)))
    kw = {} if run_timeout_s is None else {"run_timeout_s": run_timeout_s}
    monkeypatch.setattr(elevenlabs_llm, "_coalescer", TurnCoalescer(debounce_s=0.0, **kw))
    return metering


def _spoken(r) -> str:
    return json.loads(_sse(r.text)[0])["choices"][0]["delta"]["content"]


def _tenant(**over):
    t = dental_city()
    for k, v in over.items():
        setattr(t.channels["voice"], k, v)
    return t


@pytest.fixture(autouse=True)
def _reset_settings():
    yield
    get_settings.cache_clear()


# ---- messages ---------------------------------------------------------------------------------


def test_default_messages_follow_the_default_language_and_substitute_the_brand():
    en = voice(default_language="en", spoken_brand_name="Quiet Spa")
    ne = voice(default_language="ne", spoken_brand_name="Quiet Spa")
    assert spoken_message("kill_switch", en) == (
        "Quiet Spa can't take calls right now. Please try again later."
    )
    assert spoken_message("error_fallback", en) == (
        "Sorry, I'm having trouble right now. Could you say that again?"
    )
    assert spoken_message("kill_switch", ne).startswith("Quiet Spa ले")
    assert "फेरि" in spoken_message("error_fallback", ne)
    assert "{brand}" not in spoken_message("kill_switch", ne)


def test_unknown_language_falls_back_to_english_and_a_tenant_override_wins():
    fr = voice(languages=["fr"], default_language="fr", spoken_brand_name="Spa")
    assert spoken_message("kill_switch", fr).startswith("Spa can't take calls")
    custom = voice(kill_switch_message="{brand} is offline.", spoken_brand_name="Spa")
    assert spoken_message("kill_switch", custom) == "Spa is offline."


# ---- kill switch ------------------------------------------------------------------------------


async def test_kill_switch_flag_off_is_still_a_403(monkeypatch):
    backend = _Backend()
    metering = _wire(monkeypatch, _tenant(kill_switch=True), backend)
    r = await _post()
    assert r.status_code == 403 and backend.calls == []
    assert metering.closed  # unchanged: the call is still closed


async def test_kill_switch_flag_on_speaks_the_message_and_never_calls_the_backend(monkeypatch):
    backend = _Backend()
    tenant = _tenant(kill_switch=True, spoken_kill_switch=True)
    metering = _wire(monkeypatch, tenant, backend)
    r = await _post()
    assert r.status_code == 200 and backend.calls == []
    assert _spoken(r) == spoken_message("kill_switch", tenant.channels["voice"])
    assert _sse(r.text)[-1] == "[DONE]"
    assert [reason for _, reason in metering.closed] == ["kill_switch"]


async def test_spoken_kill_switch_does_nothing_while_the_switch_is_off(monkeypatch):
    backend = _Backend()
    _wire(monkeypatch, _tenant(spoken_kill_switch=True), backend)
    r = await _post()
    assert _spoken(r) == "final:hello"  # served normally


@pytest.mark.parametrize(
    "over",
    [{"is_enabled": False}, {"kill_switch": True, "is_enabled": False}],
)
async def test_spoken_kill_switch_never_speaks_for_a_disabled_channel(monkeypatch, over):
    _wire(monkeypatch, _tenant(spoken_kill_switch=True, **over), _Backend())
    assert (await _post()).status_code == 403


async def test_spoken_kill_switch_never_speaks_for_an_inactive_or_unknown_tenant(monkeypatch):
    tenant = _tenant(kill_switch=True, spoken_kill_switch=True)
    tenant.is_active = False
    _wire(monkeypatch, tenant, _Backend())
    assert (await _post()).status_code == 403
    assert (await _post(headers=_headers(tenant="nobody"))).status_code == 403


async def test_kill_switch_message_is_per_tenant_not_shared(monkeypatch):
    spa = quiet_spa()
    spa.channels["voice"].kill_switch = True
    spa.channels["voice"].spoken_kill_switch = True
    _wire(monkeypatch, spa, _Backend())
    r = await _post(headers=_headers(tenant="quiet-spa"))
    assert _spoken(r).startswith("Quiet Spa can't take calls")  # its own brand and language


# ---- timeout / backend error --------------------------------------------------------------------


async def test_backend_error_flag_off_is_still_a_502(monkeypatch):
    backend = _Backend(events=[TurnEvent(type="error", data={"message": "boom"})])
    _wire(monkeypatch, _tenant(), backend)
    assert (await _post()).status_code == 502


async def test_backend_error_flag_on_speaks_the_apology_and_records_an_error_turn(monkeypatch):
    backend = _Backend(events=[TurnEvent(type="error", data={"message": "vendor.example blew"})])
    tenant = _tenant(spoken_error_fallback=True)
    metering = _wire(monkeypatch, tenant, backend)
    r = await _post()
    assert r.status_code == 200
    assert _spoken(r) == spoken_message("error_fallback", tenant.channels["voice"])
    assert "vendor" not in r.text
    (turn,) = metering.completed
    assert turn["ended_by"] == "error" and turn["usage"] is None
    assert turn["user_text"] == "hello"


async def test_run_timeout_flag_off_is_still_a_502(monkeypatch):
    _wire(monkeypatch, _tenant(), _Backend(delay=1.0), run_timeout_s=0.05)
    assert (await _post()).status_code == 502


async def test_run_timeout_flag_on_speaks_the_apology_never_a_502(monkeypatch):
    tenant = _tenant(spoken_error_fallback=True)
    metering = _wire(monkeypatch, tenant, _Backend(delay=1.0), run_timeout_s=0.05)
    r = await _post()
    assert r.status_code == 200
    assert _spoken(r) == spoken_message("error_fallback", tenant.channels["voice"])
    assert [t["ended_by"] for t in metering.completed] == ["error"]


async def test_error_fallback_flag_on_does_not_touch_a_healthy_turn(monkeypatch):
    backend = _Backend()
    metering = _wire(monkeypatch, _tenant(spoken_error_fallback=True), backend)
    r = await _post()
    assert _spoken(r) == "final:hello"
    assert [t["ended_by"] for t in metering.completed] == [None]


async def test_error_fallback_does_not_swallow_the_daily_spend_cap_refusal(monkeypatch):
    class _Capped(_Metering):
        async def daily_spend_usd(self, slug):
            return 99.0

        async def refuse_call(self, **kw):
            return None

    tenant = _tenant(spoken_error_fallback=True, daily_spend_cap=1.0)
    _wire(monkeypatch, tenant, _Backend())
    monkeypatch.setattr(deps, "get_calls_repo", lambda: _Capped())
    assert (await _post()).status_code == 403  # a refusal, not an "error" turn


async def test_a_retry_after_a_spoken_error_reaches_the_backend_again(monkeypatch):
    backend = _Backend(events=[TurnEvent(type="error", data={"message": "boom"})])
    _wire(monkeypatch, _tenant(spoken_error_fallback=True), backend)
    await _post()
    backend.events = None
    r = await _post()
    assert _spoken(r) == "final:hello" and len(backend.calls) == 2

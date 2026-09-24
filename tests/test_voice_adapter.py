import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator

import httpx
import pytest

from orca_gateway import deps
from orca_gateway.channels import elevenlabs_llm
from orca_gateway.coalescer import TurnCoalescer
from orca_gateway.config import get_settings
from orca_gateway.main import app
from orca_gateway.seam import Channel, Identity, TurnEvent
from orca_gateway.tenant_concurrency import TenantConcurrencyLimiter
from orca_gateway.tenants import TenantStore
from tests.tenant_fixtures import InMemoryRepo, dental_city, quiet_spa

TRACE = "ae020331887c7f6b95acd0c22afb86fa"
TP = f"00-{TRACE}-8a2e73c1d4f50b96-01"
SECRET = "s3cret-test"


class _Backend:
    def __init__(self, events=None, delay=0.0):
        self.calls: list[dict] = []
        self.events = events
        self.delay = delay

    async def session(
        self,
        *,
        agent_id: str,
        channel: Channel,
        identity: Identity,
        tenant: str,
        turn: str,
        conversation_id: str,
    ) -> AsyncIterator[TurnEvent]:
        self.calls.append(
            {
                "agent_id": agent_id,
                "channel": channel,
                "identity": identity.authority,
                "tenant": tenant,
                "turn": turn,
                "conversation_id": conversation_id,
            }
        )
        await asyncio.sleep(self.delay)
        for e in self.events or [
            TurnEvent(type="token", data={"text": "provisional"}),
            TurnEvent(type="done", data={"answer": f"final:{turn}", "sources": []}),
        ]:
            yield e


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setenv("ORCA_VOICE_SHARED_SECRET", SECRET)
    get_settings.cache_clear()
    backend = _Backend()
    monkeypatch.setattr(deps, "get_backend", lambda: backend)
    monkeypatch.setattr(deps, "get_tenant_store", lambda: TenantStore(InMemoryRepo(dental_city())))
    monkeypatch.setattr(elevenlabs_llm, "_coalescer", TurnCoalescer(debounce_s=0.05))
    yield backend
    get_settings.cache_clear()


def _body(user="hello", **kw):
    return {
        "model": "x",
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [
            {"role": "system", "content": "THEIR TEMPLATE"},
            {"role": "assistant", "content": "earlier"},
            {"role": "user", "content": user},
        ],
        **kw,
    }


def _headers(tp=TP, auth=f"Bearer {SECRET}", tenant="dental-city"):
    h = {}
    if tenant:
        h["x-orca-tenant"] = tenant
    if tp:
        h["traceparent"] = tp
    if auth:
        h["authorization"] = auth
    return h


async def _post(body=None, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post(
            "/chat/completions", json=body or _body(), headers=headers or _headers()
        )


def _sse(text: str) -> list[str]:
    return [line[6:] for line in text.splitlines() if line.startswith("data: ")]


async def test_happy_path_speaks_done_answer_not_provisional_tokens(wired):
    r = await _post()
    assert r.status_code == 200
    frames = _sse(r.text)
    assert frames[-1] == "[DONE]"
    content = json.loads(frames[0])["choices"][0]["delta"]["content"]
    assert content == "final:hello"  # the authoritative answer; provisional tokens never spoken
    assert "provisional" not in r.text


async def test_seam_call_uses_trace_id_last_user_turn_and_ignores_their_prompt(wired):
    await _post()
    (call,) = wired.calls
    assert call["conversation_id"] == TRACE and call["turn"] == "hello"
    assert (call["channel"], call["identity"]) == ("voice", "anonymous")
    assert (call["tenant"], call["agent_id"]) == ("dental-city", "front-desk")


@pytest.mark.parametrize("tp", [None, "", "garbage", f"00-{'0' * 32}-8a2e73c1d4f50b96-01"])
async def test_missing_or_invalid_traceparent_is_400_and_never_reaches_backend(wired, tp):
    r = await _post(headers=_headers(tp=tp))
    assert r.status_code == 400 and wired.calls == []


@pytest.mark.parametrize("auth", [None, "Bearer wrong", "Bearer ", SECRET])
async def test_authorization_presence_is_not_authentication(wired, auth):
    r = await _post(headers=_headers(auth=auth))
    assert r.status_code == 401 and wired.calls == []


async def test_fails_closed_when_secret_unset(wired, monkeypatch):
    monkeypatch.setenv("ORCA_VOICE_SHARED_SECRET", "")
    get_settings.cache_clear()
    assert (await _post()).status_code == 503


async def test_non_streaming_request_is_rejected(wired):
    assert (await _post(body=_body(stream=False))).status_code == 400


async def test_upstream_error_is_502_before_any_audio_and_leaks_nothing(wired):
    wired.events = [TurnEvent(type="error", data={"message": "vendor.example.com exploded"})]
    r = await _post()
    assert r.status_code == 502 and "vendor" not in r.text


async def test_fan_out_of_four_variants_makes_exactly_one_backend_call(wired):
    variants = ["हो, अलिकति भन्न।", "हो, अलिकति भन।", "हो, अलिकति बनाउँ।", "हो, अलिकति बन्न।"]
    rs = await asyncio.gather(*[_post(body=_body(user=v)) for v in variants])
    assert [r.status_code for r in rs] == [200] * 4
    assert len(wired.calls) == 1 and wired.calls[0]["turn"] == variants[-1]


async def test_usage_chunk_only_when_backend_reports_it(wired):
    assert '"usage"' not in (await _post()).text
    wired.events = [
        TurnEvent(type="done", data={"answer": "ok", "sources": []}),
        TurnEvent(type="usage", data={"prompt_tokens": 3, "completion_tokens": 1}),
    ]
    elevenlabs_llm._coalescer = TurnCoalescer(debounce_s=0.0)
    r = await _post(headers=_headers(tp=f"00-{'1' * 32}-8a2e73c1d4f50b96-01"))
    assert '"prompt_tokens": 3' in r.text


async def test_stale_depth_gets_benign_empty_200_not_a_retryable_error(wired):
    later = _body()
    later["messages"] += [{"role": "assistant", "content": "a"}, {"role": "user", "content": "x"}]
    assert (await _post(body=later)).status_code == 200  # depth 5 advances the conversation
    calls_before = len(wired.calls)
    r = await _post(body=_body())  # depth 3: superseded
    assert r.status_code == 200 and r.text.rstrip().endswith("[DONE]")
    assert json.loads(_sse(r.text)[0])["choices"][0]["delta"]["content"] == ""
    assert len(wired.calls) == calls_before  # stale turn never reaches the backend


async def test_upstream_failure_then_retry_at_same_depth_succeeds(wired):
    wired.events = [TurnEvent(type="error", data={"message": "boom"})]
    assert (await _post()).status_code == 502
    wired.events = None
    r = await _post()  # the platform's retry, same depth and same trace-id
    assert r.status_code == 200 and "final:hello" in r.text


async def test_no_database_configured_fails_closed_with_503_not_a_traceback(wired, monkeypatch):
    monkeypatch.undo()  # drop the fixture's fakes: use the real dependency with no DB URL set
    monkeypatch.setenv("ORCA_VOICE_SHARED_SECRET", SECRET)
    monkeypatch.setenv("ORCA_DATABASE_URL", "")
    get_settings.cache_clear()
    deps.get_tenant_store.cache_clear()
    r = await _post()
    assert r.status_code == 503 and "Traceback" not in r.text
    deps.get_tenant_store.cache_clear()


ARRIVAL_LOGGER = "orca_gateway.channels.elevenlabs_llm"


def _arrivals(caplog) -> list[dict]:
    """Parse the per-request arrival log lines into dicts."""
    out = []
    for m in caplog.messages:
        if m.startswith("voice request arrival "):
            out.append(dict(p.split("=", 1) for p in m.split()[3:]))
    return out


async def test_arrival_log_records_each_request_and_the_coalescer_decision(wired, caplog):
    secret_text = "my card number is 4111 and my name is Sita"
    wired.delay = 0.3
    with caplog.at_level(logging.INFO, logger=ARRIVAL_LOGGER):
        rs = await asyncio.gather(*[_post(body=_body(user=secret_text)) for _ in range(3)])
    assert [r.status_code for r in rs] == [200] * 3

    lines = _arrivals(caplog)
    assert sorted(x["decision"] for x in lines) == ["joined", "joined", "started"]
    for x in lines:
        assert x["conversation"] == TRACE
        assert x["depth"] == "3"
        assert x["text_sha"] == hashlib.sha256(secret_text.encode()).hexdigest()[:12]
        assert x["span"] == "8a2e73c1d4f50b96"
        float(x["arrived_mono"])  # a monotonic timestamp, parseable
    # The text itself is caller data and must never be logged.
    assert "4111" not in caplog.text and "Sita" not in caplog.text


async def test_arrival_log_marks_restart_joined_late_and_stale(wired, caplog, monkeypatch):
    monkeypatch.setattr(elevenlabs_llm, "_coalescer", TurnCoalescer(debounce_s=0.3))
    wired.delay = 0.4

    async def fire(text, wait):
        await asyncio.sleep(wait)
        return await _post(body=_body(user=text))

    deeper = [{"role": "assistant", "content": "a"}, {"role": "user", "content": "x"}]
    with caplog.at_level(logging.INFO, logger=ARRIVAL_LOGGER):
        await asyncio.gather(
            fire("first hypothesis", 0.0),
            fire("second hypothesis", 0.1),  # inside the 0.3s debounce: a free restart
            fire("third hypothesis", 0.5),  # after the backend was called: joins, never cancels
        )
        later = _body(user="later")
        later["messages"] += deeper
        await _post(body=later)
        await _post(body=_body(user="older turn"))  # depth 3 after depth 5: superseded
    decisions = [x["decision"] for x in _arrivals(caplog)]
    assert decisions == ["started", "restarted", "joined_late", "started", "stale"]
    assert len(wired.calls) == 2  # one run per logical turn, never one per hypothesis
    assert wired.calls[0]["turn"] == "second hypothesis"  # the restart inside the debounce won


async def test_arrival_log_marks_a_different_text_after_the_answer_as_joined_after_done(
    wired, caplog
):
    with caplog.at_level(logging.INFO, logger=ARRIVAL_LOGGER):
        assert (await _post(body=_body(user="garbled interim"))).status_code == 200
        r = await _post(body=_body(user="the clear final transcript"))
    assert r.status_code == 200
    assert [x["decision"] for x in _arrivals(caplog)] == ["started", "joined_after_done"]
    assert len(wired.calls) == 1 and wired.calls[0]["turn"] == "garbled interim"
    # both texts appear only as hashes, and they differ
    hashes = [x["text_sha"] for x in _arrivals(caplog)]
    assert hashes[0] != hashes[1] and "clear final" not in caplog.text


def _legs(caplog) -> list[dict]:
    """Parse the diagnostic timing log lines (latency breakdown brief §1) into dicts."""
    out = []
    for m in caplog.messages:
        if m.startswith("voice latency leg="):
            out.append(dict(p.split("=", 1) for p in m.split()[2:]))
    return out


async def test_latency_legs_are_logged_in_order_for_a_completed_turn(wired, caplog):
    with caplog.at_level(logging.INFO, logger=ARRIVAL_LOGGER):
        r = await _post()
    assert r.status_code == 200

    legs = _legs(caplog)
    assert [x["leg"] for x in legs] == [
        "dispatch",
        "backend_response_received",
        "first_audio_sent",
    ]
    for x in legs:
        assert x["conversation"] == TRACE
        assert x["depth"] == "3"
        assert x["span"] == "8a2e73c1d4f50b96"
        float(x["mono"])  # a monotonic timestamp, parseable
    # monotonically non-decreasing, in the order the gateway actually does the work
    monos = [float(x["mono"]) for x in legs]
    assert monos == sorted(monos)


async def test_numbers_in_the_final_answer_are_spoken_as_words(wired, caplog):
    wired.events = [
        TurnEvent(type="token", data={"text": "provisional 5"}),
        TurnEvent(
            type="done",
            data={"answer": "\"Oral Surgery\" को मूल्य १,००,००० रुपैयाँ छ, समय 16:30।", "sources": []},
        ),
    ]
    with caplog.at_level(logging.INFO, logger=ARRIVAL_LOGGER):
        r = await _post()
    content = json.loads(_sse(r.text)[0])["choices"][0]["delta"]["content"]
    assert content == "\"Oral Surgery\" को मूल्य एक लाख रुपैयाँ छ, समय दिउँसो साढे चार बजे।"
    line = next(m for m in caplog.messages if m.startswith("voice number speech"))
    assert "conversations=2" in line and "'currency': 1" in line and "'time': 1" in line
    assert "लाख" not in caplog.text and "१,००,०००" not in caplog.text  # counts only, never text


async def test_an_answer_with_no_numbers_is_untouched_and_logs_nothing(wired, caplog):
    wired.events = [TurnEvent(type="done", data={"answer": "नमस्ते, कसरी मद्दत गरौँ?", "sources": []})]
    with caplog.at_level(logging.INFO, logger=ARRIVAL_LOGGER):
        r = await _post()
    assert json.loads(_sse(r.text)[0])["choices"][0]["delta"]["content"] == "नमस्ते, कसरी मद्दत गरौँ?"
    assert not [m for m in caplog.messages if m.startswith("voice number speech")]


async def test_the_switch_turns_number_speech_off(wired, monkeypatch):
    monkeypatch.setenv("ORCA_VOICE_NUMBER_SPEECH", "false")
    get_settings.cache_clear()
    wired.events = [TurnEvent(type="done", data={"answer": "मूल्य 1,00,000 छ", "sources": []})]
    r = await _post()
    assert json.loads(_sse(r.text)[0])["choices"][0]["delta"]["content"] == "मूल्य 1,00,000 छ"


@pytest.mark.parametrize(
    "utc_now,spoken",
    [
        # 2026-12-31 20:00 UTC is already 2027-01-01 in Kathmandu, so 2026 is NOT the current year
        ("2026-12-31T20:00:00+00:00", "मिति डिसेम्बर एकतीस, दुई हजार छब्बीस हो"),
        ("2026-06-01T06:00:00+00:00", "मिति डिसेम्बर एकतीस हो"),
    ],
)
async def test_the_current_year_is_the_tenants_local_year_not_utc(
    wired, monkeypatch, utc_now, spoken
):
    from datetime import datetime

    monkeypatch.setattr(deps, "now", lambda: datetime.fromisoformat(utc_now))
    wired.events = [TurnEvent(type="done", data={"answer": "मिति 2026-12-31 हो", "sources": []})]
    r = await _post()
    assert json.loads(_sse(r.text)[0])["choices"][0]["delta"]["content"] == spoken


# ---- P2 brief A5: per-tenant, per-channel concurrency cap -----------------------------------


class _TrackingBackend(_Backend):
    """Same as _Backend, but tracks how many backend calls are in flight AT ONCE, per tenant --
    a leak across the per-tenant cap would show up as a tenant's own max_inflight exceeding it."""

    def __init__(self, delay: float = 0.15):
        super().__init__(delay=delay)
        self.inflight: dict[str, int] = {}
        self.max_inflight: dict[str, int] = {}

    async def session(self, *, tenant, **kw):
        self.inflight[tenant] = self.inflight.get(tenant, 0) + 1
        self.max_inflight[tenant] = max(self.max_inflight.get(tenant, 0), self.inflight[tenant])
        try:
            async for event in super().session(tenant=tenant, **kw):
                yield event
        finally:
            self.inflight[tenant] -= 1


def _tp(n: int) -> str:
    return f"00-{n:032x}-8a2e73c1d4f50b96-01"


async def test_two_tenants_each_capped_at_one_never_exceed_their_own_slot(monkeypatch):
    """The scenario named in the P2 brief: dental-city and quiet-spa, each with its own
    max_concurrent_runs = 1, hammering the gateway at once. Neither tenant's own in-flight count
    may ever exceed its own cap, and -- since the two run under separate semaphores -- both sets
    of calls proceed concurrently rather than serialising against each other."""
    monkeypatch.setenv("ORCA_VOICE_SHARED_SECRET", SECRET)
    get_settings.cache_clear()
    backend = _TrackingBackend(delay=0.15)
    dental = dental_city()
    dental.channels["voice"].max_concurrent_runs = 1
    spa = quiet_spa()
    spa.channels["voice"].max_concurrent_runs = 1
    monkeypatch.setattr(deps, "get_backend", lambda: backend)
    monkeypatch.setattr(
        deps, "get_tenant_store", lambda: TenantStore(InMemoryRepo(dental, spa))
    )
    monkeypatch.setattr(
        elevenlabs_llm, "_coalescer", TurnCoalescer(debounce_s=0.0, max_concurrent_runs=4)
    )
    monkeypatch.setattr(elevenlabs_llm, "_tenant_limiter", TenantConcurrencyLimiter())

    dental_reqs = [
        _post(body=_body(user=f"d{i}"), headers=_headers(tp=_tp(i), tenant="dental-city"))
        for i in range(1, 4)
    ]
    spa_reqs = [
        _post(body=_body(user=f"s{i}"), headers=_headers(tp=_tp(100 + i), tenant="quiet-spa"))
        for i in range(1, 4)
    ]
    responses = await asyncio.gather(*dental_reqs, *spa_reqs)

    assert all(r.status_code == 200 for r in responses)
    assert backend.max_inflight["dental-city"] == 1  # never exceeded its own cap
    assert backend.max_inflight["quiet-spa"] == 1  # never exceeded its own cap, either
    assert len(backend.calls) == 6  # every request reached the backend exactly once
    tenants_called = {c["tenant"] for c in backend.calls}
    assert tenants_called == {"dental-city", "quiet-spa"}


async def test_a_configured_cap_is_never_shared_with_a_tenant_that_has_none(monkeypatch):
    """dental-city is capped at 1; quiet-spa has no cap of its own at all. quiet-spa's uncapped
    runs must never be limited by dental-city's semaphore -- proven by both tenants' calls
    reaching the backend, and quiet-spa alone allowed to exceed a count of 1 in flight."""
    monkeypatch.setenv("ORCA_VOICE_SHARED_SECRET", SECRET)
    get_settings.cache_clear()
    backend = _TrackingBackend(delay=0.15)
    dental = dental_city()
    dental.channels["voice"].max_concurrent_runs = 1
    spa = quiet_spa()  # no max_concurrent_runs: unbounded except by the environment ceiling
    monkeypatch.setattr(deps, "get_backend", lambda: backend)
    monkeypatch.setattr(
        deps, "get_tenant_store", lambda: TenantStore(InMemoryRepo(dental, spa))
    )
    monkeypatch.setattr(
        elevenlabs_llm, "_coalescer", TurnCoalescer(debounce_s=0.0, max_concurrent_runs=4)
    )
    monkeypatch.setattr(elevenlabs_llm, "_tenant_limiter", TenantConcurrencyLimiter())

    spa_reqs = [
        _post(body=_body(user=f"s{i}"), headers=_headers(tp=_tp(200 + i), tenant="quiet-spa"))
        for i in range(3)
    ]
    responses = await asyncio.gather(*spa_reqs)

    assert all(r.status_code == 200 for r in responses)
    assert backend.max_inflight["quiet-spa"] == 3  # bounded only by the environment cap of 4

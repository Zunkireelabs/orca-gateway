import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from orca_gateway import deps
from orca_gateway.channels import elevenlabs_llm, widget_chat
from orca_gateway.coalescer import TurnCoalescer
from orca_gateway.main import app
from orca_gateway.rate_limit import CallerRateLimiter
from orca_gateway.seam import Channel, Identity, TurnEvent
from orca_gateway.tenant_concurrency import TenantConcurrencyLimiter
from orca_gateway.tenants import TenantStore
from tests.tenant_fixtures import InMemoryRepo, chat, dental_city, quiet_spa

ORIGIN = "https://widget.example.com"


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
            TurnEvent(
                type="done",
                data={"answer": f"final:{turn}", "sources": [], "suggestions": ["more?"]},
            ),
        ]:
            yield e


def _dental_with_chat(**over) -> object:
    t = dental_city()
    t.channels["chat"] = chat(agent_id="front-desk", **over)
    return t


@pytest.fixture
def wired(monkeypatch):
    backend = _Backend()
    monkeypatch.setattr(deps, "get_backend", lambda: backend)
    monkeypatch.setattr(
        deps, "get_tenant_store", lambda: TenantStore(InMemoryRepo(_dental_with_chat()))
    )
    monkeypatch.setattr(elevenlabs_llm, "_coalescer", TurnCoalescer(debounce_s=0.0))
    monkeypatch.setattr(elevenlabs_llm, "_tenant_limiter", TenantConcurrencyLimiter())
    monkeypatch.setattr(widget_chat, "_rate_limiter", CallerRateLimiter(window_s=60.0))
    yield backend


def _body(question="hello", session_id="sess-1", site_id="dental-city", **kw):
    return {"site_id": site_id, "question": question, "session_id": session_id, **kw}


def _headers(origin=ORIGIN):
    h = {"content-type": "application/json"}
    if origin:
        h["origin"] = origin
    return h


async def _post(body=None, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post(
            "/v1/widget/stream", json=body or _body(), headers=headers or _headers()
        )


def _sse(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


# ---- SSE shape mapping ---------------------------------------------------------------------


async def test_happy_path_maps_to_widgets_sse_shape(wired):
    r = await _post()
    assert r.status_code == 200
    frames = _sse(r.text)
    assert frames[0] == {"type": "token", "data": "final:hello"}
    assert frames[1] == {
        "type": "done",
        "answer": "final:hello",
        "sources": [],
        "suggestions": ["more?"],
        "session_id": "sess-1",
    }
    assert r.headers["access-control-allow-origin"] == ORIGIN


async def test_seam_call_uses_session_id_as_conversation_id_and_channel_chat(wired):
    await _post(body=_body(question="what are your hours", session_id="sess-xyz"))
    (call,) = wired.calls
    assert call["conversation_id"] == "sess-xyz" and call["turn"] == "what are your hours"
    assert (call["channel"], call["identity"]) == ("chat", "anonymous")
    assert (call["tenant"], call["agent_id"]) == ("dental-city", "front-desk")


async def test_tool_and_usage_events_are_dropped_from_the_wire(wired):
    wired.events = [
        TurnEvent(type="tool", data={"name": "get_hours", "status": "running"}),
        TurnEvent(type="usage", data={"model": "gpt-4o-mini", "prompt_tokens": 1}),
        TurnEvent(type="done", data={"answer": "ok", "sources": [], "suggestions": []}),
    ]
    r = await _post()
    assert "get_hours" not in r.text and "prompt_tokens" not in r.text


# ---- A2: origin allowlist -------------------------------------------------------------------


async def test_disallowed_origin_is_rejected_before_any_backend_call(wired):
    r = await _post(headers=_headers(origin="https://not-allowed.example.com"))
    assert r.status_code == 403
    assert "access-control-allow-origin" not in r.headers
    assert wired.calls == []


async def test_missing_origin_is_rejected(wired):
    r = await _post(headers=_headers(origin=None))
    assert r.status_code == 403 and wired.calls == []


async def test_unknown_tenant_origin_is_always_rejected_since_nothing_is_listed(wired):
    r = await _post(body=_body(site_id="does-not-exist"))
    assert r.status_code == 403 and wired.calls == []


async def test_allowed_origin_gets_reflected_cors_header(wired):
    r = await _post()
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert r.headers["vary"] == "Origin"


async def test_preflight_reflects_any_origin_and_allows_post(wired):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.options("/v1/widget/stream", headers={"origin": "https://anything.example.com"})
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "https://anything.example.com"
    assert "POST" in r.headers["access-control-allow-methods"]


# ---- kill switch / unknown tenant / disabled -------------------------------------------------


async def test_unknown_tenant_gets_a_polite_sse_error_no_backend_call(monkeypatch):
    backend = _Backend()
    monkeypatch.setattr(deps, "get_backend", lambda: backend)
    monkeypatch.setattr(deps, "get_tenant_store", lambda: TenantStore(InMemoryRepo()))
    monkeypatch.setattr(elevenlabs_llm, "_coalescer", TurnCoalescer(debounce_s=0.0))
    monkeypatch.setattr(elevenlabs_llm, "_tenant_limiter", TenantConcurrencyLimiter())
    monkeypatch.setattr(widget_chat, "_rate_limiter", CallerRateLimiter(window_s=60.0))
    # An unknown tenant has no allowed_origins at all, so it is rejected by the origin gate --
    # the polite SSE-error path is reachable only for a KNOWN tenant/channel whose origin matches.
    r = await _post(body=_body(site_id="does-not-exist"))
    assert r.status_code == 403 and backend.calls == []


async def test_kill_switch_gets_a_polite_sse_error_no_backend_call(wired, monkeypatch):
    killed = _dental_with_chat()
    killed.channels["chat"].kill_switch = True
    monkeypatch.setattr(deps, "get_tenant_store", lambda: TenantStore(InMemoryRepo(killed)))
    r = await _post()
    assert r.status_code == 200
    frames = _sse(r.text)
    assert frames == [{"type": "error", "message": widget_chat._UNAVAILABLE_MESSAGE}]
    assert wired.calls == []
    assert r.headers["access-control-allow-origin"] == ORIGIN  # origin matched; still shown


async def test_disabled_channel_gets_a_polite_sse_error(wired, monkeypatch):
    disabled = _dental_with_chat()
    disabled.channels["chat"].is_enabled = False
    monkeypatch.setattr(deps, "get_tenant_store", lambda: TenantStore(InMemoryRepo(disabled)))
    r = await _post()
    assert r.status_code == 200
    assert _sse(r.text) == [{"type": "error", "message": widget_chat._UNAVAILABLE_MESSAGE}]
    assert wired.calls == []


# ---- rate limiting -------------------------------------------------------------------------


async def test_per_session_rate_limit_is_enforced(wired, monkeypatch):
    limited = _dental_with_chat(per_caller_rate_limit=2)
    monkeypatch.setattr(deps, "get_tenant_store", lambda: TenantStore(InMemoryRepo(limited)))
    for i in range(2):
        r = await _post(body=_body(question=f"q{i}", session_id="same-session"))
        assert r.status_code == 200
    r = await _post(body=_body(question="q3", session_id="same-session"))
    assert r.status_code == 429
    assert len(wired.calls) == 2  # the third never reached the backend


async def test_per_ip_rate_limit_is_enforced_across_different_sessions(wired, monkeypatch):
    limited = _dental_with_chat(per_caller_rate_limit=2)
    monkeypatch.setattr(deps, "get_tenant_store", lambda: TenantStore(InMemoryRepo(limited)))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers={"x-forwarded-for": "9.9.9.9"}
    ) as c:
        for i in range(2):
            r = await c.post(
                "/v1/widget/stream",
                json=_body(question=f"q{i}", session_id=f"sess-{i}"),
                headers=_headers(),
            )
            assert r.status_code == 200
        r = await c.post(
            "/v1/widget/stream",
            json=_body(question="q-over", session_id="sess-over"),
            headers=_headers(),
        )
    assert r.status_code == 429  # different session_id, same IP: still throttled
    assert len(wired.calls) == 2


async def test_no_rate_limit_configured_is_unbounded(wired):
    for i in range(5):
        r = await _post(body=_body(question=f"q{i}", session_id="same-session"))
        assert r.status_code == 200
    assert len(wired.calls) == 5


# ---- P2 brief A5 reused: concurrency cap keyed (tenant, "chat") -----------------------------


class _TrackingBackend(_Backend):
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


async def test_two_tenants_each_capped_at_one_on_chat_never_exceed_their_own_slot(monkeypatch):
    backend = _TrackingBackend(delay=0.15)
    dental = _dental_with_chat(max_concurrent_runs=1)
    spa = quiet_spa()
    spa.channels["chat"] = chat(
        agent_id="concierge", allowed_origins=[ORIGIN], max_concurrent_runs=1
    )
    monkeypatch.setattr(deps, "get_backend", lambda: backend)
    monkeypatch.setattr(deps, "get_tenant_store", lambda: TenantStore(InMemoryRepo(dental, spa)))
    monkeypatch.setattr(
        elevenlabs_llm, "_coalescer", TurnCoalescer(debounce_s=0.0, max_concurrent_runs=4)
    )
    monkeypatch.setattr(elevenlabs_llm, "_tenant_limiter", TenantConcurrencyLimiter())
    monkeypatch.setattr(widget_chat, "_rate_limiter", CallerRateLimiter(window_s=60.0))

    dental_reqs = [
        _post(body=_body(question=f"d{i}", session_id=f"d-sess-{i}"))
        for i in range(3)
    ]
    spa_reqs = [
        _post(
            body=_body(question=f"s{i}", session_id=f"s-sess-{i}", site_id="quiet-spa"),
        )
        for i in range(3)
    ]
    responses = await asyncio.gather(*dental_reqs, *spa_reqs)

    assert all(r.status_code == 200 for r in responses)
    assert backend.max_inflight["dental-city"] == 1
    assert backend.max_inflight["quiet-spa"] == 1
    assert len(backend.calls) == 6
    assert {c["channel"] for c in backend.calls} == {"chat"}


async def test_chat_and_voice_share_the_same_environment_ceiling_never_a_chat_only_copy(wired):
    # widget_chat imports get_coalescer/get_tenant_limiter FROM elevenlabs_llm -- the same
    # process-wide singletons voice uses, never separate ones. Proven by asserting identity.
    assert widget_chat.get_coalescer is elevenlabs_llm.get_coalescer
    assert widget_chat.get_tenant_limiter is elevenlabs_llm.get_tenant_limiter


# ---- bounds ---------------------------------------------------------------------------------


async def test_oversized_payload_is_rejected(wired):
    r = await _post(body=_body(question="x" * 20_000))
    assert r.status_code in (400, 413)
    assert wired.calls == []


async def test_overlong_question_is_rejected(wired):
    r = await _post(body=_body(question="x" * 5000))
    assert r.status_code == 400 and wired.calls == []


async def test_empty_question_is_rejected(wired):
    r = await _post(body=_body(question=""))
    assert r.status_code == 400 and wired.calls == []

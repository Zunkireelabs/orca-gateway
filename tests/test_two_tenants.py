"""S4 acceptance: two tenants, differing only as ROWS in a real Postgres, behave differently through
the same HTTP adapter with no code change between them."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from orca_gateway import deps
from orca_gateway.channels import elevenlabs_llm
from orca_gateway.coalescer import TurnCoalescer
from orca_gateway.config import get_settings
from orca_gateway.main import app
from orca_gateway.seam import TurnEvent
from orca_gateway.tenant_repo import PgTenantRepository
from orca_gateway.tenants import TenantStore
from tests.tenant_fixtures import dental_city, quiet_spa

SECRET = "s3cret-test"
TP = "00-ae020331887c7f6b95acd0c22afb86fa-8a2e73c1d4f50b96-01"
SATURDAY = datetime(2026, 9, 26, 5, 0, tzinfo=UTC)  # 10:45 Saturday in Kathmandu
SUNDAY = datetime(2026, 9, 27, 5, 0, tzinfo=UTC)


class RecordingBackend:
    def __init__(self):
        self.calls: list[dict] = []

    async def session(self, *, agent_id, channel, identity, tenant, turn, conversation_id):
        self.calls.append({"tenant": tenant, "agent_id": agent_id, "turn": turn})
        yield TurnEvent(type="done", data={"answer": f"[{tenant}/{agent_id}] you said: {turn}"})


@pytest.fixture
async def world(pg_url, monkeypatch):
    monkeypatch.setenv("ORCA_VOICE_SHARED_SECRET", SECRET)
    get_settings.cache_clear()
    repo = PgTenantRepository(pg_url)
    await repo.upsert(dental_city())
    await repo.upsert(quiet_spa())
    store = TenantStore(repo, ttl_s=1000)
    backend = RecordingBackend()
    clock = {"now": SATURDAY}
    monkeypatch.setattr(deps, "get_tenant_store", lambda: store)
    monkeypatch.setattr(deps, "get_backend", lambda: backend)
    monkeypatch.setattr(deps, "now", lambda: clock["now"])
    monkeypatch.setattr(elevenlabs_llm, "_coalescer", TurnCoalescer(debounce_s=0.0))
    yield repo, store, backend, clock
    get_settings.cache_clear()


async def ask(tenant, *, trace="ae020331887c7f6b95acd0c22afb86fa", text="hello"):
    headers = {
        "authorization": f"Bearer {SECRET}",
        "traceparent": f"00-{trace}-8a2e73c1d4f50b96-01",
    }
    if tenant:
        headers["x-orca-tenant"] = tenant
    body = {"stream": True, "messages": [{"role": "user", "content": text}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/chat/completions", json=body, headers=headers)
    spoken = None
    if r.status_code == 200:
        first = next(line[6:] for line in r.text.splitlines() if line.startswith("data: {"))
        spoken = json.loads(first)["choices"][0]["delta"]["content"]
    return r.status_code, spoken


async def test_same_request_two_tenants_two_behaviours_zero_code_change(world):
    _, _, backend, _ = world  # Saturday morning in Kathmandu

    # Tenant A: open, hands off to its own agent, on its own backend.
    status, spoken = await ask("dental-city", trace="a" * 32)
    assert (status, spoken) == (200, "[dental-city/front-desk] you said: hello")

    # Tenant B: SAME request, differs only in the header -> its row says "closed on Saturdays,
    # say so, in my brand's spoken name". The backend must never be called.
    status, spoken = await ask("quiet-spa", trace="b" * 32)
    assert (status, spoken) == (200, "Thank you for calling Quiet Spa. We are closed today.")
    assert [c["tenant"] for c in backend.calls] == ["dental-city"]


async def test_the_behaviour_follows_the_row_not_the_code(world):
    _, _, backend, clock = world
    clock["now"] = SUNDAY  # tenant B's Saturday closure no longer applies
    status, spoken = await ask("quiet-spa", trace="c" * 32)
    assert (status, spoken) == (200, "[quiet-spa/concierge] you said: hello")
    assert backend.calls == [{"tenant": "quiet-spa", "agent_id": "concierge", "turn": "hello"}]


async def test_editing_the_row_changes_behaviour_without_a_deploy(world):
    repo, store, backend, _ = world
    upd = quiet_spa()
    upd.channels["voice"].closed_weekdays = []  # the tenant reopens Saturdays: a DATA edit
    await repo.upsert(upd)
    store.invalidate("quiet-spa")
    status, spoken = await ask("quiet-spa", trace="d" * 32)
    assert spoken.startswith("[quiet-spa/concierge]")


async def test_no_default_tenant_missing_or_unknown_fails_closed(world):
    _, _, backend, _ = world
    assert (await ask(None))[0] == 400  # no header: a client error, never a guess
    assert (await ask("Bad Slug!"))[0] == 400
    assert (await ask("nope"))[0] == 403  # unknown: refused
    assert backend.calls == []


async def test_kill_switch_stops_the_tenant_dead_and_only_that_tenant(world):
    repo, store, backend, _ = world
    assert (await ask("dental-city", trace="e" * 32))[0] == 200
    await repo.set_kill_switch("dental-city", "voice", True)
    store.invalidate("dental-city")  # the explicit hook; otherwise it waits out the TTL
    calls_before = len(backend.calls)
    assert (await ask("dental-city", trace="f" * 32))[0] == 403
    assert len(backend.calls) == calls_before  # nothing reached the brain
    assert (await ask("quiet-spa", trace="1" * 32))[0] == 200  # the other tenant is unaffected


async def test_inactive_tenant_and_disabled_channel_are_refused(world):
    repo, store, _, _ = world
    off = quiet_spa().model_copy(update={"is_active": False})
    await repo.upsert(off)
    store.invalidate()
    assert (await ask("quiet-spa"))[0] == 403
    dc = dental_city()
    dc.channels["voice"].is_enabled = False
    await repo.upsert(dc)
    store.invalidate()
    assert (await ask("dental-city"))[0] == 403

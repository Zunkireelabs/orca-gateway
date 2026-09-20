"""End-to-end through the voice adapter, against a REAL Postgres: S5 acceptance §2 (exactly one
row per call), §3 (max_session_seconds), §4 (daily_spend_cap)."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import pytest

from orca_gateway import deps
from orca_gateway.calls_repo import PgCallsRepository
from orca_gateway.channels import elevenlabs_llm
from orca_gateway.coalescer import TurnCoalescer
from orca_gateway.config import get_settings
from orca_gateway.main import app
from orca_gateway.seam import Channel, Identity, TurnEvent
from orca_gateway.tenant_repo import PgTenantRepository
from orca_gateway.tenants import TenantStore
from tests.tenant_fixtures import InMemoryRepo, dental_city

TRACE = "ae020331887c7f6b95acd0c22afb86fa"
TP = f"00-{TRACE}-8a2e73c1d4f50b96-01"
SECRET = "s3cret-test"


class _Backend:
    def __init__(self):
        self.calls: list[str] = []

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
        self.calls.append(conversation_id)
        yield TurnEvent(type="done", data={"answer": f"final:{turn}", "sources": []})


@pytest.fixture
async def wired(pg_url, monkeypatch):
    """Real Postgres for tenants + metering; an in-memory tenant STORE (so channel config, incl.
    caps, can be tweaked per-test) whose slug matches a row actually persisted in orca_gw.tenants
    (calls.tenant_id's FK needs a real row)."""
    await PgTenantRepository(pg_url).upsert(dental_city())
    monkeypatch.setenv("ORCA_VOICE_SHARED_SECRET", SECRET)
    get_settings.cache_clear()
    backend = _Backend()
    tenant_cfg = dental_city()
    monkeypatch.setattr(deps, "get_backend", lambda: backend)
    monkeypatch.setattr(deps, "get_tenant_store", lambda: TenantStore(InMemoryRepo(tenant_cfg)))
    monkeypatch.setattr(deps, "get_calls_repo", lambda: PgCallsRepository(pg_url))
    monkeypatch.setattr(elevenlabs_llm, "_coalescer", TurnCoalescer(debounce_s=0.05))
    yield backend, tenant_cfg, pg_url
    get_settings.cache_clear()


def _body(user="hello", **kw):
    return {
        "model": "x",
        "stream": True,
        "messages": [
            {"role": "system", "content": "THEIR TEMPLATE"},
            {"role": "assistant", "content": "earlier"},
            {"role": "user", "content": user},
        ],
        **kw,
    }


def _headers(tp=TP, auth=f"Bearer {SECRET}", tenant="dental-city"):
    return {"x-orca-tenant": tenant, "traceparent": tp, "authorization": auth}


async def _post(body=None, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post(
            "/chat/completions", json=body or _body(), headers=headers or _headers()
        )


def _sse(text: str) -> list[str]:
    return [line[6:] for line in text.splitlines() if line.startswith("data: ")]


async def test_multi_turn_conversation_produces_exactly_one_row(wired):
    backend, _, pg_url = wired
    r1 = await _post(body=_body("hi"))
    assert r1.status_code == 200

    later = _body("bye")
    later["messages"] += [{"role": "assistant", "content": "a"}, {"role": "user", "content": "x"}]
    r2 = await _post(body=later)
    assert r2.status_code == 200

    assert len(backend.calls) == 2  # two real turns

    with psycopg.connect(pg_url) as conn:
        rows = conn.execute(
            "select turn_count from orca_gw.calls where conversation_id = %s", (TRACE,)
        ).fetchall()
    assert rows == [(2,)]  # exactly one row, turn_count reflects both turns


async def test_max_session_seconds_exceeded_gets_handoff_not_hard_error(wired):
    backend, tenant_cfg, pg_url = wired
    tenant_cfg.channels["voice"].max_session_seconds = 60

    r1 = await _post(body=_body("hi"))
    assert r1.status_code == 200
    assert len(backend.calls) == 1

    # Backdate the call's started_at so the NEXT turn looks like it's past the session limit.
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(
            "update orca_gw.calls set started_at = %s where conversation_id = %s",
            (datetime.now(UTC) - timedelta(seconds=120), TRACE),
        )

    later = _body("still there?")
    later["messages"] += [{"role": "assistant", "content": "a"}, {"role": "user", "content": "x"}]
    r2 = await _post(body=later)

    assert r2.status_code == 200
    assert len(backend.calls) == 1  # backend NOT called for the over-limit turn
    content = json.loads(_sse(r2.text)[0])["choices"][0]["delta"]["content"]
    assert "time limit" in content
    assert "डेन्टल सिटी" in content  # {brand} was substituted with spoken_brand_name
    assert "{brand}" not in content


async def test_daily_spend_cap_refuses_new_call_before_reaching_backend(wired):
    backend, tenant_cfg, pg_url = wired
    tenant_cfg.channels["voice"].daily_spend_cap = 1.00

    with psycopg.connect(pg_url) as conn:
        tenant_id = conn.execute(
            "select id from orca_gw.tenants where slug = 'dental-city'"
        ).fetchone()[0]
        conn.execute(
            "insert into orca_gw.tenant_daily_spend "
            "(tenant_id, spend_date, llm_cost_usd, call_count) "
            "values (%s, (now() at time zone 'utc')::date, 5.00, 3)",
            (tenant_id,),
        )
        conn.commit()

    r = await _post(body=_body("hello"))  # a brand new conversation_id (first turn)
    assert r.status_code == 403
    assert backend.calls == []  # never reached the backend


async def test_daily_spend_cap_does_not_cut_off_an_already_running_call(wired):
    """The cap gates a call's FIRST turn only -- a call already in progress when the tenant hits
    its cap is not cut off mid-conversation (S5 brief §3.4)."""
    backend, tenant_cfg, pg_url = wired
    r1 = await _post(body=_body("hi"))
    assert r1.status_code == 200

    tenant_cfg.channels["voice"].daily_spend_cap = 0.00  # now "at cap" for any NEW call

    later = _body("continuing")
    later["messages"] += [{"role": "assistant", "content": "a"}, {"role": "user", "content": "x"}]
    r2 = await _post(body=later)
    assert r2.status_code == 200
    assert len(backend.calls) == 2

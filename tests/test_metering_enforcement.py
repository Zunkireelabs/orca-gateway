"""End-to-end through the voice adapter, against a REAL Postgres: S5 acceptance §2 (exactly one
row per call), §3 (max_session_seconds), §4 (daily_spend_cap)."""

import asyncio
import json
import logging
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
from orca_gateway.cost import cost_usd
from orca_gateway.main import app
from orca_gateway.seam import Channel, Identity, TurnEvent
from orca_gateway.tenant_repo import PgTenantRepository
from orca_gateway.tenants import TenantStore
from tests.tenant_fixtures import InMemoryRepo, dental_city

TRACE = "ae020331887c7f6b95acd0c22afb86fa"
TP = f"00-{TRACE}-8a2e73c1d4f50b96-01"
SECRET = "s3cret-test"


USAGE = {"model": "gpt-4o-mini", "prompt_tokens": 3000, "completion_tokens": 100}


class _Backend:
    def __init__(self, *, delay=0.0):
        self.calls: list[str] = []
        self.delay = delay  # seconds a run takes; a cancelled run never reaches its events

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
        await asyncio.sleep(self.delay)
        yield TurnEvent(type="done", data={"answer": f"final:{turn}", "sources": []})
        yield TurnEvent(type="usage", data=dict(USAGE))


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


def _row(pg_url, columns):
    with psycopg.connect(pg_url) as conn:
        return conn.execute(
            f"select {columns} from orca_gw.calls where conversation_id = %s", (TRACE,)
        ).fetchone()


def _deeper(body, extra=2):
    pair = [{"role": "assistant", "content": "a"}, {"role": "user", "content": "x"}]
    body["messages"] += pair * (extra // 2)
    return body


async def test_fan_out_duplicates_count_tokens_cost_turn_and_spend_exactly_once(wired):
    """The platform sends N duplicate requests per spoken turn; they share ONE result. The
    duplicates must not each add the usage again (the bug this regression-tests: a 6-request
    fan-out recorded 6x the tokens)."""
    backend, _, pg_url = wired
    rs = await asyncio.gather(*[_post(body=_body("hi")) for _ in range(6)])
    assert [r.status_code for r in rs] == [200] * 6
    assert len(backend.calls) == 1

    tokens, cost, turns = _row(pg_url, "llm_prompt_tokens, llm_cost_usd, turn_count")[0:3]
    expected = cost_usd("gpt-4o-mini", 3000, 100)
    assert (tokens, turns) == (3000, 1)
    assert float(cost) == expected

    await PgCallsRepository(pg_url).close_call(TRACE, "completed")
    assert await PgCallsRepository(pg_url).daily_spend_usd("dental-city") == expected


async def test_slow_backend_with_alternating_hypotheses_is_one_call_and_no_abandoned_runs(
    wired, monkeypatch
):
    """The live failure shape (a slow turn while the platform alternates hypotheses): with the
    commit rule there is exactly one backend call, one turn, one run's usage, and nothing was
    abandoned. (Before the rule, every flip cancelled a paid run: 16 abandoned in one call.)"""
    backend, _, pg_url = wired
    backend.delay = 1.2
    monkeypatch.setattr(elevenlabs_llm, "_coalescer", TurnCoalescer(debounce_s=0.05))

    async def fire(text, wait):
        await asyncio.sleep(wait)
        return await _post(body=_body(text))

    rs = await asyncio.gather(*[fire(f"hypothesis {i % 2}", 0.1 + i * 0.08) for i in range(12)])
    assert [r.status_code for r in rs] == [200] * 12
    assert len(backend.calls) == 1

    tokens, completion, cost, turns, abandoned = _row(
        pg_url,
        "llm_prompt_tokens, llm_completion_tokens, llm_cost_usd, turn_count, abandoned_run_count",
    )
    assert (tokens, completion, turns, abandoned) == (3000, 100, 1, 0)
    assert float(cost) == cost_usd("gpt-4o-mini", 3000, 100)


async def test_a_run_that_fails_is_counted_abandoned_not_silently_lost(wired, monkeypatch):
    """abandoned_run_count now means real failures (a run that reached the backend and did not
    complete), no longer coalescer restarts."""
    backend, _, pg_url = wired

    async def session(**kw):
        raise ConnectionError("backend down")
        yield  # pragma: no cover

    monkeypatch.setattr(backend, "session", session)
    assert (await _post(body=_body("hi"))).status_code == 502
    assert _row(pg_url, "turn_count, abandoned_run_count, llm_prompt_tokens") == (0, 1, None)


async def test_incomplete_usage_payload_is_not_defaulted_to_zero(wired, monkeypatch):
    backend, _, pg_url = wired

    async def session(**kw):
        yield TurnEvent(type="done", data={"answer": "ok", "sources": []})
        yield TurnEvent(type="usage", data={"model": "gpt-4o-mini"})  # no token counts

    monkeypatch.setattr(backend, "session", session)
    assert (await _post(body=_body("hi"))).status_code == 200
    assert _row(pg_url, "llm_prompt_tokens, llm_cost_usd, turn_count") == (None, None, 1)


async def test_max_session_trip_is_logged_and_recorded_on_the_row(wired, caplog):
    backend, tenant_cfg, pg_url = wired
    tenant_cfg.channels["voice"].max_session_seconds = 60
    assert (await _post(body=_body("hi"))).status_code == 200
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(
            "update orca_gw.calls set started_at = %s where conversation_id = %s",
            (datetime.now(UTC) - timedelta(seconds=120), TRACE),
        )
    with caplog.at_level(logging.WARNING, logger="orca_gateway.channels.elevenlabs_llm"):
        r = await _post(body=_deeper(_body("again")))
    assert r.status_code == 200 and len(backend.calls) == 1
    assert any(
        "max_session_seconds tripped" in m
        and "tenant=dental-city" in m
        and f"conversation={TRACE}" in m
        and "elapsed=" in m
        for m in caplog.messages
    )
    ended_at, reason = _row(pg_url, "ended_at, ended_reason")
    assert ended_at is not None and reason == "max_session"

    # The gateway cannot hang up: a later turn gets the same clean handoff, not an error.
    r2 = await _post(body=_deeper(_body("still?"), 4))
    assert r2.status_code == 200 and len(backend.calls) == 1


async def test_daily_spend_cap_trip_is_logged_recorded_and_keeps_refusing(wired, caplog):
    backend, tenant_cfg, pg_url = wired
    tenant_cfg.channels["voice"].daily_spend_cap = 1.00
    with psycopg.connect(pg_url) as conn:
        tenant_id = conn.execute(
            "select id from orca_gw.tenants where slug='dental-city'"
        ).fetchone()[0]
        conn.execute(
            "insert into orca_gw.tenant_daily_spend "
            "(tenant_id, spend_date, llm_cost_usd, call_count) "
            "values (%s, (now() at time zone 'utc')::date, 5.00, 3)",
            (tenant_id,),
        )
        conn.commit()
    with caplog.at_level(logging.WARNING, logger="orca_gateway.channels.elevenlabs_llm"):
        assert (await _post(body=_body("hi"))).status_code == 403
    assert any(
        "daily_spend_cap tripped" in m and f"conversation={TRACE}" in m for m in caplog.messages
    )
    assert _row(pg_url, "ended_reason, turn_count") == ("daily_spend_cap", 0)

    # The refusal is sticky for that call: a retry must not slip through because a row now exists.
    assert (await _post(body=_deeper(_body("retry")))).status_code == 403
    assert backend.calls == []

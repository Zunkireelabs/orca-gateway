"""Against a REAL Postgres, seeded through calls_repo: reporting.py's query shape (S5 brief §3.5,
acceptance §7 -- reporting.py's functions return correct numbers against a seeded test db)."""

import pytest

from orca_gateway.calls_repo import PgCallsRepository
from orca_gateway.cost import cost_usd
from orca_gateway.reporting import Reporting
from orca_gateway.tenant_repo import PgTenantRepository
from tests.tenant_fixtures import dental_city, quiet_spa


@pytest.fixture
async def seeded(pg_url):
    tenants = PgTenantRepository(pg_url)
    await tenants.upsert(dental_city())
    await tenants.upsert(quiet_spa())
    calls = PgCallsRepository(pg_url)

    # dental-city: two calls today, one closed with real usage, one still open.
    await calls.record_turn(
        tenant_slug="dental-city",
        channel="voice",
        conversation_id="dc-closed",
        agent_id="front-desk",
        elevenlabs_agent_id=None,
    )
    await calls.record_usage(
        conversation_id="dc-closed", model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=200
    )
    await calls.close_call("dc-closed", "completed")

    await calls.record_turn(
        tenant_slug="dental-city",
        channel="voice",
        conversation_id="dc-open",
        agent_id="front-desk",
        elevenlabs_agent_id=None,
    )

    # quiet-spa: one closed call, no usage ever arrived (llm_cost_usd stays null throughout).
    await calls.record_turn(
        tenant_slug="quiet-spa",
        channel="voice",
        conversation_id="qs-closed",
        agent_id="concierge",
        elevenlabs_agent_id=None,
    )
    await calls.close_call("qs-closed", "timed_out")

    return pg_url


async def test_per_tenant_cost_this_month(seeded):
    rows = {r["slug"]: r for r in await Reporting(seeded).per_tenant_cost_this_month()}
    assert rows["dental-city"]["llm_cost_usd"] == cost_usd("gpt-4o-mini", 1000, 200)
    assert rows["dental-city"]["call_count"] == 1
    assert rows["dental-city"]["unpriced_call_count"] == 0
    # quiet-spa closed a call but never got a real usage event: a true sum over zero priced
    # calls ($0.0), with unpriced_call_count saying so -- never a fabricated "no cost" claim.
    assert rows["quiet-spa"]["llm_cost_usd"] == 0.0
    assert rows["quiet-spa"]["call_count"] == 1
    assert rows["quiet-spa"]["unpriced_call_count"] == 1


async def test_calls_today_across_all_tenants(seeded):
    assert await Reporting(seeded).calls_today() == 3


async def test_calls_today_scoped_to_one_tenant(seeded):
    assert await Reporting(seeded).calls_today("dental-city") == 2
    assert await Reporting(seeded).calls_today("quiet-spa") == 1


async def test_open_calls_lists_only_unended(seeded):
    open_calls = await Reporting(seeded).open_calls()
    assert [c["conversation_id"] for c in open_calls] == ["dc-open"]
    assert open_calls[0]["tenant_slug"] == "dental-city"


async def test_calls_today_zero_when_nothing_seeded(pg_url):
    assert await Reporting(pg_url).calls_today() == 0
    assert await Reporting(pg_url).open_calls() == []
    assert await Reporting(pg_url).per_tenant_cost_this_month() == []

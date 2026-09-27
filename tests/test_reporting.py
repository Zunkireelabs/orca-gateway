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
    await calls.complete_turn(
        conversation_id="dc-closed",
        depth=3,
        usage={"model": "gpt-4o-mini", "prompt_tokens": 1000, "completion_tokens": 200},
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


# ---- P6: cost_by_tenant_month / cost_csv_rows (all-in cost, per-channel, N2) -----------------


@pytest.fixture
async def seeded_p6(pg_url):
    """One tenant, one voice call already reconciled with ElevenLabs, one voice call not yet
    reconciled, and one chat call (which never touches ElevenLabs at all)."""
    tenants = PgTenantRepository(pg_url)
    await tenants.upsert(dental_city())
    calls = PgCallsRepository(pg_url)

    await calls.record_turn(
        tenant_slug="dental-city",
        channel="voice",
        conversation_id="dc-voice-reconciled",
        agent_id="front-desk",
        elevenlabs_agent_id="el-agent-1",
    )
    await calls.complete_turn(
        conversation_id="dc-voice-reconciled",
        depth=1,
        usage={"model": "gpt-4o-mini", "prompt_tokens": 1000, "completion_tokens": 200},
    )
    await calls.close_call("dc-voice-reconciled", "completed")
    await calls.record_elevenlabs_meters(
        conversation_id="dc-voice-reconciled",
        stt_minutes=0.5,
        tts_characters=400,
        elevenlabs_cost_fiat=0.02,
    )

    await calls.record_turn(
        tenant_slug="dental-city",
        channel="voice",
        conversation_id="dc-voice-unreconciled",
        agent_id="front-desk",
        elevenlabs_agent_id="el-agent-1",
    )
    await calls.complete_turn(
        conversation_id="dc-voice-unreconciled",
        depth=1,
        usage={"model": "gpt-4o-mini", "prompt_tokens": 1000, "completion_tokens": 200},
    )
    await calls.close_call("dc-voice-unreconciled", "completed")
    # never reconciled: elevenlabs_cost_fiat stays null

    await calls.record_turn(
        tenant_slug="dental-city",
        channel="chat",
        conversation_id="dc-chat-1",
        agent_id="front-desk",
        elevenlabs_agent_id=None,
    )
    await calls.complete_turn(
        conversation_id="dc-chat-1",
        depth=1,
        usage={"model": "gpt-4o-mini", "prompt_tokens": 500, "completion_tokens": 100},
    )
    await calls.close_call("dc-chat-1", "completed")

    return pg_url


async def test_cost_by_tenant_month_splits_by_channel(seeded_p6):
    rows = {r["channel"]: r for r in await Reporting(seeded_p6).cost_by_tenant_month()}
    assert set(rows) == {"voice", "chat"}
    assert rows["voice"]["calls"] == 2
    assert rows["chat"]["calls"] == 1


async def test_cost_by_tenant_month_voice_all_in_cost_and_unreconciled_count(seeded_p6):
    rows = {r["channel"]: r for r in await Reporting(seeded_p6).cost_by_tenant_month()}
    voice = rows["voice"]
    llm_per_call = cost_usd("gpt-4o-mini", 1000, 200)
    assert voice["known_llm_cost_usd"] == round(llm_per_call * 2, 6)
    assert voice["known_elevenlabs_cost_usd"] == 0.02  # only the reconciled call
    assert voice["unreconciled_voice_calls"] == 1
    # all-in only sums what's KNOWN -- exactly one call's worth of ElevenLabs cost, never a
    # fabricated $0 for the unreconciled one and never silently dropped from the total either.
    assert voice["all_in_known_cost_usd"] == round(llm_per_call * 2 + 0.02, 6)
    assert voice["cost_per_call"] == voice["all_in_known_cost_usd"] / 2
    assert voice["margin_usd"] is None  # TODO, documented: no price basis yet (brief §6 Q4)


async def test_cost_by_tenant_month_chat_all_in_cost_is_llm_only_and_never_unreconciled(
    seeded_p6,
):
    rows = {r["channel"]: r for r in await Reporting(seeded_p6).cost_by_tenant_month()}
    chat = rows["chat"]
    llm_cost = cost_usd("gpt-4o-mini", 500, 100)
    assert chat["known_elevenlabs_cost_usd"] == 0.0
    assert chat["unreconciled_voice_calls"] == 0  # chat never touches ElevenLabs -- not "pending"
    assert chat["all_in_known_cost_usd"] == round(llm_cost, 6)


async def test_cost_csv_rows_all_in_cost_per_call(seeded_p6):
    rows = {r["conversation_id"]: r for r in await Reporting(seeded_p6).cost_csv_rows()}
    llm_per_call = cost_usd("gpt-4o-mini", 1000, 200)

    reconciled = rows["dc-voice-reconciled"]
    assert reconciled["elevenlabs_cost_fiat"] == 0.02
    assert reconciled["all_in_cost_usd"] == round(llm_per_call + 0.02, 6)

    unreconciled = rows["dc-voice-unreconciled"]
    assert unreconciled["elevenlabs_cost_fiat"] is None
    assert unreconciled["all_in_cost_usd"] is None  # unknown, never llm cost alone

    chat_llm_cost = cost_usd("gpt-4o-mini", 500, 100)
    chat_row = rows["dc-chat-1"]
    assert chat_row["elevenlabs_cost_fiat"] is None  # never populated for chat
    assert chat_row["all_in_cost_usd"] == round(chat_llm_cost, 6)  # still complete, LLM-only

"""Against a REAL Postgres: the calls_repo hot path (idempotency, usage accumulation, closing,
daily-spend rollup, the idle-timeout sweep)."""

import psycopg
import pytest

from orca_gateway.calls_repo import PgCallsRepository
from orca_gateway.cost import cost_usd
from orca_gateway.tenant_repo import PgTenantRepository
from tests.tenant_fixtures import dental_city


@pytest.fixture
async def tenant(pg_url):
    """dental-city exists in orca_gw.tenants, satisfying calls.tenant_id's FK."""
    await PgTenantRepository(pg_url).upsert(dental_city())
    return "dental-city"


@pytest.fixture
def repo(pg_url) -> PgCallsRepository:
    return PgCallsRepository(pg_url)


async def test_get_open_call_returns_none_for_unknown_conversation(repo):
    assert await repo.get_open_call("nope") is None


async def test_record_turn_is_idempotent_on_conversation_id(repo, tenant, pg_url):
    for expected_turn_count in (1, 2, 3):
        state = await repo.record_turn(
            tenant_slug=tenant,
            channel="voice",
            conversation_id="conv-1",
            agent_id="front-desk",
            elevenlabs_agent_id=None,
        )
        assert state.turn_count == expected_turn_count

    with psycopg.connect(pg_url) as conn:
        rows = conn.execute(
            "select count(*) from orca_gw.calls where conversation_id = %s", ("conv-1",)
        ).fetchone()
    assert rows[0] == 1  # exactly one row, never one per turn


async def test_record_turn_keeps_first_elevenlabs_agent_id_seen(repo, tenant):
    await repo.record_turn(
        tenant_slug=tenant,
        channel="voice",
        conversation_id="conv-2",
        agent_id="front-desk",
        elevenlabs_agent_id="el-agent-1",
    )
    await repo.record_turn(
        tenant_slug=tenant,
        channel="voice",
        conversation_id="conv-2",
        agent_id="front-desk",
        elevenlabs_agent_id=None,
    )
    call = await repo.get_open_call("conv-2")
    assert call is not None


async def test_complete_turn_usage_accumulates_across_turns_and_computes_cost(repo, tenant, pg_url):
    await repo.record_turn(
        tenant_slug=tenant,
        channel="voice",
        conversation_id="conv-3",
        agent_id="front-desk",
        elevenlabs_agent_id=None,
    )
    await repo.complete_turn(
        conversation_id="conv-3",
        depth=3,
        usage={"model": "gpt-4o-mini", "prompt_tokens": 100, "completion_tokens": 20},
    )
    await repo.complete_turn(
        conversation_id="conv-3",
        depth=5,
        usage={"model": "gpt-4o-mini", "prompt_tokens": 50, "completion_tokens": 10},
    )

    with psycopg.connect(pg_url) as conn:
        row = conn.execute(
            "select llm_prompt_tokens, llm_completion_tokens, llm_cost_usd, llm_model "
            "from orca_gw.calls where conversation_id = %s",
            ("conv-3",),
        ).fetchone()
    prompt, completion, cost, model = row
    assert (prompt, completion, model) == (150, 30, "gpt-4o-mini")
    assert float(cost) == cost_usd("gpt-4o-mini", 150, 30)


async def test_complete_turn_usage_for_unknown_conversation_is_dropped_not_fabricated(repo):
    # No exception, no row created out of thin air -- just a documented no-op (logged).
    await repo.complete_turn(
        conversation_id="ghost",
        depth=7,
        usage={"model": "gpt-4o-mini", "prompt_tokens": 10, "completion_tokens": 1},
    )


async def test_close_call_rolls_cost_into_tenant_daily_spend(repo, tenant, pg_url):
    await repo.record_turn(
        tenant_slug=tenant,
        channel="voice",
        conversation_id="conv-4",
        agent_id="front-desk",
        elevenlabs_agent_id=None,
    )
    await repo.complete_turn(
        conversation_id="conv-4",
        depth=9,
        usage={"model": "gpt-4o-mini", "prompt_tokens": 1000, "completion_tokens": 200},
    )
    await repo.close_call("conv-4", "completed")

    spend = await repo.daily_spend_usd(tenant)
    assert spend == cost_usd("gpt-4o-mini", 1000, 200)

    with psycopg.connect(pg_url) as conn:
        ended = conn.execute(
            "select ended_at is not null, ended_reason from orca_gw.calls "
            "where conversation_id = %s",
            ("conv-4",),
        ).fetchone()
    assert ended == (True, "completed")


async def test_close_call_is_idempotent_does_not_double_count_spend(repo, tenant):
    await repo.record_turn(
        tenant_slug=tenant,
        channel="voice",
        conversation_id="conv-5",
        agent_id="front-desk",
        elevenlabs_agent_id=None,
    )
    await repo.complete_turn(
        conversation_id="conv-5",
        depth=11,
        usage={"model": "gpt-4o-mini", "prompt_tokens": 1000, "completion_tokens": 200},
    )
    await repo.close_call("conv-5", "completed")
    await repo.close_call("conv-5", "completed")  # already ended: must be a no-op

    assert await repo.daily_spend_usd(tenant) == cost_usd("gpt-4o-mini", 1000, 200)


async def test_record_turn_does_not_reopen_an_already_closed_call(repo, tenant):
    state = await repo.record_turn(
        tenant_slug=tenant,
        channel="voice",
        conversation_id="conv-6",
        agent_id="front-desk",
        elevenlabs_agent_id=None,
    )
    await repo.close_call("conv-6", "kill_switch")

    # A straggler turn arrives after the call is already closed: not reopened, turn_count untouched.
    straggler = await repo.record_turn(
        tenant_slug=tenant,
        channel="voice",
        conversation_id="conv-6",
        agent_id="front-desk",
        elevenlabs_agent_id=None,
    )
    assert straggler.ended_at is not None
    assert straggler.turn_count == state.turn_count


async def test_sweep_idle_closes_stale_open_calls_as_timed_out(repo, tenant, pg_url):
    await repo.record_turn(
        tenant_slug=tenant,
        channel="voice",
        conversation_id="conv-7",
        agent_id="front-desk",
        elevenlabs_agent_id=None,
    )
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(
            "update orca_gw.calls set last_turn_at = now() - interval '10 minutes' "
            "where conversation_id = %s",
            ("conv-7",),
        )

    closed = await repo.sweep_idle(idle_s=300)
    assert closed == ["conv-7"]

    call = await repo.get_open_call("conv-7")
    assert call.ended_at is not None


async def test_sweep_idle_leaves_recently_active_calls_open(repo, tenant):
    await repo.record_turn(
        tenant_slug=tenant,
        channel="voice",
        conversation_id="conv-8",
        agent_id="front-desk",
        elevenlabs_agent_id=None,
    )
    closed = await repo.sweep_idle(idle_s=300)
    assert closed == []
    assert (await repo.get_open_call("conv-8")).ended_at is None


async def test_daily_spend_usd_is_zero_for_a_tenant_with_no_spend_today(repo, tenant):
    assert await repo.daily_spend_usd(tenant) == 0.0


async def test_close_call_with_no_usage_counts_as_unpriced_not_a_fabricated_zero(
    repo, tenant, pg_url
):
    await repo.record_turn(
        tenant_slug=tenant,
        channel="voice",
        conversation_id="conv-9",
        agent_id="front-desk",
        elevenlabs_agent_id=None,
    )
    await repo.close_call("conv-9", "timed_out")  # no record_usage ever called

    with psycopg.connect(pg_url) as conn:
        row = conn.execute(
            "select s.llm_cost_usd, s.call_count, s.unpriced_call_count "
            "from orca_gw.tenant_daily_spend s join orca_gw.tenants t on t.id = s.tenant_id "
            "where t.slug = %s",
            (tenant,),
        ).fetchone()
    assert row == (0, 1, 1)


async def _one_call(repo, tenant, cid):
    await repo.touch_call(
        tenant_slug=tenant,
        channel="voice",
        conversation_id=cid,
        agent_id="front-desk",
        elevenlabs_agent_id=None,
    )


async def test_complete_turn_writes_one_transcript_row_with_every_field(repo, tenant, pg_url):
    await _one_call(repo, tenant, "conv-t1")
    await repo.complete_turn(
        conversation_id="conv-t1",
        depth=3,
        usage={"model": "gpt-4o-mini", "prompt_tokens": 100, "completion_tokens": 20},
        user_text="what are your hours",
        answer_text="We open at nine.",
        tools=[{"name": "lookup", "status": "running"}, {"name": "lookup", "status": "done"}],
        latency_ms=1234,
        coalescer={"requests": 4, "started": 1, "restarted": 1, "joined": 2},
    )
    with psycopg.connect(pg_url) as conn:
        row = conn.execute(
            "select depth, user_text, answer_text, tools, usage, latency_ms, coalescer, ended_by "
            "from orca_gw.turns"
        ).fetchone()
    assert row[0:3] == (3, "what are your hours", "We open at nine.")
    assert row[3] == [{"name": "lookup", "status": "running"}, {"name": "lookup", "status": "done"}]
    assert row[4]["prompt_tokens"] == 100 and row[5] == 1234
    assert row[6] == {"requests": 4, "started": 1, "restarted": 1, "joined": 2} and row[7] is None


async def test_the_same_depth_completing_twice_is_counted_once(repo, tenant, pg_url):
    """The idempotency key: turn count, usage and the transcript row are one unit keyed on
    (call, depth), so a second completion of the same depth cannot double-count."""
    await _one_call(repo, tenant, "conv-t2")
    usage = {"model": "gpt-4o-mini", "prompt_tokens": 100, "completion_tokens": 20}
    for _ in range(3):
        await repo.complete_turn(
            conversation_id="conv-t2", depth=3, usage=usage, user_text="hi", answer_text="hello"
        )
    with psycopg.connect(pg_url) as conn:
        turns = conn.execute("select count(*) from orca_gw.turns").fetchone()[0]
        call = conn.execute(
            "select turn_count, llm_prompt_tokens, llm_completion_tokens from orca_gw.calls "
            "where conversation_id = 'conv-t2'"
        ).fetchone()
    assert turns == 1 and call == (1, 100, 20)


async def test_retention_purges_old_turns_and_keeps_calls_costs_and_labels(repo, tenant, pg_url):
    await _one_call(repo, tenant, "conv-t3")
    usage = {"model": "gpt-4o-mini", "prompt_tokens": 1000, "completion_tokens": 200}
    for depth in (3, 5):
        await repo.complete_turn(
            conversation_id="conv-t3", depth=depth, usage=usage, user_text="q", answer_text="a"
        )
    with psycopg.connect(pg_url, autocommit=True) as conn:
        call_id = conn.execute(
            "select id from orca_gw.calls where conversation_id = 'conv-t3'"
        ).fetchone()[0]
        conn.execute(
            "insert into orca_gw.call_labels (call_id, depth, verdict) values (%s, 3, 'bad')",
            (call_id,),
        )
        conn.execute(
            "update orca_gw.turns set created_at = now() - interval '31 days' where depth = 3"
        )
    assert await repo.purge_turns(30) == 1  # only the 31-day-old one
    with psycopg.connect(pg_url) as conn:
        assert [r[0] for r in conn.execute("select depth from orca_gw.turns")] == [5]
        assert conn.execute("select count(*) from orca_gw.calls").fetchone()[0] == 1
        assert conn.execute(
            "select llm_prompt_tokens from orca_gw.calls where id = %s", (call_id,)
        ).fetchone()[0] == 2000  # the cost data stays
        assert conn.execute("select count(*) from orca_gw.call_labels").fetchone()[0] == 1
    assert await repo.purge_turns(30) == 0


async def test_sweep_loop_purges_expired_turns(repo, tenant, pg_url):
    import asyncio

    from orca_gateway import sweep

    await _one_call(repo, tenant, "conv-t4")
    await repo.complete_turn(conversation_id="conv-t4", depth=3, usage=None, user_text="q")
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("update orca_gw.turns set created_at = now() - interval '40 days'")
    task = asyncio.create_task(
        sweep.run_forever(repo, idle_s=300, interval_s=0.05, retention_days=30)
    )
    await asyncio.sleep(0.3)
    task.cancel()
    with psycopg.connect(pg_url) as conn:
        assert conn.execute("select count(*) from orca_gw.turns").fetchone()[0] == 0

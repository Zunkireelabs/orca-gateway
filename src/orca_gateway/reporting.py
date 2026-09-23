"""S5 brief §3.5: plain query functions over `orca_gw.calls` / `orca_gw.tenant_daily_spend`, not
an API route yet -- S6 wires a route in front of these. Kept here so the query shape is proven
before a UI is built on it. Same short-lived-connection discipline as the other repositories.
"""

from __future__ import annotations

from datetime import UTC, datetime

from psycopg.rows import dict_row

from orca_gateway.db_schema import connection_class, validate_schema


class Reporting:
    def __init__(
        self, database_url: str, *, schema: str = "orca_gw", connect_timeout: int = 5
    ) -> None:
        self._url = database_url
        self._schema = validate_schema(schema)
        self._connect_timeout = connect_timeout

    def _connect(self):
        return connection_class(self._schema).connect(
            self._url,
            autocommit=True,
            prepare_threshold=None,
            connect_timeout=self._connect_timeout,
            row_factory=dict_row,
        )

    async def per_tenant_cost_this_month(self) -> list[dict]:
        """One row per tenant with any spend this month (UTC calendar month), summed from the
        running `tenant_daily_spend` total -- the real cost signal, whatever meters are live
        today. A tenant with zero calls this month is simply absent, not a zero row.

        `llm_cost_usd` sums only calls that had a real usage event; `unpriced_call_count` says how
        many more calls closed with no cost data, so a caller never mistakes "priced at $0" for
        "we don't actually know." A tenant where every call is unpriced still shows llm_cost_usd
        == 0.0 (a true sum over zero priced calls) with unpriced_call_count == call_count."""
        async with await self._connect() as conn:
            cur = await conn.execute(
                "select t.slug, "
                "       sum(s.llm_cost_usd) as llm_cost_usd, "
                "       sum(s.call_count) as call_count, "
                "       sum(s.unpriced_call_count) as unpriced_call_count "
                "from orca_gw.tenant_daily_spend s "
                "join orca_gw.tenants t on t.id = s.tenant_id "
                "where s.spend_date >= date_trunc('month', now() at time zone 'utc')::date "
                "group by t.slug "
                "order by llm_cost_usd desc nulls last"
            )
            rows = await cur.fetchall()
            for r in rows:
                r["llm_cost_usd"] = float(r["llm_cost_usd"])
            return rows

    async def calls_today(self, tenant_slug: str | None = None) -> int:
        """Count of calls STARTED today (UTC), optionally scoped to one tenant."""
        async with await self._connect() as conn:
            if tenant_slug is None:
                cur = await conn.execute(
                    "select count(*) as n from orca_gw.calls "
                    "where started_at >= date_trunc('day', now() at time zone 'utc')"
                )
            else:
                cur = await conn.execute(
                    "select count(*) as n from orca_gw.calls c "
                    "join orca_gw.tenants t on t.id = c.tenant_id "
                    "where t.slug = %s "
                    "and c.started_at >= date_trunc('day', now() at time zone 'utc')",
                    (tenant_slug,),
                )
            return (await cur.fetchone())["n"]

    # PR #10 (S5 metering fix) deployed at this UTC instant. tenant_daily_spend / calls.llm_cost_usd
    # rows whose call STARTED before it may be overstated by the bug it fixed (double-counted
    # coalescer restarts) -- never silently folded in as if they were accurate.
    METERING_FIX_CUTOFF = datetime(2026, 9, 21, 6, 13, 53, tzinfo=UTC)

    async def fleet_rows(self) -> list[dict]:
        """Panel 1: one row per (tenant, channel) -- agent_id, enabled, kill switch, TODAY's calls
        and known spend (from `calls`, which carries `channel`; `tenant_daily_spend` does not, so
        it cannot answer this per-channel), last call time, and whether this channel has had a
        call in the last hour that ended in trouble (abandoned runs or ended_reason='error')."""
        async with await self._connect() as conn:
            cur = await conn.execute(
                "select t.slug as tenant_slug, t.display_name, tc.channel, tc.agent_id, "
                "       tc.is_enabled, tc.kill_switch, tc.elevenlabs_agent_id, "
                "       c.calls_today, c.known_spend_today, c.last_call_at, "
                "       coalesce(alert.n, 0) > 0 as alert "
                "from orca_gw.tenants t "
                "join orca_gw.tenant_channels tc on tc.tenant_id = t.id "
                "left join lateral ("
                "  select count(*) as calls_today, "
                "         sum(llm_cost_usd) as known_spend_today, "
                "         max(started_at) as last_call_at "
                "  from orca_gw.calls "
                "  where tenant_id = t.id and channel = tc.channel "
                "  and started_at >= date_trunc('day', now() at time zone 'utc')"
                ") c on true "
                "left join lateral ("
                "  select count(*) as n from orca_gw.calls "
                "  where tenant_id = t.id and channel = tc.channel "
                "  and started_at >= now() - interval '1 hour' "
                "  and (abandoned_run_count > 0 or ended_reason = 'error')"
                ") alert on true "
                "order by t.slug, tc.channel"
            )
            rows = await cur.fetchall()
            for r in rows:
                r["known_spend_today"] = float(r["known_spend_today"] or 0)
            return rows

    async def list_calls(
        self, *, tenant_slug: str | None = None, on_date: str | None = None, limit: int = 200
    ) -> list[dict]:
        """Panel 2 list: newest first, optionally filtered by tenant slug and a UTC calendar date
        (YYYY-MM-DD). `label` is the call-level verdict (depth is null), if any."""
        where = ["1 = 1"]
        params: list = []
        if tenant_slug:
            where.append("t.slug = %s")
            params.append(tenant_slug)
        if on_date:
            where.append("c.started_at::date = %s")
            params.append(on_date)
        params.append(limit)
        async with await self._connect() as conn:
            cur = await conn.execute(
                "select c.id, c.conversation_id, t.slug as tenant_slug, c.channel, "
                "       c.started_at, c.ended_at, c.turn_count, c.ended_reason, c.llm_cost_usd, "
                "       c.abandoned_run_count, "
                "       (select verdict from orca_gw.call_labels cl "
                "        where cl.call_id = c.id and cl.depth is null "
                "        order by cl.created_at desc limit 1) as label "
                "from orca_gw.calls c join orca_gw.tenants t on t.id = c.tenant_id "
                f"where {' and '.join(where)} "
                "order by c.started_at desc limit %s",
                params,
            )
            rows = await cur.fetchall()
            for r in rows:
                r["llm_cost_usd"] = None if r["llm_cost_usd"] is None else float(r["llm_cost_usd"])
                r["overstated"] = r["started_at"] < self.METERING_FIX_CUTOFF
            return rows

    async def call_detail(self, call_id: str) -> dict | None:
        """Panel 2 detail: the call row, every turn in depth order, and every label on it (call-
        level and per-turn). Turns outside the retention window are simply absent -- purged, not
        hidden -- which the template says plainly rather than showing an empty table as if nothing
        happened."""
        async with await self._connect() as conn:
            cur = await conn.execute(
                "select c.*, t.slug as tenant_slug, t.display_name "
                "from orca_gw.calls c join orca_gw.tenants t on t.id = c.tenant_id "
                "where c.id = %s",
                (call_id,),
            )
            call = await cur.fetchone()
            if call is None:
                return None
            call["llm_cost_usd"] = (
                None if call["llm_cost_usd"] is None else float(call["llm_cost_usd"])
            )
            call["overstated"] = call["started_at"] < self.METERING_FIX_CUTOFF
            cur = await conn.execute(
                "select depth, user_text, answer_text, tools, usage, latency_ms, coalescer, "
                "       ended_by, created_at from orca_gw.turns "
                "where call_id = %s order by depth",
                (call_id,),
            )
            call["turns"] = await cur.fetchall()
            cur = await conn.execute(
                "select depth, verdict, note, created_at from orca_gw.call_labels "
                "where call_id = %s order by created_at desc",
                (call_id,),
            )
            call["labels"] = await cur.fetchall()
            return call

    async def cost_by_tenant_month(self) -> list[dict]:
        """Panel 3: per tenant, this UTC calendar month -- calls, turns, tokens, known LLM cost,
        calls with unknown cost (unpriced + abandoned runs, kept separate, never folded into the
        $0 sum), cost per call and per minute. `has_overstated` marks a tenant-month that includes
        any call started before the PR #10 fix, so the total is flagged rather than presented as
        clean."""
        async with await self._connect() as conn:
            cur = await conn.execute(
                "select t.slug as tenant_slug, t.display_name, "
                "       count(*) as calls, "
                "       coalesce(sum(c.turn_count), 0) as turns, "
                "       coalesce(sum(coalesce(c.llm_prompt_tokens, 0) "
                "                    + coalesce(c.llm_completion_tokens, 0)), 0) as tokens, "
                "       coalesce(sum(c.llm_cost_usd), 0) as known_llm_cost_usd, "
                "       count(*) filter (where c.llm_cost_usd is null) "
                "         + coalesce(sum(c.abandoned_run_count), 0) as unknown_cost_calls, "
                "       coalesce(sum(extract(epoch from (coalesce(c.ended_at, now()) "
                "                                         - c.started_at))), 0) / 60.0 as minutes, "
                "       bool_or(c.started_at < %s) as has_overstated "
                "from orca_gw.calls c join orca_gw.tenants t on t.id = c.tenant_id "
                "where c.started_at >= date_trunc('month', now() at time zone 'utc')::date "
                "group by t.slug, t.display_name "
                "order by known_llm_cost_usd desc",
                (self.METERING_FIX_CUTOFF,),
            )
            rows = await cur.fetchall()
            for r in rows:
                r["known_llm_cost_usd"] = float(r["known_llm_cost_usd"])
                minutes = float(r["minutes"])
                r["cost_per_call"] = (
                    r["known_llm_cost_usd"] / r["calls"] if r["calls"] else None
                )
                r["cost_per_minute"] = r["known_llm_cost_usd"] / minutes if minutes > 0 else None
            return rows

    async def cost_csv_rows(self, tenant_slug: str | None = None) -> list[dict]:
        """Panel 3 export: one row per call this month, for the unit-economics sheet. Carries
        `overstated` per row (not just per tenant-month) so the sheet can exclude or re-price
        exactly the affected calls."""
        where = ["c.started_at >= date_trunc('month', now() at time zone 'utc')::date"]
        params: list = []
        if tenant_slug:
            where.append("t.slug = %s")
            params.append(tenant_slug)
        async with await self._connect() as conn:
            cur = await conn.execute(
                "select t.slug as tenant_slug, c.conversation_id, c.channel, c.started_at, "
                "       c.ended_at, c.ended_reason, c.turn_count, c.llm_model, "
                "       c.llm_prompt_tokens, c.llm_completion_tokens, c.llm_cost_usd, "
                "       c.abandoned_run_count "
                "from orca_gw.calls c join orca_gw.tenants t on t.id = c.tenant_id "
                f"where {' and '.join(where)} "
                "order by c.started_at",
                params,
            )
            rows = await cur.fetchall()
            for r in rows:
                r["llm_cost_usd"] = None if r["llm_cost_usd"] is None else float(r["llm_cost_usd"])
                r["overstated"] = r["started_at"] < self.METERING_FIX_CUTOFF
            return rows

    async def open_calls(self) -> list[dict]:
        """Every call the idle-timeout sweep has not yet closed -- what "in progress right now"
        means without a telephony hangup signal (see 0002_calls.sql)."""
        async with await self._connect() as conn:
            cur = await conn.execute(
                "select c.conversation_id, t.slug as tenant_slug, c.channel, "
                "       c.started_at, c.last_turn_at, c.turn_count "
                "from orca_gw.calls c "
                "join orca_gw.tenants t on t.id = c.tenant_id "
                "where c.ended_at is null "
                "order by c.started_at"
            )
            return list(await cur.fetchall())

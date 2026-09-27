"""S5 brief §3.5: plain query functions over `orca_gw.calls` / `orca_gw.tenant_daily_spend`, not
an API route yet -- S6 wires a route in front of these. Kept here so the query shape is proven
before a UI is built on it. Same short-lived-connection discipline as the other repositories.
"""

from __future__ import annotations

from datetime import UTC, datetime

from psycopg.rows import dict_row

from orca_gateway.cost import TELEPHONY_RATE_USD_PER_MINUTE, all_in_cost_usd
from orca_gateway.db_schema import connection_class, validate_schema


def _elevenlabs_component(channel: str, elevenlabs_cost_fiat: float | None) -> float:
    """Chat never touches ElevenLabs (P6 brief §7): its non-LLM cost is always and exactly zero,
    never 'unreconciled'. Only voice's `elevenlabs_cost_fiat` can be genuinely unknown (not yet
    pulled by `reconcile.py`)."""
    return 0.0 if channel == "chat" else elevenlabs_cost_fiat


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

    async def agent_rows(self) -> list[dict]:
        """Fleet 'Agents' section (P5 brief §6): every `orca_gw.agents` row -- the shared,
        platform-level object §2.3 is about -- with the tenant×channels that reference it via
        `agent_ref`. An agent nothing currently references is still listed, with `used_by = []`
        (never hidden: the kill switch still exists and might be flipped ahead of a rollout)."""
        async with await self._connect() as conn:
            cur = await conn.execute(
                "select id, name, display_name, class as agent_class, owning_product, "
                "version, kill_switch from orca_gw.agents order by name"
            )
            agents = await cur.fetchall()
            cur = await conn.execute(
                "select tc.agent_ref, t.slug as tenant_slug, t.display_name, tc.channel "
                "from orca_gw.tenant_channels tc "
                "join orca_gw.tenants t on t.id = tc.tenant_id "
                "where tc.agent_ref is not null "
                "order by t.slug, tc.channel"
            )
            used_by: dict = {}
            for row in await cur.fetchall():
                used_by.setdefault(row["agent_ref"], []).append(
                    {
                        "tenant_slug": row["tenant_slug"],
                        "display_name": row["display_name"],
                        "channel": row["channel"],
                    }
                )
            for a in agents:
                a["used_by"] = used_by.get(a["id"], [])
            return agents

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
        """Panel 3, P6: per **tenant × channel** (N2 -- voice and chat cost are different things,
        `calls` already carries `channel`), this UTC calendar month -- calls, turns, tokens, known
        LLM cost, calls with unknown LLM cost (unpriced + abandoned runs, kept separate, never
        folded into the $0 sum), the STT/TTS volume meters, known ElevenLabs platform cost
        (`elevenlabs_cost_fiat`, populated by `reconcile.py`), the all-in known cost (LLM +
        ElevenLabs + telephony) and cost per call/minute computed from IT rather than LLM alone,
        and `unreconciled_voice_calls` -- voice calls not yet pulled from ElevenLabs, kept
        separate from the all-in total exactly like `unknown_cost_calls` is for LLM. Chat rows
        never carry ElevenLabs data (brief §7: chat's cost is LLM-only and already complete), so
        their `unreconciled_voice_calls` is always 0 and their all-in cost already IS the whole
        story. `has_overstated` marks a row that includes any call started before the PR #10 fix,
        so the total is flagged rather than presented as clean. `margin_usd` is always None here
        -- see the P6 brief §6 Q4: it needs the tenant's price basis, not yet decided; adding it
        is a template/reporting change only once that lands, never a fabricated number now."""
        async with await self._connect() as conn:
            cur = await conn.execute(
                "select t.slug as tenant_slug, t.display_name, c.channel, "
                "       count(*) as calls, "
                "       coalesce(sum(c.turn_count), 0) as turns, "
                "       coalesce(sum(coalesce(c.llm_prompt_tokens, 0) "
                "                    + coalesce(c.llm_completion_tokens, 0)), 0) as tokens, "
                "       coalesce(sum(c.llm_cost_usd), 0) as known_llm_cost_usd, "
                "       count(*) filter (where c.llm_cost_usd is null) "
                "         + coalesce(sum(c.abandoned_run_count), 0) as unknown_cost_calls, "
                "       coalesce(sum(c.stt_minutes), 0) as stt_minutes, "
                "       coalesce(sum(c.tts_characters), 0) as tts_characters, "
                "       coalesce(sum(c.elevenlabs_cost_fiat), 0) as known_elevenlabs_cost_usd, "
                "       count(*) filter (where c.channel = 'voice' "
                "                        and c.elevenlabs_cost_fiat is null) "
                "         as unreconciled_voice_calls, "
                "       coalesce(sum(coalesce(c.telephony_minutes, 0)), 0) as telephony_minutes, "
                "       coalesce(sum(extract(epoch from (coalesce(c.ended_at, now()) "
                "                                         - c.started_at))), 0) / 60.0 as minutes, "
                "       bool_or(c.started_at < %s) as has_overstated "
                "from orca_gw.calls c join orca_gw.tenants t on t.id = c.tenant_id "
                "where c.started_at >= date_trunc('month', now() at time zone 'utc')::date "
                "group by t.slug, t.display_name, c.channel "
                "order by t.slug, c.channel",
                (self.METERING_FIX_CUTOFF,),
            )
            rows = await cur.fetchall()
            for r in rows:
                r["known_llm_cost_usd"] = float(r["known_llm_cost_usd"])
                r["stt_minutes"] = float(r["stt_minutes"])
                r["known_elevenlabs_cost_usd"] = float(r["known_elevenlabs_cost_usd"])
                r["telephony_minutes"] = float(r["telephony_minutes"])
                minutes = float(r["minutes"])
                r["minutes"] = minutes
                r["all_in_known_cost_usd"] = round(
                    r["known_llm_cost_usd"]
                    + r["known_elevenlabs_cost_usd"]
                    + r["telephony_minutes"] * TELEPHONY_RATE_USD_PER_MINUTE,
                    6,
                )
                r["cost_per_call"] = (
                    r["all_in_known_cost_usd"] / r["calls"] if r["calls"] else None
                )
                r["cost_per_minute"] = (
                    r["all_in_known_cost_usd"] / minutes if minutes > 0 else None
                )
                r["margin_usd"] = None  # TODO(P6 §6 Q4): needs the tenant's price basis
            return rows

    async def cost_csv_rows(self, tenant_slug: str | None = None) -> list[dict]:
        """Panel 3 export: one row per call this month, for the unit-economics sheet. Carries
        `overstated` per row (not just per tenant-month) so the sheet can exclude or re-price
        exactly the affected calls. `all_in_cost_usd` is per-call (see cost.all_in_cost_usd):
        None for a voice call not yet reconciled with ElevenLabs, never a partial total silently
        presented as complete; a chat call's is always its `llm_cost_usd` (it never touches
        ElevenLabs -- brief §7)."""
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
                "       c.abandoned_run_count, c.stt_minutes, c.tts_characters, "
                "       c.elevenlabs_cost_fiat, c.telephony_minutes "
                "from orca_gw.calls c join orca_gw.tenants t on t.id = c.tenant_id "
                f"where {' and '.join(where)} "
                "order by c.started_at",
                params,
            )
            rows = await cur.fetchall()
            for r in rows:
                r["llm_cost_usd"] = None if r["llm_cost_usd"] is None else float(r["llm_cost_usd"])
                r["stt_minutes"] = None if r["stt_minutes"] is None else float(r["stt_minutes"])
                r["elevenlabs_cost_fiat"] = (
                    None if r["elevenlabs_cost_fiat"] is None else float(r["elevenlabs_cost_fiat"])
                )
                r["all_in_cost_usd"] = all_in_cost_usd(
                    r["llm_cost_usd"],
                    _elevenlabs_component(r["channel"], r["elevenlabs_cost_fiat"]),
                    r["telephony_minutes"],
                )
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

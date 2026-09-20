"""S5 brief §3.5: plain query functions over `orca_gw.calls` / `orca_gw.tenant_daily_spend`, not
an API route yet -- S6 wires a route in front of these. Kept here so the query shape is proven
before a UI is built on it. Same short-lived-connection discipline as the other repositories.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row


class Reporting:
    def __init__(self, database_url: str, *, connect_timeout: int = 5) -> None:
        self._url = database_url
        self._connect_timeout = connect_timeout

    def _connect(self):
        return psycopg.AsyncConnection.connect(
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

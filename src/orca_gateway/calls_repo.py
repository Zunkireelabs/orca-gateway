"""Postgres persistence for per-call metering (schema `orca_gw`, tables `calls` and
`tenant_daily_spend`). Same discipline as tenant_repo.py: one short-lived connection per call.

`conversation_id` is the idempotency key everywhere here: a call has many turns and must produce
exactly one row in `orca_gw.calls`, never one per turn. `record_turn` is the only write on the hot
path (every turn); everything else (usage, closing, daily-spend) is a follow-on write gated by that
first insert already having happened.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import psycopg
from psycopg.rows import dict_row

from orca_gateway.cost import cost_usd

logger = logging.getLogger("orca_gateway.calls_repo")

EndedReason = str  # "completed" | "timed_out" | "kill_switch" | "error"


@dataclass
class CallState:
    id: str
    started_at: object  # datetime; typed loosely to avoid importing tz machinery here
    turn_count: int
    ended_at: object | None


class PgCallsRepository:
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

    async def get_open_call(self, conversation_id: str) -> CallState | None:
        """Read-only: used to decide whether a turn is a call's FIRST (no row yet -- the
        daily_spend_cap gate applies) and, for an existing call, how long it has been running
        (max_session_seconds)."""
        async with await self._connect() as conn:
            cur = await conn.execute(
                "select id, started_at, turn_count, ended_at from orca_gw.calls "
                "where conversation_id = %s",
                (conversation_id,),
            )
            row = await cur.fetchone()
            return None if row is None else CallState(**row)

    async def record_turn(
        self,
        *,
        tenant_slug: str,
        channel: str,
        conversation_id: str,
        agent_id: str,
        elevenlabs_agent_id: str | None,
    ) -> CallState:
        """Idempotent on conversation_id: the first turn inserts the row, every later turn
        increments turn_count and bumps last_turn_at (which is what the idle-timeout sweep reads).
        A turn arriving for a conversation_id the sweep already closed does NOT reopen it -- it is
        logged and returned as-is, a known, documented limitation for a very late straggler turn
        (see docs/metering-reconciliation.md). Keyed by tenant SLUG, not id -- channel adapters
        only ever have the slug, same convention as tenant_repo.set_kill_switch."""
        async with await self._connect() as conn:
            cur = await conn.execute(
                "insert into orca_gw.calls "
                "(tenant_id, channel, conversation_id, agent_id, elevenlabs_agent_id, "
                " started_at, last_turn_at, turn_count) "
                "values ((select id from orca_gw.tenants where slug = %s), %s, %s, %s, %s, "
                "        now(), now(), 1) "
                "on conflict (conversation_id) do update set "
                "  turn_count = orca_gw.calls.turn_count + 1, "
                "  last_turn_at = now(), "
                "  elevenlabs_agent_id = coalesce(orca_gw.calls.elevenlabs_agent_id, "
                "                                 excluded.elevenlabs_agent_id) "
                "where orca_gw.calls.ended_at is null "
                "returning id, started_at, turn_count, ended_at",
                (tenant_slug, channel, conversation_id, agent_id, elevenlabs_agent_id),
            )
            row = await cur.fetchone()
            if row is None:
                # conversation_id exists but was already closed (sweep beat this straggler turn).
                logger.warning(
                    "turn arrived for already-closed conversation_id=%s; not reopening",
                    conversation_id,
                )
                cur = await conn.execute(
                    "select id, started_at, turn_count, ended_at from orca_gw.calls "
                    "where conversation_id = %s",
                    (conversation_id,),
                )
                row = await cur.fetchone()
            return CallState(**row)

    async def record_usage(
        self,
        *,
        conversation_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """Accumulates token counts across all turns of the call (a conversation's real dollar
        cost is the sum over its turns, not just the last one) and recomputes llm_cost_usd from
        the running total. `model` is assumed constant across a conversation; the last value
        written wins if that assumption is ever wrong."""
        async with await self._connect() as conn:
            cur = await conn.execute(
                "update orca_gw.calls set "
                "  llm_prompt_tokens = coalesce(llm_prompt_tokens, 0) + %s, "
                "  llm_completion_tokens = coalesce(llm_completion_tokens, 0) + %s, "
                "  llm_model = %s, "
                "  updated_at = now() "
                "where conversation_id = %s "
                "returning llm_prompt_tokens, llm_completion_tokens, llm_model",
                (prompt_tokens, completion_tokens, model, conversation_id),
            )
            row = await cur.fetchone()
            if row is None:
                logger.warning(
                    "usage event for unknown conversation_id=%s dropped", conversation_id
                )
                return
            cost = cost_usd(
                row["llm_model"], row["llm_prompt_tokens"], row["llm_completion_tokens"]
            )
            await conn.execute(
                "update orca_gw.calls set llm_cost_usd = %s, updated_at = now() "
                "where conversation_id = %s",
                (cost, conversation_id),
            )

    async def close_call(self, conversation_id: str, ended_reason: EndedReason) -> None:
        """Ends the call (idempotent: a no-op if already ended) and rolls its cost into
        tenant_daily_spend, keyed by the call's started_at date in UTC (see 0002_calls.sql for
        why UTC rather than the tenant's local timezone). A call that closed with a null
        llm_cost_usd counts toward unpriced_call_count, NEVER toward llm_cost_usd as a fabricated
        zero -- see 0002_calls.sql on why collapsing "unknown" into "$0" would be a lie."""
        async with await self._connect() as conn, conn.transaction():
            cur = await conn.execute(
                "update orca_gw.calls set ended_at = now(), ended_reason = %s, updated_at = now() "
                "where conversation_id = %s and ended_at is null "
                "returning tenant_id, started_at, llm_cost_usd",
                (ended_reason, conversation_id),
            )
            row = await cur.fetchone()
            if row is None:
                return  # already closed, or never existed -- both fine, this is idempotent
            spend_date: date = row["started_at"].date()
            priced = row["llm_cost_usd"] is not None
            await conn.execute(
                "insert into orca_gw.tenant_daily_spend "
                "(tenant_id, spend_date, llm_cost_usd, call_count, unpriced_call_count) "
                "values (%s, %s, %s, 1, %s) "
                "on conflict (tenant_id, spend_date) do update set "
                "  llm_cost_usd = orca_gw.tenant_daily_spend.llm_cost_usd + excluded.llm_cost_usd, "
                "  call_count = orca_gw.tenant_daily_spend.call_count + 1, "
                "  unpriced_call_count = orca_gw.tenant_daily_spend.unpriced_call_count "
                "                        + excluded.unpriced_call_count, "
                "  updated_at = now()",
                (
                    row["tenant_id"],
                    spend_date,
                    row["llm_cost_usd"] if priced else 0,
                    0 if priced else 1,
                ),
            )

    async def daily_spend_usd(self, tenant_slug: str) -> float:
        """Today's (UTC) running LLM spend for a tenant -- the only real cost signal today. Used
        by the daily_spend_cap gate before a new call's first turn."""
        async with await self._connect() as conn:
            cur = await conn.execute(
                "select s.llm_cost_usd from orca_gw.tenant_daily_spend s "
                "join orca_gw.tenants t on t.id = s.tenant_id "
                "where t.slug = %s and s.spend_date = (now() at time zone 'utc')::date",
                (tenant_slug,),
            )
            row = await cur.fetchone()
            return float(row["llm_cost_usd"]) if row else 0.0

    async def sweep_idle(self, idle_s: float = 300.0) -> list[str]:
        """Closes every open call whose last_turn_at is older than idle_s (default 5 minutes --
        see 0002_calls.sql: a real phone call is not silent that long) with ended_reason
        'timed_out', rolling each into tenant_daily_spend. Returns the conversation_ids closed.
        Meant to be called periodically (see sweep.py), never from the request path."""
        async with await self._connect() as conn:
            cur = await conn.execute(
                "select conversation_id from orca_gw.calls "
                "where ended_at is null and last_turn_at < now() - (%s || ' seconds')::interval",
                (idle_s,),
            )
            ids = [r["conversation_id"] for r in await cur.fetchall()]
        for cid in ids:
            await self.close_call(cid, "timed_out")
        return ids

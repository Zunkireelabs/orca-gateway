"""Postgres persistence for per-call metering (schema `orca_gw`, tables `calls` and
`tenant_daily_spend`). Same discipline as tenant_repo.py: one short-lived connection per call.

`conversation_id` is the idempotency key everywhere here: a call has many turns and must produce
exactly one row in `orca_gw.calls`, never one per turn. On the hot path `touch_call` makes the row
exist when a run starts and `complete_turn` counts the turn and its usage when a run COMPLETES:
never at run start and never per raw HTTP request, or every coalescer restart and duplicate request
would be counted again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from orca_gateway.cost import cost_usd

logger = logging.getLogger("orca_gateway.calls_repo")

EndedReason = str  # "completed" | "timed_out" | "kill_switch" | "error"


@dataclass
class CallState:
    id: str
    started_at: object  # datetime; typed loosely to avoid importing tz machinery here
    turn_count: int
    ended_at: object | None
    ended_reason: str | None = None


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
                "select id, started_at, turn_count, ended_at, ended_reason from orca_gw.calls "
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
                "returning id, started_at, turn_count, ended_at, ended_reason",
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
                    "select id, started_at, turn_count, ended_at, ended_reason from orca_gw.calls "
                    "where conversation_id = %s",
                    (conversation_id,),
                )
                row = await cur.fetchone()
            return CallState(**row)

    async def touch_call(
        self,
        *,
        tenant_slug: str,
        channel: str,
        conversation_id: str,
        agent_id: str,
        elevenlabs_agent_id: str | None,
    ) -> CallState:
        """Ensures the row exists and bumps last_turn_at (so the idle sweep cannot close a call
        whose turn is still running), WITHOUT counting a turn. A turn is counted once, when a run
        completes (`complete_turn`): counting at run START would count every coalescer restart of
        the same spoken turn again."""
        async with await self._connect() as conn:
            cur = await conn.execute(
                "insert into orca_gw.calls "
                "(tenant_id, channel, conversation_id, agent_id, elevenlabs_agent_id, "
                " started_at, last_turn_at, turn_count) "
                "values ((select id from orca_gw.tenants where slug = %s), %s, %s, %s, %s, "
                "        now(), now(), 0) "
                "on conflict (conversation_id) do update set "
                "  last_turn_at = case when orca_gw.calls.ended_at is null then now() "
                "                      else orca_gw.calls.last_turn_at end, "
                "  elevenlabs_agent_id = coalesce(orca_gw.calls.elevenlabs_agent_id, "
                "                                 excluded.elevenlabs_agent_id) "
                "returning id, started_at, turn_count, ended_at, ended_reason",
                (tenant_slug, channel, conversation_id, agent_id, elevenlabs_agent_id),
            )
            return CallState(**await cur.fetchone())

    async def complete_turn(
        self,
        *,
        conversation_id: str,
        depth: int,
        usage: dict | None,
        user_text: str | None = None,
        answer_text: str | None = None,
        tools: list[dict] | None = None,
        latency_ms: int | None = None,
        coalescer: dict | None = None,
        ended_by: str | None = None,
    ) -> None:
        """Called once per COMPLETED run, and the ONLY place a turn is counted, its usage added
        and its transcript row written, all in one transaction. The transcript row is unique on
        (call, depth): if it already exists this whole call is a no-op, so nothing here can
        double-count a depth however it is reached (the S5 bug). Usage is recorded only when the
        backend reported a model and both token counts; a partial or absent payload leaves the
        columns as they were (null = unknown), never a defaulted zero. Turn count and
        last_turn_at only move on a still-open call. The text columns are PII: never log them."""
        async with await self._connect() as conn, conn.transaction():
            cur = await conn.execute(
                "select id from orca_gw.calls where conversation_id = %s", (conversation_id,)
            )
            call = await cur.fetchone()
            if call is None:
                logger.warning("completed turn for unknown conversation_id=%s", conversation_id)
                return
            cur = await conn.execute(
                "insert into orca_gw.turns (call_id, depth, user_text, answer_text, tools, "
                " usage, latency_ms, coalescer, ended_by) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (call_id, depth) do nothing returning id",
                (
                    call["id"],
                    depth,
                    user_text,
                    answer_text,
                    Jsonb(tools or []),
                    None if usage is None else Jsonb(usage),
                    latency_ms,
                    None if coalescer is None else Jsonb(coalescer),
                    ended_by,
                ),
            )
            if await cur.fetchone() is None:
                logger.info(
                    "turn already recorded conversation_id=%s depth=%s; not counting again",
                    conversation_id,
                    depth,
                )
                return
            await conn.execute(
                "update orca_gw.calls set "
                "  turn_count = turn_count + (case when ended_at is null then 1 else 0 end), "
                "  last_turn_at = case when ended_at is null then now() else last_turn_at end, "
                "  updated_at = now() "
                "where id = %s",
                (call["id"],),
            )
            model = (usage or {}).get("model")
            prompt = (usage or {}).get("prompt_tokens")
            completion = (usage or {}).get("completion_tokens")
            if not (
                isinstance(model, str)
                and model
                and isinstance(prompt, int)
                and isinstance(completion, int)
            ):
                if usage:
                    logger.warning(
                        "incomplete usage payload for conversation_id=%s", conversation_id
                    )
                return
            cur = await conn.execute(
                "update orca_gw.calls set "
                "  llm_prompt_tokens = coalesce(llm_prompt_tokens, 0) + %s, "
                "  llm_completion_tokens = coalesce(llm_completion_tokens, 0) + %s, "
                "  llm_model = %s, updated_at = now() "
                "where id = %s "
                "returning llm_prompt_tokens, llm_completion_tokens, llm_model",
                (prompt, completion, model, call["id"]),
            )
            row = await cur.fetchone()
            cost = cost_usd(
                row["llm_model"], row["llm_prompt_tokens"], row["llm_completion_tokens"]
            )
            await conn.execute(
                "update orca_gw.calls set llm_cost_usd = %s where id = %s",
                (cost, call["id"]),
            )

    async def purge_turns(self, retention_days: int) -> int:
        """Deletes transcript rows older than the retention window (run by the sweep). Calls,
        costs and labels are untouched. Returns how many rows were purged."""
        async with await self._connect() as conn:
            cur = await conn.execute(
                "delete from orca_gw.turns where created_at < now() - make_interval(days => %s)",
                (retention_days,),
            )
            return cur.rowcount

    async def record_abandoned_run(self, conversation_id: str) -> None:
        """A run that reached the backend but never completed (cancelled by a coalescer restart,
        timed out, or failed). Its provider-side cost, if any, is invisible to us: usage arrives
        only at the end of a completed stream. We cannot record a number we never received (and
        must not estimate one), so this counts the runs whose cost is missing instead."""
        async with await self._connect() as conn:
            await conn.execute(
                "update orca_gw.calls set abandoned_run_count = abandoned_run_count + 1, "
                "updated_at = now() where conversation_id = %s",
                (conversation_id,),
            )

    async def refuse_call(
        self,
        *,
        tenant_slug: str,
        channel: str,
        conversation_id: str,
        agent_id: str,
        elevenlabs_agent_id: str | None,
    ) -> None:
        """A call refused at its first turn by daily_spend_cap: recorded as an already-closed
        row with zero turns (idempotent), so the refusal is visible. Not rolled into
        tenant_daily_spend -- nothing was served."""
        async with await self._connect() as conn:
            await conn.execute(
                "insert into orca_gw.calls "
                "(tenant_id, channel, conversation_id, agent_id, elevenlabs_agent_id, "
                " started_at, last_turn_at, ended_at, ended_reason, turn_count) "
                "values ((select id from orca_gw.tenants where slug = %s), %s, %s, %s, %s, "
                "        now(), now(), now(), 'daily_spend_cap', 0) "
                "on conflict (conversation_id) do nothing",
                (tenant_slug, channel, conversation_id, agent_id, elevenlabs_agent_id),
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

    async def add_label(
        self, *, call_id: str, depth: int | None, verdict: str, note: str | None
    ) -> None:
        """Panel 2 write: the eval-set seed (vision §5). `depth = None` labels the whole call;
        an integer labels one turn. Never a foreign key to `turns` -- a label must outlive the
        30-day purge of the turn text it was about. Audited like every other console write (before
        is always null: a label is an addition, not an edit of a prior one)."""
        async with await self._connect() as conn, conn.transaction():
            cur = await conn.execute(
                "select tenant_id, channel from orca_gw.calls where id = %s", (call_id,)
            )
            call = await cur.fetchone()
            if call is None:
                raise ValueError(f"no such call: {call_id}")
            await conn.execute(
                "insert into orca_gw.call_labels (call_id, depth, verdict, note) "
                "values (%s, %s, %s, %s)",
                (call_id, depth, verdict, note),
            )
            await conn.execute(
                "insert into orca_gw.config_audit "
                "(tenant_id, channel, action, before, after, actor) "
                "values (%s, %s, 'call_label', null, %s, 'console')",
                (
                    call["tenant_id"],
                    call["channel"],
                    Jsonb({"call_id": call_id, "depth": depth, "verdict": verdict, "note": note}),
                ),
            )

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

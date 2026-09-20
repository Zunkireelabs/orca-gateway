"""Postgres persistence for tenant config (schema `orca_gw`). One short-lived connection per
call: the store's cache makes calls rare, and a long-lived pool would compete for the shared
project's connection ceiling. `prepare_threshold=None` keeps it safe behind a transaction pooler."""

from __future__ import annotations

import json

import psycopg
from psycopg.rows import dict_row

from orca_gateway.tenants import ChannelConfig, TenantConfig

_CHANNEL_COLS = (
    "channel, is_enabled, agent_id, languages, default_language, voice_id, spoken_brand_name, "
    "handoff_target, handoff_hours, out_of_hours_behaviour, out_of_hours_message, "
    "escalation_policy, escalation_instruction, closed_dates, closed_weekdays, "
    "max_session_seconds, daily_spend_cap, per_caller_rate_limit, kill_switch"
)


class PgTenantRepository:
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

    async def load(self, slug: str) -> TenantConfig | None:
        async with await self._connect() as conn:
            cur = await conn.execute(
                "select id, slug, display_name, is_active, timezone, backend, backend_config "
                "from orca_gw.tenants where slug = %s",
                (slug,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            cur = await conn.execute(
                f"select {_CHANNEL_COLS} from orca_gw.tenant_channels where tenant_id = %s",
                (row["id"],),
            )
            channels = {}
            for c in await cur.fetchall():
                c["daily_spend_cap"] = (
                    None if c["daily_spend_cap"] is None else float(c["daily_spend_cap"])
                )
                channels[c["channel"]] = ChannelConfig(**c)
            return TenantConfig(
                slug=row["slug"],
                display_name=row["display_name"],
                is_active=row["is_active"],
                timezone=row["timezone"],
                backend=row["backend"],
                backend_config=row["backend_config"],
                channels=channels,
            )

    async def upsert(self, cfg: TenantConfig) -> None:
        """Write a tenant and its channels (used by tests and the bootstrap CLI)."""
        async with await self._connect() as conn, conn.transaction():
            cur = await conn.execute(
                "insert into orca_gw.tenants (slug, display_name, is_active, timezone, backend, "
                "backend_config) values (%s, %s, %s, %s, %s, %s) "
                "on conflict (slug) do update set display_name = excluded.display_name, "
                "is_active = excluded.is_active, timezone = excluded.timezone, "
                "backend = excluded.backend, backend_config = excluded.backend_config, "
                "updated_at = now() returning id",
                (
                    cfg.slug,
                    cfg.display_name,
                    cfg.is_active,
                    cfg.timezone,
                    cfg.backend,
                    json.dumps(cfg.backend_config),
                ),
            )
            tenant_id = (await cur.fetchone())["id"]
            for ch in cfg.channels.values():
                await self._upsert_channel(conn, tenant_id, ch)

    async def insert_if_missing(self, cfg: TenantConfig) -> bool:
        """Bootstrap: create a tenant only if its slug is absent. Never modifies an existing row,
        so it cannot overwrite an edit made through the console."""
        async with await self._connect() as conn, conn.transaction():
            cur = await conn.execute(
                "insert into orca_gw.tenants (slug, display_name, is_active, timezone, backend, "
                "backend_config) values (%s, %s, %s, %s, %s, %s) "
                "on conflict (slug) do nothing returning id",
                (
                    cfg.slug,
                    cfg.display_name,
                    cfg.is_active,
                    cfg.timezone,
                    cfg.backend,
                    json.dumps(cfg.backend_config),
                ),
            )
            row = await cur.fetchone()
            if row is None:
                return False
            for ch in cfg.channels.values():
                await self._upsert_channel(conn, row["id"], ch)
            return True

    async def set_kill_switch(self, slug: str, channel: str, on: bool) -> None:
        async with await self._connect() as conn:
            await conn.execute(
                "update orca_gw.tenant_channels set kill_switch = %s, updated_at = now() "
                "where channel = %s and tenant_id = "
                "(select id from orca_gw.tenants where slug = %s)",
                (on, channel, slug),
            )

    @staticmethod
    async def _upsert_channel(conn, tenant_id, ch: ChannelConfig) -> None:
        await conn.execute(
            f"insert into orca_gw.tenant_channels (tenant_id, {_CHANNEL_COLS}) values "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "on conflict (tenant_id, channel) do update set "
            + ", ".join(f"{c.strip()} = excluded.{c.strip()}" for c in _CHANNEL_COLS.split(",")[1:])
            + ", updated_at = now()",
            (
                tenant_id,
                ch.channel,
                ch.is_enabled,
                ch.agent_id,
                ch.languages,
                ch.default_language,
                ch.voice_id,
                ch.spoken_brand_name,
                ch.handoff_target,
                None if ch.handoff_hours is None else json.dumps(ch.handoff_hours),
                ch.out_of_hours_behaviour,
                ch.out_of_hours_message,
                ch.escalation_policy,
                ch.escalation_instruction,
                ch.closed_dates,
                ch.closed_weekdays,
                ch.max_session_seconds,
                ch.daily_spend_cap,
                ch.per_caller_rate_limit,
                ch.kill_switch,
            ),
        )

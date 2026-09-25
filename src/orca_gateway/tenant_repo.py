"""Postgres persistence for tenant config (schema `orca_gw`). One short-lived connection per
call: the store's cache makes calls rare, and a long-lived pool would compete for the shared
project's connection ceiling. `prepare_threshold=None` keeps it safe behind a transaction pooler."""

from __future__ import annotations

import json

from psycopg.rows import dict_row

from orca_gateway.db_schema import connection_class, validate_schema
from orca_gateway.tenants import ChannelConfig, TenantConfig

_CHANNEL_COLS = (
    "channel, is_enabled, agent_id, elevenlabs_agent_id, languages, default_language, voice_id, "
    "spoken_brand_name, handoff_target, handoff_hours, out_of_hours_behaviour, "
    "out_of_hours_message, escalation_policy, escalation_instruction, closed_dates, "
    "closed_weekdays, max_session_seconds, daily_spend_cap, per_caller_rate_limit, "
    "max_concurrent_runs, kill_switch, allowed_origins, spoken_kill_switch, "
    "spoken_error_fallback, kill_switch_message, error_fallback_message, phone_guard, "
    "allowed_phone_numbers, phone_guard_message"
)
_CHANNEL_PLACEHOLDERS = ", ".join(["%s"] * (_CHANNEL_COLS.count(",") + 2))  # + tenant_id


class PgTenantRepository:
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

    async def insert_missing_channels(self, cfg: TenantConfig) -> list[str]:
        """Bootstrap, existing-tenant path: insert each channel in `cfg.channels` that has no row
        yet for this tenant. Insert-only -- `on conflict (tenant_id, channel) do nothing` -- never
        `do update`, so an existing row is left untouched even where it differs from the JSON. Not
        `_upsert_channel`: that does `do update` and would overwrite a console edit.

        A4 guard: a channel being ADDED to an existing tenant must carry `kill_switch: true` in the
        JSON. If any would-be-added channel doesn't, nothing is inserted for this tenant -- the
        guard is checked for every missing channel before any insert runs, and the whole call is
        one transaction, so a failed guard can't leave some channels in and others out.

        One `config_audit` row per inserted channel (`action='channel_created'`, `before=null`,
        `after`=the inserted values, `actor='bootstrap'`); nothing is written for a channel that
        already existed."""
        async with await self._connect() as conn, conn.transaction():
            cur = await conn.execute(
                "select id from orca_gw.tenants where slug = %s for update", (cfg.slug,)
            )
            row = await cur.fetchone()
            if row is None:
                raise ValueError(f"no such tenant: {cfg.slug}")
            tenant_id = row["id"]
            cur = await conn.execute(
                "select channel from orca_gw.tenant_channels where tenant_id = %s", (tenant_id,)
            )
            existing = {r["channel"] for r in await cur.fetchall()}
            to_add = {name: ch for name, ch in cfg.channels.items() if name not in existing}
            for name, ch in to_add.items():
                if not ch.kill_switch:
                    raise ValueError(
                        f"refusing to add {cfg.slug}/{name}: a channel added to an existing "
                        "tenant must have kill_switch: true in its JSON"
                    )
            added: list[str] = []
            for name, ch in to_add.items():
                cur = await conn.execute(
                    f"insert into orca_gw.tenant_channels (tenant_id, {_CHANNEL_COLS}) values "
                    f"({_CHANNEL_PLACEHOLDERS}) on conflict (tenant_id, channel) do nothing "
                    "returning channel",
                    self._channel_values(tenant_id, ch),
                )
                if await cur.fetchone() is None:
                    continue  # raced with another writer between the select above and here
                added.append(name)
                await conn.execute(
                    "insert into orca_gw.config_audit "
                    "(tenant_id, channel, action, before, after, actor) "
                    "values (%s, %s, 'channel_created', null, %s, 'bootstrap')",
                    (tenant_id, name, json.dumps(ch.model_dump(mode="json"))),
                )
            return added

    async def set_kill_switch(self, slug: str, channel: str, on: bool) -> None:
        async with await self._connect() as conn:
            await conn.execute(
                "update orca_gw.tenant_channels set kill_switch = %s, updated_at = now() "
                "where channel = %s and tenant_id = "
                "(select id from orca_gw.tenants where slug = %s)",
                (on, channel, slug),
            )

    async def list_tenants(self) -> list[dict]:
        """Every tenant with its channels, for panel 1 (Fleet). Console-only read: unlike
        `load()`, this is never on the request-serving path and is never cached."""
        async with await self._connect() as conn:
            cur = await conn.execute(
                "select id, slug, display_name, is_active from orca_gw.tenants order by slug"
            )
            tenants = await cur.fetchall()
            cur = await conn.execute(
                f"select tenant_id, {_CHANNEL_COLS} from orca_gw.tenant_channels "
                "order by channel"
            )
            by_tenant: dict = {}
            for row in await cur.fetchall():
                by_tenant.setdefault(row["tenant_id"], []).append(row)
            for t in tenants:
                t["channels"] = by_tenant.get(t["id"], [])
            return tenants

    async def set_channel_field(
        self, slug: str, channel: str, on: bool, *, field: str, actor: str = "console"
    ) -> tuple[bool, bool]:
        """Toggle a boolean channel field (kill_switch or is_enabled) and write a config_audit
        row (before, after). `field` is never taken from request input -- callers pass a literal,
        so this can never become an arbitrary-column write."""
        if field not in ("kill_switch", "is_enabled"):
            raise ValueError(f"not an audited toggle field: {field}")
        async with await self._connect() as conn, conn.transaction():
            cur = await conn.execute(
                f"select t.id as tenant_id, tc.{field} as before "
                "from orca_gw.tenants t join orca_gw.tenant_channels tc on tc.tenant_id = t.id "
                "where t.slug = %s and tc.channel = %s for update",
                (slug, channel),
            )
            row = await cur.fetchone()
            if row is None:
                raise ValueError(f"no such tenant/channel: {slug}/{channel}")
            await conn.execute(
                f"update orca_gw.tenant_channels set {field} = %s, updated_at = now() "
                "where tenant_id = %s and channel = %s",
                (on, row["tenant_id"], channel),
            )
            await conn.execute(
                "insert into orca_gw.config_audit "
                "(tenant_id, channel, action, before, after, actor) "
                "values (%s, %s, %s, %s, %s, %s)",
                (
                    row["tenant_id"],
                    channel,
                    field,
                    json.dumps({field: row["before"]}),
                    json.dumps({field: on}),
                    actor,
                ),
            )
            return row["before"], on

    async def update_tenant_timezone(
        self, slug: str, timezone: str, *, actor: str = "console"
    ) -> None:
        """Panel 4 also edits the tenant-level IANA timezone (hours/availability are computed in
        it). A no-op (no audit row) if unchanged."""
        async with await self._connect() as conn, conn.transaction():
            cur = await conn.execute(
                "select id, timezone from orca_gw.tenants where slug = %s for update", (slug,)
            )
            row = await cur.fetchone()
            if row is None:
                raise ValueError(f"no such tenant: {slug}")
            if row["timezone"] == timezone:
                return
            await conn.execute(
                "update orca_gw.tenants set timezone = %s, updated_at = now() where id = %s",
                (timezone, row["id"]),
            )
            await conn.execute(
                "insert into orca_gw.config_audit "
                "(tenant_id, channel, action, before, after, actor) "
                "values (%s, null, 'timezone', %s, %s, %s)",
                (
                    row["id"],
                    json.dumps({"timezone": row["timezone"]}),
                    json.dumps({"timezone": timezone}),
                    actor,
                ),
            )

    async def update_channel_config(
        self, slug: str, channel: str, updates: dict, *, actor: str = "console"
    ) -> tuple[dict, dict]:
        """Panel 4 save: validate by constructing a ChannelConfig from the merged fields (the
        same model the request path trusts), write it, and record one config_audit row with the
        full before/after channel config. Raises pydantic's ValidationError on bad input -- the
        route turns that into a 400 with the field errors, never a partial write."""
        async with await self._connect() as conn, conn.transaction():
            cur = await conn.execute(
                f"select t.id as tenant_id, {_CHANNEL_COLS} "
                "from orca_gw.tenants t join orca_gw.tenant_channels tc on tc.tenant_id = t.id "
                "where t.slug = %s and tc.channel = %s for update",
                (slug, channel),
            )
            row = await cur.fetchone()
            if row is None:
                raise ValueError(f"no such tenant/channel: {slug}/{channel}")
            tenant_id = row.pop("tenant_id")
            before = dict(row)
            before["daily_spend_cap"] = (
                None if before["daily_spend_cap"] is None else float(before["daily_spend_cap"])
            )
            merged = {**before, **updates}
            cfg = ChannelConfig(**merged)  # raises ValidationError on bad input
            await self._upsert_channel(conn, tenant_id, cfg)
            after = cfg.model_dump(mode="json")
            await conn.execute(
                "insert into orca_gw.config_audit "
                "(tenant_id, channel, action, before, after, actor) "
                "values (%s, %s, %s, %s, %s, %s)",
                (
                    tenant_id,
                    channel,
                    "update_channel_config",
                    json.dumps(before, default=str),
                    json.dumps(after, default=str),
                    actor,
                ),
            )
            return before, after

    @staticmethod
    def _channel_values(tenant_id, ch: ChannelConfig) -> tuple:
        """The `(tenant_id, {_CHANNEL_COLS})` value tuple, shared by every writer of
        `tenant_channels` so the column list and the value order can't drift apart."""
        return (
            tenant_id,
            ch.channel,
            ch.is_enabled,
            ch.agent_id,
            ch.elevenlabs_agent_id,
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
            ch.max_concurrent_runs,
            ch.kill_switch,
            ch.allowed_origins,
            ch.spoken_kill_switch,
            ch.spoken_error_fallback,
            ch.kill_switch_message,
            ch.error_fallback_message,
            ch.phone_guard,
            ch.allowed_phone_numbers,
            ch.phone_guard_message,
        )

    @classmethod
    async def _upsert_channel(cls, conn, tenant_id, ch: ChannelConfig) -> None:
        await conn.execute(
            f"insert into orca_gw.tenant_channels (tenant_id, {_CHANNEL_COLS}) values "
            f"({_CHANNEL_PLACEHOLDERS}) "
            "on conflict (tenant_id, channel) do update set "
            + ", ".join(f"{c.strip()} = excluded.{c.strip()}" for c in _CHANNEL_COLS.split(",")[1:])
            + ", updated_at = now()",
            cls._channel_values(tenant_id, ch),
        )

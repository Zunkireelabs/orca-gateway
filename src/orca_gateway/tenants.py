"""Tenant configuration as DATA: who a tenant is, how the gateway reaches its brain, and how each
channel behaves for it. Two tenants differ by rows, never by code.

Nothing here knows what a tenant's product does. Every field passes the test "would a spa, a school
or a dealership have this too?"; anything that fails belongs in the backend.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime
from typing import Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger("orca_gateway.tenants")

Channel = Literal["voice", "chat"]
OutOfHours = Literal["take_message", "say_closed", "handoff_anyway"]
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


class ChannelConfig(BaseModel):
    channel: Channel
    is_enabled: bool = True
    agent_id: str = "default"
    languages: list[str] = Field(min_length=1)
    default_language: str
    voice_id: str | None = None
    spoken_brand_name: str
    handoff_target: str | None = None
    # weekday (0=Mon..6=Sun) -> [[start, end], ...] local windows; None = no handoff configured.
    handoff_hours: dict[str, list[list[str]]] | None = None
    out_of_hours_behaviour: OutOfHours = "handoff_anyway"
    out_of_hours_message: str | None = None
    escalation_policy: Literal["none", "handoff", "instruction"] = "none"
    escalation_instruction: str | None = None
    closed_dates: list[date] = Field(default_factory=list)
    closed_weekdays: list[int] = Field(default_factory=list)
    max_session_seconds: int | None = None
    daily_spend_cap: float | None = None
    per_caller_rate_limit: int | None = None
    kill_switch: bool = False

    @field_validator("closed_weekdays")
    @classmethod
    def _weekdays_in_range(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("closed_weekdays must be 0 (Mon) .. 6 (Sun)")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> ChannelConfig:
        if self.default_language not in self.languages:
            raise ValueError("default_language must be one of languages")
        if self.out_of_hours_behaviour == "say_closed" and not self.out_of_hours_message:
            raise ValueError("say_closed requires out_of_hours_message")
        for day, windows in (self.handoff_hours or {}).items():
            if day not in {"0", "1", "2", "3", "4", "5", "6"}:
                raise ValueError(f"handoff_hours key {day!r} is not a weekday 0..6")
            for window in windows:
                if len(window) != 2:
                    raise ValueError("each handoff window is [start, end]")
                dtime.fromisoformat(window[0])
                dtime.fromisoformat(window[1])
        return self


class TenantConfig(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    display_name: str
    is_active: bool = True
    timezone: str = "Asia/Kathmandu"
    backend: Literal["zunkiree"] = "zunkiree"
    backend_config: dict = Field(default_factory=dict)
    channels: dict[str, ChannelConfig] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {v!r}") from exc
        return v

    def channel(self, name: Channel) -> ChannelConfig | None:
        return self.channels.get(name)


class TenantUnavailableError(Exception):
    """The tenant cannot serve this channel right now. Always fails CLOSED, never falls back."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def require_serving(cfg: TenantConfig | None, channel: Channel) -> ChannelConfig:
    """The single fail-closed gate: unknown, inactive, disabled or killed all stop here."""
    if cfg is None:
        raise TenantUnavailableError("unknown tenant")
    if not cfg.is_active:
        raise TenantUnavailableError("tenant inactive")
    ch = cfg.channel(channel)
    if ch is None or not ch.is_enabled:
        raise TenantUnavailableError("channel not enabled")
    if ch.kill_switch:
        raise TenantUnavailableError("kill switch on")
    return ch


@dataclass(frozen=True)
class Availability:
    open: bool
    reason: str | None = None  # "closed_date" | "closed_weekday" | "outside_handoff_hours"


def availability(cfg: TenantConfig, ch: ChannelConfig, now: datetime) -> Availability:
    """Channel availability in the tenant's timezone. This is NOT the product's own schedule (the
    product owns that); it answers only 'should the channel pick up / promise a human right now'."""
    local = now.astimezone(ZoneInfo(cfg.timezone))
    if local.date() in ch.closed_dates:
        return Availability(False, "closed_date")
    if local.weekday() in ch.closed_weekdays:
        return Availability(False, "closed_weekday")
    if ch.handoff_hours is not None:
        windows = ch.handoff_hours.get(str(local.weekday()), [])
        t = local.time().replace(tzinfo=None)
        if not any(dtime.fromisoformat(s) <= t < dtime.fromisoformat(e) for s, e in windows):
            return Availability(False, "outside_handoff_hours")
    return Availability(True)


class TenantRepository(Protocol):
    async def load(self, slug: str) -> TenantConfig | None: ...


class TenantStoreError(Exception):
    """The config store is unreachable and nothing recent enough is cached."""


class TenantStore:
    """Cache in front of a repository. Bounded on every axis: entries live `ttl_s`, at most
    `max_entries` are held, stale entries are served during an outage for at most `stale_grace_s`,
    and `invalidate()` is an explicit hook (a config edit must be able to take effect NOW)."""

    def __init__(
        self,
        repo: TenantRepository,
        *,
        ttl_s: float = 15.0,
        stale_grace_s: float = 300.0,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repo = repo
        self._ttl_s = ttl_s
        self._stale_grace_s = stale_grace_s
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[str, tuple[TenantConfig | None, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, slug: str) -> TenantConfig | None:
        entry = self._entries.get(slug)
        if entry is not None and self._clock() - entry[1] < self._ttl_s:
            return entry[0]
        lock = self._locks.setdefault(slug, asyncio.Lock())
        async with lock:
            entry = self._entries.get(slug)  # another waiter may have refreshed it
            if entry is not None and self._clock() - entry[1] < self._ttl_s:
                return entry[0]
            try:
                value = await self._repo.load(slug)
            except Exception as exc:
                if entry is not None and self._clock() - entry[1] < self._stale_grace_s:
                    logger.warning("tenant store unreachable; serving stale slug=%s: %s", slug, exc)
                    return entry[0]
                raise TenantStoreError(slug) from exc
            self._entries[slug] = (value, self._clock())
            while len(self._entries) > self._max_entries:
                del self._entries[min(self._entries, key=lambda k: self._entries[k][1])]
            return value

    def invalidate(self, slug: str | None = None) -> None:
        if slug is None:
            self._entries.clear()
        else:
            self._entries.pop(slug, None)

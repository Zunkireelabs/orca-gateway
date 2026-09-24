"""P2 brief A5: a per-tenant, per-channel concurrency cap, sitting UNDER the per-environment
ceiling (`ORCA_VOICE_MAX_CONCURRENT_RUNS`, enforced by `coalescer.TurnCoalescer`'s own semaphore).

One tenant can never use another tenant's slots: each (tenant, channel) pair gets its own
`asyncio.Semaphore`, sized only from that pair's own configured cap, never shared and never
resized from another key's write. A tenant/channel with no cap configured is unbounded HERE --
still bounded by the shared environment ceiling, same as before this module existed.

Wired into `TurnCoalescer.submit()`'s `slot` argument, entered OUTSIDE the environment semaphore
(coalescer.py acquires it first, then the environment slot): a tenant waiting on its own,
saturated cap never ties up a slot another tenant could otherwise use.
"""

from __future__ import annotations

import asyncio


class _NullSlot:
    """No per-tenant cap configured: a no-op async context manager, so the caller's shape (an
    async context manager either way) never has to branch on whether a cap exists."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


_NULL_SLOT = _NullSlot()


class TenantConcurrencyLimiter:
    def __init__(self) -> None:
        self._semaphores: dict[tuple[str, str], asyncio.Semaphore] = {}
        self._limits: dict[tuple[str, str], int] = {}

    def slot(self, tenant_slug: str, channel: str, limit: int | None):
        """An async context manager for one backend run's slot at (tenant_slug, channel). `limit`
        is that pair's OWN configured cap (`ChannelConfig.max_concurrent_runs`); `None` means no
        cap is configured for it.

        A changed limit takes effect for the NEXT run to arrive at this key: `asyncio.Semaphore`
        cannot be resized in place, so a new one replaces it. A run already holding a slot on the
        old semaphore keeps it (that semaphore's own counter accounted for it); new arrivals only
        ever wait on the fresh one, sized to the new limit."""
        if limit is None:
            return _NULL_SLOT
        key = (tenant_slug, channel)
        sem = self._semaphores.get(key)
        if sem is None or self._limits.get(key) != limit:
            sem = asyncio.Semaphore(limit)
            self._semaphores[key] = sem
            self._limits[key] = limit
        return sem  # asyncio.Semaphore is itself an async context manager

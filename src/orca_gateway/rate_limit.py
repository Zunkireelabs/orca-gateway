"""A per-caller request-rate limiter, sized from `ChannelConfig.per_caller_rate_limit`: how many
turns one caller may send in a trailing window. Generic across channels and callers -- nothing
here knows what a "caller" is beyond a (tenant, channel, caller_key) tuple and a monotonic clock;
a channel adapter decides what a caller_key is (chat uses session_id and, separately, client IP).

Fixed trailing window (not a token bucket): simple, and correct enough for keeping one abusive
caller from drowning a tenant, not a precise SLA. `per_caller_rate_limit` is turns per
`window_s` (default 60s) -- the only unit this column has ever had a value for (P3 brief A2).
"""

from __future__ import annotations

import time
from collections import deque


class CallerRateLimiter:
    def __init__(self, *, window_s: float = 60.0, clock=time.monotonic) -> None:
        self._window_s = window_s
        self._clock = clock
        self._hits: dict[tuple[str, str, str], deque[float]] = {}

    def allow(self, tenant_slug: str, channel: str, caller_key: str, limit: int | None) -> bool:
        """True and records the hit if this caller may proceed now; False (and un-recorded --
        a caller that keeps getting refused must not shrink its own future window) if it has
        already made `limit` calls in the trailing `window_s`. `limit=None` means unbounded:
        always True, and nothing is tracked for a caller nobody asked to be limited."""
        if limit is None:
            return True
        key = (tenant_slug, channel, caller_key)
        now = self._clock()
        cutoff = now - self._window_s
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= limit:
            if not hits:
                del self._hits[key]
            return False
        hits.append(now)
        return True

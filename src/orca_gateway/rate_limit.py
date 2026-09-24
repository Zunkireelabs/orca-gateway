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
    def __init__(
        self, *, window_s: float = 60.0, idle_ttl_s: float | None = None, clock=time.monotonic
    ) -> None:
        self._window_s = window_s
        # A key (e.g. a widget session_id) is almost always seen exactly once -- a browser tab
        # rarely revisits the same session. Without a sweep this dict grows forever, one entry
        # per visitor for the life of the process. Same idle-TTL shape as
        # `coalescer.TurnCoalescer._prune()`: swept opportunistically on every `allow()` call,
        # never a background task of its own. Default is 10x the window: generous enough that a
        # bursty-but-legitimate caller is never pruned mid-window.
        self._idle_ttl_s = idle_ttl_s if idle_ttl_s is not None else window_s * 10
        self._clock = clock
        self._hits: dict[tuple[str, str, str], deque[float]] = {}
        self._last_seen: dict[tuple[str, str, str], float] = {}

    def allow(self, tenant_slug: str, channel: str, caller_key: str, limit: int | None) -> bool:
        """True and records the hit if this caller may proceed now; False (and un-recorded --
        a caller that keeps getting refused must not shrink its own future window) if it has
        already made `limit` calls in the trailing `window_s`. `limit=None` means unbounded:
        always True, and nothing is tracked for a caller nobody asked to be limited."""
        if limit is None:
            return True
        now = self._clock()
        self._prune_idle(now)
        key = (tenant_slug, channel, caller_key)
        cutoff = now - self._window_s
        hits = self._hits.setdefault(key, deque())
        self._last_seen[key] = now
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True

    def _prune_idle(self, now: float) -> None:
        cutoff = now - self._idle_ttl_s
        for key in [k for k, seen in self._last_seen.items() if seen < cutoff]:
            del self._last_seen[key]
            self._hits.pop(key, None)

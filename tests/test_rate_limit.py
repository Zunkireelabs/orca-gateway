"""Unit tests for CallerRateLimiter, in particular the idle-key prune follow-up: a widget
session_id is almost always seen exactly once, so without a sweep `_hits`/`_last_seen` grow one
entry per visitor for the life of the process."""

from orca_gateway.rate_limit import CallerRateLimiter


def test_allow_within_limit_then_blocks_over_limit():
    clock = [0.0]
    limiter = CallerRateLimiter(window_s=60.0, clock=lambda: clock[0])
    assert limiter.allow("t", "chat", "caller-1", 2) is True
    assert limiter.allow("t", "chat", "caller-1", 2) is True
    assert limiter.allow("t", "chat", "caller-1", 2) is False


def test_unbounded_when_limit_is_none_tracks_nothing():
    limiter = CallerRateLimiter()
    assert limiter.allow("t", "chat", "caller-1", None) is True
    assert limiter._hits == {}
    assert limiter._last_seen == {}


def test_window_expiry_allows_again_after_the_window_passes():
    clock = [0.0]
    limiter = CallerRateLimiter(window_s=60.0, clock=lambda: clock[0])
    assert limiter.allow("t", "chat", "caller-1", 1) is True
    assert limiter.allow("t", "chat", "caller-1", 1) is False
    clock[0] = 61.0
    assert limiter.allow("t", "chat", "caller-1", 1) is True


def test_idle_keys_are_pruned_once_the_idle_ttl_elapses():
    clock = [0.0]
    limiter = CallerRateLimiter(window_s=60.0, idle_ttl_s=300.0, clock=lambda: clock[0])
    for i in range(500):
        limiter.allow("t", "chat", f"session-{i}", 5)
    assert len(limiter._hits) == 500

    # Idle TTL elapses with no further traffic from any of those one-off sessions; the next
    # allow() call for an unrelated key sweeps them all out opportunistically.
    clock[0] = 301.0
    limiter.allow("t", "chat", "a-brand-new-session", 5)

    assert len(limiter._hits) == 1
    assert len(limiter._last_seen) == 1
    assert ("t", "chat", "a-brand-new-session") in limiter._last_seen


def test_a_caller_still_within_its_idle_ttl_is_not_pruned():
    clock = [0.0]
    limiter = CallerRateLimiter(window_s=60.0, idle_ttl_s=300.0, clock=lambda: clock[0])
    limiter.allow("t", "chat", "caller-1", 5)
    clock[0] = 100.0
    limiter.allow("t", "chat", "caller-2", 5)  # triggers a sweep, but caller-1 is still fresh
    assert ("t", "chat", "caller-1") in limiter._hits
    assert ("t", "chat", "caller-2") in limiter._hits

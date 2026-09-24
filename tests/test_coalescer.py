import asyncio

import pytest

from orca_gateway.coalescer import ClientGoneError, StaleTurnError, TurnCoalescer


async def _never_gone() -> bool:
    return False


class _Work:
    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.started: list[str] = []
        self.cancelled: list[str] = []
        self.finished: list[str] = []
        self.inflight = 0
        self.max_inflight = 0

    async def __call__(self, text: str) -> str:
        self.started.append(text)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(self.delay)
            self.finished.append(text)
            return f"answer:{text}"
        except asyncio.CancelledError:
            self.cancelled.append(text)
            raise
        finally:
            self.inflight -= 1


async def test_variants_at_same_depth_share_one_run_and_newest_wins():
    c, w = TurnCoalescer(debounce_s=0.05), _Work()
    tasks = [asyncio.create_task(c.submit("t", 5, v, w, _never_gone)) for v in ["a", "b", "c", "d"]]
    results = await asyncio.gather(*tasks)
    assert results == ["answer:d"] * 4  # every variant's caller gets the one answer
    assert w.started == ["d"]  # debounce meant the superseded variants never reached the backend


def _decisions():
    seen: list[str] = []
    return seen, seen.append


async def test_different_text_after_the_backend_started_joins_and_never_cancels():
    """POLICY REVERSED from S1 (which restarted and cancelled the in-flight run): once work() has
    started, a newer hypothesis JOINS it. The backend runs once, on the first text it saw."""
    c, w = TurnCoalescer(debounce_s=0.0), _Work(delay=0.3)
    seen, on = _decisions()
    first = asyncio.create_task(c.submit("t", 5, "a", w, _never_gone, on_decision=on))
    await asyncio.sleep(0.1)  # 'a' is now in flight against the backend
    second = asyncio.create_task(c.submit("t", 5, "b", w, _never_gone, on_decision=on))
    assert await first == await second == "answer:a"
    assert w.started == ["a"] and w.cancelled == [] and w.max_inflight == 1
    assert seen == ["started", "joined_late"]


async def test_different_text_inside_the_debounce_window_restarts_for_free():
    c, w = TurnCoalescer(debounce_s=0.2), _Work(delay=0.05)
    seen, on = _decisions()
    first = asyncio.create_task(c.submit("t", 5, "a", w, _never_gone, on_decision=on))
    await asyncio.sleep(0.05)  # still inside the debounce: nothing has reached the backend
    second = asyncio.create_task(c.submit("t", 5, "b", w, _never_gone, on_decision=on))
    assert await first == await second == "answer:b"
    assert w.started == ["b"] and w.cancelled == []  # the superseded text never ran at all
    assert seen == ["started", "restarted"]


async def test_slow_backend_and_alternating_hypotheses_make_exactly_one_backend_call():
    """The shape of the live failure (26 requests over 30s, 12 restarts): a slow backend while
    the platform keeps alternating two hypotheses and re-sending. One call, none cancelled."""
    c, w = TurnCoalescer(debounce_s=0.05), _Work(delay=2.0)
    seen, on = _decisions()

    async def fire(i):
        await asyncio.sleep(0.1 + i * 0.08)  # all after the debounce, spread over the run
        return await c.submit("t", 5, ("hyp-a", "hyp-b")[i % 2], w, _never_gone, on_decision=on)

    results = await asyncio.gather(*[fire(i) for i in range(20)])
    assert results == ["answer:hyp-a"] * 20
    assert w.started == ["hyp-a"] and w.cancelled == [] and w.finished == ["hyp-a"]
    assert seen.count("started") == 1 and seen.count("restarted") == 0
    assert seen.count("joined_late") == 10 and seen.count("joined") == 9


async def test_different_text_after_the_run_finished_is_joined_after_done_not_joined():
    """A different text arriving once the run has answered gets the cached answer to the EARLIER
    text. It must be distinguishable in the log from an identical retry ('joined'): that is how an
    interim hypothesis answered before the final transcript shows up."""
    c, w = TurnCoalescer(debounce_s=0.0), _Work(delay=0.05)
    seen, on = _decisions()
    assert await c.submit("t", 5, "interim", w, _never_gone, on_decision=on) == "answer:interim"
    assert await c.submit("t", 5, "interim", w, _never_gone, on_decision=on) == "answer:interim"
    assert await c.submit("t", 5, "the final text", w, _never_gone, on_decision=on) == (
        "answer:interim"  # the later text is NOT answered
    )
    assert seen == ["started", "joined", "joined_after_done"]
    assert w.started == ["interim"]  # and it does not run again (that policy is not decided yet)


async def test_all_waiters_gone_before_the_backend_is_called_aborts_for_free():
    c, w = TurnCoalescer(debounce_s=1.0), _Work(delay=1.0)

    async def gone_after():
        await asyncio.sleep(0.15)
        return True

    with pytest.raises(ClientGoneError):
        await c.submit("t", 3, "a", w, gone_after)
    await asyncio.sleep(0.05)
    assert w.started == []  # never reached the backend, so nothing to finish or undo


async def test_all_waiters_gone_after_the_backend_started_lets_the_run_finish():
    """DECISION: a committed run is never abandoned, even when every caller disconnects. A backend
    call may have a side effect the gateway cannot see, and a write must land or fail, never
    half-land. The answer stays cached, so a retry at the same depth gets it without a rerun."""
    c, w = TurnCoalescer(debounce_s=0.0), _Work(delay=0.5)

    async def gone_after():
        await asyncio.sleep(0.15)
        return True

    with pytest.raises(ClientGoneError):
        await c.submit("t", 3, "a", w, gone_after)
    await asyncio.sleep(0.6)
    assert w.started == ["a"] and w.finished == ["a"] and w.cancelled == []
    assert await c.submit("t", 3, "a", w, _never_gone) == "answer:a"  # cached, not rerun
    assert w.started == ["a"]


async def test_one_disconnect_does_not_abort_while_another_waits():
    c, w = TurnCoalescer(debounce_s=0.0), _Work(delay=0.4)

    async def gone_soon():
        await asyncio.sleep(0.1)
        return True

    leaver = asyncio.create_task(c.submit("t", 3, "a", w, gone_soon))
    stayer = asyncio.create_task(c.submit("t", 3, "a", w, _never_gone))
    with pytest.raises(ClientGoneError):
        await leaver
    assert await stayer == "answer:a"
    assert w.started == ["a"] and w.cancelled == []


async def test_newer_depth_does_not_cancel_a_committed_older_run():
    """POLICY CHANGED from S1: the older run had reached the backend, so it finishes (its callers
    are told the turn is stale) and the newer depth's run waits for it, one at a time."""
    c, w = TurnCoalescer(debounce_s=0.0), _Work(delay=0.3)
    old = asyncio.create_task(c.submit("t", 3, "old", w, _never_gone))
    await asyncio.sleep(0.1)  # 'old' is committed
    new = asyncio.create_task(c.submit("t", 5, "new", w, _never_gone))
    with pytest.raises(StaleTurnError):
        await old
    assert await new == "answer:new"
    assert w.finished == ["old", "new"] and w.cancelled == [] and w.max_inflight == 1
    with pytest.raises(StaleTurnError):
        await c.submit("t", 3, "late", w, _never_gone)


async def test_newer_depth_still_supersedes_an_older_run_that_has_not_started():
    c, w = TurnCoalescer(debounce_s=0.5), _Work(delay=0.05)
    old = asyncio.create_task(c.submit("t", 3, "old", w, _never_gone))
    await asyncio.sleep(0.05)  # 'old' is still in its debounce
    new = asyncio.create_task(c.submit("t", 5, "new", w, _never_gone))
    with pytest.raises(StaleTurnError):
        await old
    assert await new == "answer:new"
    assert w.started == ["new"]


async def test_cancelling_a_run_that_waits_for_a_committed_one_does_not_cancel_that_one():
    """The newer depth's run waits for the committed run. If THAT waiter is itself cancelled
    (aborted because its callers left), the committed run must be unaffected."""
    c, w = TurnCoalescer(debounce_s=0.0), _Work(delay=0.4)
    old = asyncio.create_task(c.submit("t", 3, "old", w, _never_gone))
    await asyncio.sleep(0.1)

    async def gone_soon():
        await asyncio.sleep(0.1)
        return True

    new = asyncio.create_task(c.submit("t", 5, "new", w, gone_soon))
    with pytest.raises(ClientGoneError):
        await new
    with pytest.raises(StaleTurnError):
        await old  # its caller is told stale by the newer depth...
    await asyncio.sleep(0.4)
    assert w.finished == ["old"] and w.cancelled == []  # ...but the run itself completed


async def test_straggler_after_completion_returns_cached_answer_not_a_rerun():
    c, w = TurnCoalescer(debounce_s=0.0), _Work(delay=0.02)
    assert await c.submit("t", 5, "a", w, _never_gone) == "answer:a"
    assert await c.submit("t", 5, "a", w, _never_gone) == "answer:a"
    assert w.started == ["a"]  # a duplicate of a finished turn must not run the backend again


async def test_conversations_are_independent():
    c, w = TurnCoalescer(debounce_s=0.0), _Work(delay=0.05)
    a, b = await asyncio.gather(
        c.submit("x", 5, "one", w, _never_gone), c.submit("y", 5, "two", w, _never_gone)
    )
    assert (a, b) == ("answer:one", "answer:two")


async def test_backend_error_propagates_to_every_waiter():
    c = TurnCoalescer(debounce_s=0.0)

    async def boom(_: str):
        raise ConnectionError("x")

    results = await asyncio.gather(
        c.submit("t", 3, "a", boom, _never_gone),
        c.submit("t", 3, "a", boom, _never_gone),
        return_exceptions=True,
    )
    assert all(isinstance(r, ConnectionError) for r in results)


async def test_global_cap_limits_backend_runs_across_conversations():
    c, w = TurnCoalescer(debounce_s=0.0, max_concurrent_runs=1), _Work(delay=0.1)
    await asyncio.gather(*[c.submit(f"conv{i}", 5, "q", w, _never_gone) for i in range(4)])
    assert w.max_inflight == 1 and len(w.finished) == 4


async def test_cap_of_two_allows_two_at_once_but_no_more():
    c, w = TurnCoalescer(debounce_s=0.0, max_concurrent_runs=2), _Work(delay=0.1)
    await asyncio.gather(*[c.submit(f"conv{i}", 5, "q", w, _never_gone) for i in range(5)])
    assert w.max_inflight == 2


async def test_hung_backend_times_out_instead_of_waiting_forever():
    c = TurnCoalescer(debounce_s=0.0, run_timeout_s=0.1)

    async def hang(_: str):
        await asyncio.sleep(30)

    with pytest.raises(TimeoutError):
        await c.submit("t", 3, "a", hang, _never_gone)


async def test_failed_turn_is_not_cached_so_a_retry_reruns_the_backend():
    c = TurnCoalescer(debounce_s=0.0)
    calls: list[str] = []

    async def flaky(text: str) -> str:
        calls.append(text)
        if len(calls) == 1:
            raise ConnectionError("transient")
        return "ok"

    with pytest.raises(ConnectionError):
        await c.submit("t", 3, "a", flaky, _never_gone)
    assert await c.submit("t", 3, "a", flaky, _never_gone) == "ok"
    assert len(calls) == 2


async def test_a_failing_on_decision_callback_cannot_change_the_outcome():
    coalescer = TurnCoalescer(debounce_s=0.01)

    async def work(text):
        return f"done:{text}"

    async def not_gone():
        return False

    def boom(_decision):
        raise RuntimeError("logging blew up")

    assert await coalescer.submit("c", 1, "hi", work, not_gone, on_decision=boom) == "done:hi"


async def test_summary_tallies_how_each_depths_requests_were_handled():
    c, w = TurnCoalescer(debounce_s=0.2), _Work(delay=0.3)
    tasks = [
        asyncio.create_task(c.submit("t", 5, "a", w, _never_gone)),  # started
        asyncio.create_task(c.submit("t", 5, "b", w, _never_gone)),  # restarted (in debounce)
        asyncio.create_task(c.submit("t", 5, "b", w, _never_gone)),  # joined (same text)
    ]
    await asyncio.sleep(0.3)  # the run is committed now
    tasks.append(asyncio.create_task(c.submit("t", 5, "c", w, _never_gone)))  # joined_late
    await asyncio.gather(*tasks)
    s = c.summary("t", 5)
    first = s.pop("first_arrival")
    assert isinstance(first, float)
    assert s == {"requests": 4, "started": 1, "restarted": 1, "joined": 1, "joined_late": 1}
    assert c.summary("t", 99) is None and c.summary("nope", 5) is None
    # a stale request is tallied against ITS depth
    await c.submit("t", 7, "z", w, _never_gone)
    with pytest.raises(StaleTurnError):
        await c.submit("t", 5, "late", w, _never_gone)
    assert c.summary("t", 5)["stale"] == 1


# ---- the caller-supplied `slot` (P2 brief A5: a per-tenant cap sits OUTSIDE the environment
# semaphore, which the tests above already cover via max_concurrent_runs) ---------------------


class _TrackingSlot:
    """A minimal async context manager standing in for a per-tenant semaphore: records whether it
    was ever entered while the environment-wide slot was ALSO held, and vice versa."""

    def __init__(self):
        self.entered = 0
        self.was_held_during_work = False

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *exc_info):
        return False


async def test_slot_is_entered_around_the_backend_call():
    slot = _TrackingSlot()
    c = TurnCoalescer(debounce_s=0.0)

    async def work(text):
        assert slot.entered == 1  # already held by the time work() runs
        return f"done:{text}"

    assert await c.submit("t", 1, "hi", work, _never_gone, slot=slot) == "done:hi"
    assert slot.entered == 1


async def test_slot_is_only_touched_by_the_request_that_starts_or_restarts_a_run():
    # A request that merely JOINS an existing run never enters the slot it was given -- passing
    # one is harmless (it isn't awaited unless a new run actually starts).
    slots = [_TrackingSlot(), _TrackingSlot()]
    c, w = TurnCoalescer(debounce_s=0.05), _Work(delay=0.2)
    first = asyncio.create_task(c.submit("t", 5, "a", w, _never_gone, slot=slots[0]))
    await asyncio.sleep(0.1)  # 'a' is committed and running
    second = asyncio.create_task(c.submit("t", 5, "b", w, _never_gone, slot=slots[1]))
    await asyncio.gather(first, second)
    assert slots[0].entered == 1  # started the one run
    assert slots[1].entered == 0  # joined it late; never ran its own backend call


async def test_a_tenant_slot_never_blocks_another_tenants_slot():
    # Two independent semaphores (as TenantConcurrencyLimiter hands out), each capped at 1: tenant
    # A's runs never wait on tenant B's slot, and vice versa -- proven by both completing inside
    # a window that would only fit one run if they shared a single slot.
    import time as _time

    sem_a, sem_b = asyncio.Semaphore(1), asyncio.Semaphore(1)
    c, w = TurnCoalescer(debounce_s=0.0, max_concurrent_runs=2), _Work(delay=0.2)
    start = _time.monotonic()
    await asyncio.gather(
        c.submit("conv-a", 1, "q", w, _never_gone, slot=sem_a),
        c.submit("conv-b", 1, "q", w, _never_gone, slot=sem_b),
    )
    elapsed = _time.monotonic() - start
    assert elapsed < 0.35  # ran concurrently, not serialised by a shared slot
    assert w.max_inflight == 2

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


async def test_restart_after_backend_started_serializes_and_cancels_old_run():
    c, w = TurnCoalescer(debounce_s=0.0), _Work(delay=0.3)
    first = asyncio.create_task(c.submit("t", 5, "a", w, _never_gone))
    await asyncio.sleep(0.1)  # 'a' is now in flight against the backend
    second = asyncio.create_task(c.submit("t", 5, "b", w, _never_gone))
    assert await first == await second == "answer:b"
    assert w.cancelled == ["a"]
    assert w.max_inflight == 1  # never two backend calls at once for one conversation


async def test_all_waiters_gone_aborts_backend_run():
    c, w = TurnCoalescer(debounce_s=0.0), _Work(delay=1.0)

    async def gone_after():
        await asyncio.sleep(0.15)
        return True

    task = asyncio.create_task(c.submit("t", 3, "a", w, gone_after))
    with pytest.raises(ClientGoneError):
        await task
    await asyncio.sleep(0.05)
    assert w.cancelled == ["a"] and w.finished == []


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


async def test_newer_depth_supersedes_older_and_older_is_stale_afterwards():
    c, w = TurnCoalescer(debounce_s=0.0), _Work(delay=0.3)
    old = asyncio.create_task(c.submit("t", 3, "old", w, _never_gone))
    await asyncio.sleep(0.05)
    new = asyncio.create_task(c.submit("t", 5, "new", w, _never_gone))
    with pytest.raises(StaleTurnError):
        await old
    assert await new == "answer:new"
    with pytest.raises(StaleTurnError):
        await c.submit("t", 3, "late", w, _never_gone)


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

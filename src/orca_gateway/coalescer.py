"""Transport-level de-noising for a chatty channel.

A voice platform may send several requests for ONE spoken turn (one per speech-recognition
hypothesis), all sharing a conversation id and a history depth, and abort the losers later.
Running each against the backend multiplies load and risks repeating side effects.

This coalesces them: per conversation at most one backend run is in flight and requests at the
same depth share one result. It knows nothing about what a turn means.

The commit rule (the point of this module's safety story): a run is FREE to cancel only until it
is COMMITTED, and it commits the moment `work()` starts (after the debounce and after a run slot
is won). Before that, a newer hypothesis for the same turn restarts it (the newest wins), a newer
depth supersedes it, and it is aborted if every waiter disconnects: nothing has been handed to the
backend, so nothing can be half-done. After that, NOTHING cancels it because a request arrived:
- a request at the same depth with different text JOINS the in-flight run and gets its answer
  (`joined_late`, or `joined_after_done` once it has finished); the first text the backend
  saw wins;
- a newer depth does not cancel it; its own callers are told the turn is stale and it runs to
  completion in the background, and the newer depth's run waits for it;
- if every waiter disconnects it is STILL left to finish. A backend call may have side effects the
  gateway cannot see (this module is product-blind), and a write must land or fail, never
  half-land. Its answer stays cached for a retry at the same depth.
The only remaining cancellation of a committed run is the run timeout, a safety bound so a hung
backend cannot hold a slot forever; it is not triggered by any request.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field


class StaleTurnError(Exception):
    """The conversation has already moved past this turn's depth."""


class ClientGoneError(Exception):
    """Every caller waiting on this turn disconnected."""


class _NullSlot:
    """No caller-supplied slot: a no-op async context manager, so `_run()` can always do
    `async with slot:` without branching on whether one was given."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


_NULL_SLOT = _NullSlot()


@dataclass
class _Entry:
    text: str
    result: asyncio.Future = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    task: asyncio.Task | None = None
    waiters: int = 0
    committed: bool = False  # work() has started: from here on nothing cancels this run


@dataclass
class _Conversation:
    entries: dict[int, _Entry] = field(default_factory=dict)
    max_depth: int = -1
    last_seen: float = field(default_factory=time.monotonic)
    # The committed run still executing for this conversation (any depth), if any: a later run
    # waits for it, so "at most one backend call per conversation in flight" survives a newer
    # depth arriving while an older committed run is still going.
    running: asyncio.Task | None = None
    # depth -> how the requests for that turn were handled: {"requests": n, "started": n,
    # "restarted": n, "joined": n, "joined_late": n, "stale": n, "first_arrival": monotonic}.
    # Observability only (the transcript row stores it); never read by the coalescing logic.
    tallies: dict[int, dict] = field(default_factory=dict)


class TurnCoalescer:
    def __init__(
        self,
        *,
        debounce_s: float = 0.3,
        idle_ttl_s: float = 600.0,
        max_concurrent_runs: int = 1,
        run_timeout_s: float = 25.0,
    ) -> None:
        self._debounce_s = debounce_s
        self._slots = asyncio.Semaphore(max_concurrent_runs)  # across ALL conversations
        self._run_timeout_s = run_timeout_s
        self._idle_ttl_s = idle_ttl_s
        self._convs: dict[str, _Conversation] = {}

    async def submit(
        self,
        conversation_id: str,
        depth: int,
        text: str,
        work: Callable[[str], Awaitable],
        gone: Callable[[], Awaitable[bool]],
        on_decision: Callable[[str], None] | None = None,
        slot: AbstractAsyncContextManager | None = None,
    ):
        """Run (or join) the turn at `depth`. `gone()` reports this caller disconnecting.
        `on_decision`, if given, is told what was decided for THIS request -- "stale", "started",
        "restarted" (newer text, run not yet committed), "joined" (same text), "joined_late"
        (different text, run committed and still running) or "joined_after_done" (different text,
        run already finished; it gets the cached answer to the earlier text) -- for observability
        only; it never changes the outcome.

        `slot`, if given, is an extra async context manager entered around the actual backend
        call, OUTSIDE this coalescer's own environment-wide semaphore (see `_run`) -- e.g. a
        per-tenant concurrency cap, which must never hold a shared environment slot idle while it
        waits on its own, narrower one. Only used when this call actually starts or restarts a
        run; a request that merely joins an existing one never touches it."""
        self._prune()
        conv = self._convs.setdefault(conversation_id, _Conversation())
        conv.last_seen = time.monotonic()

        def decide(decision: str) -> None:
            tally = conv.tallies.setdefault(
                depth, {"requests": 0, "first_arrival": time.monotonic()}
            )
            tally["requests"] += 1
            tally[decision] = tally.get(decision, 0) + 1
            if on_decision is None:
                return
            try:
                on_decision(decision)
            except Exception:  # observability must never change what the coalescer does
                pass

        if depth < conv.max_depth:
            decide("stale")
            raise StaleTurnError(depth)
        if depth > conv.max_depth:
            for old_depth, old in list(conv.entries.items()):
                if old.committed and not old.result.done():
                    # Already handed to the backend: its callers are told the turn is stale, but
                    # the run itself is never cancelled by this arrival (see module docstring).
                    self._release(old, StaleTurnError(old_depth))
                else:
                    self._abort(old, StaleTurnError(old_depth))
                del conv.entries[old_depth]
            conv.max_depth = depth

        entry = conv.entries.get(depth)
        if entry is None:
            decide("started")
            entry = conv.entries[depth] = _Entry(text=text)
            entry.task = asyncio.create_task(self._run(conv, depth, entry, work, None, slot))
        elif not entry.result.done() and entry.text != text and not entry.committed:
            # Newer hypothesis for the same turn, and nothing has reached the backend yet:
            # restarting is free. Restart, after the old run has fully stopped.
            decide("restarted")
            prev = entry.task
            if prev is not None:
                prev.cancel()
            entry.text = text
            entry.task = asyncio.create_task(self._run(conv, depth, entry, work, prev, slot))
        else:
            # Same text, or a run that is already committed (or finished): share its result.
            if entry.text == text:
                decide("joined")
            elif entry.result.done():
                # A different text arriving after the run already answered: it gets the cached
                # answer to the EARLIER text. Its own label, so the log shows the later text was
                # never answered (an interim hypothesis answered before the final transcript).
                decide("joined_after_done")
            else:
                decide("joined_late")

        entry.waiters += 1
        try:
            while not entry.result.done():
                if await gone():
                    raise ClientGoneError(depth)
                await asyncio.wait({entry.result}, timeout=0.1)
            return entry.result.result()
        finally:
            entry.waiters -= 1
            if entry.waiters == 0 and not entry.result.done() and not entry.committed:
                # Nobody is left and nothing has reached the backend: free to abort. A committed
                # run is deliberately left to finish (its result stays cached for a retry).
                self._abort(entry, ClientGoneError(depth))
                if conv.entries.get(depth) is entry:
                    del conv.entries[depth]

    def summary(self, conversation_id: str, depth: int) -> dict | None:
        """How the requests for this turn were handled so far (a copy), or None if unknown."""
        conv = self._convs.get(conversation_id)
        tally = None if conv is None else conv.tallies.get(depth)
        return None if tally is None else dict(tally)

    async def _run(
        self,
        conv: _Conversation,
        depth: int,
        entry: _Entry,
        work,
        prev: asyncio.Task | None,
        slot: AbstractAsyncContextManager | None = None,
    ) -> None:
        me = asyncio.current_task()
        # `wait`, never `await prev`: awaiting a task directly would let OUR cancellation cancel
        # it too (and it may be a committed run), and would raise its outcome at us.
        if prev is not None:
            await asyncio.wait({prev})
        while conv.running is not None and conv.running is not me and not conv.running.done():
            await asyncio.wait({conv.running})
        await asyncio.sleep(self._debounce_s)
        try:
            async with asyncio.timeout(self._run_timeout_s):
                # The caller's slot (e.g. a per-tenant cap) is OUTER: waiting on a narrower,
                # tenant-owned resource must never hold this environment-wide one idle. Both
                # waits count against the same run timeout as the environment slot always has.
                async with (slot if slot is not None else _NULL_SLOT):
                    async with self._slots:
                        entry.committed = True  # no await between winning the slot and this line
                        conv.running = me
                        try:
                            outcome = await work(entry.text)
                        finally:
                            if conv.running is me:
                                conv.running = None
        except asyncio.CancelledError:
            raise  # restarted, superseded or abandoned; the canceller settles the future
        except Exception as exc:
            if not entry.result.done():
                entry.result.set_exception(exc)
                entry.result.exception()  # mark retrieved; waiters may all be gone
            # Failures are not cached: the platform retries a failed request at the same
            # depth, and that retry must run the backend again, not replay the error.
            if conv.entries.get(depth) is entry:
                del conv.entries[depth]
        else:
            if not entry.result.done():
                entry.result.set_result(outcome)

    @staticmethod
    def _release(entry: _Entry, exc: Exception) -> None:
        """Tell this entry's callers `exc` WITHOUT cancelling its run (which is committed)."""
        if not entry.result.done():
            entry.result.set_exception(exc)
            entry.result.exception()

    @staticmethod
    def _abort(entry: _Entry, exc: Exception) -> None:
        if entry.task is not None and not entry.task.done():
            entry.task.cancel()
        if not entry.result.done():
            entry.result.set_exception(exc)
            entry.result.exception()

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._idle_ttl_s
        for cid in [c for c, v in self._convs.items() if v.last_seen < cutoff]:
            del self._convs[cid]

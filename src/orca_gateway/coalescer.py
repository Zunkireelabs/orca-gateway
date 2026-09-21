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
  (`joined_late`); the first text the backend saw wins;
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
from dataclasses import dataclass, field


class StaleTurnError(Exception):
    """The conversation has already moved past this turn's depth."""


class ClientGoneError(Exception):
    """Every caller waiting on this turn disconnected."""


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
    ):
        """Run (or join) the turn at `depth`. `gone()` reports this caller disconnecting.
        `on_decision`, if given, is told what was decided for THIS request -- "stale", "started",
        "restarted" (newer text, run not yet committed), "joined" (same text) or "joined_late"
        (different text, run already committed) -- for observability only; it never changes the
        outcome."""
        self._prune()

        def decide(decision: str) -> None:
            if on_decision is None:
                return
            try:
                on_decision(decision)
            except Exception:  # observability must never change what the coalescer does
                pass

        conv = self._convs.setdefault(conversation_id, _Conversation())
        conv.last_seen = time.monotonic()
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
            entry.task = asyncio.create_task(self._run(conv, depth, entry, work, None))
        elif not entry.result.done() and entry.text != text and not entry.committed:
            # Newer hypothesis for the same turn, and nothing has reached the backend yet:
            # restarting is free. Restart, after the old run has fully stopped.
            decide("restarted")
            prev = entry.task
            if prev is not None:
                prev.cancel()
            entry.text = text
            entry.task = asyncio.create_task(self._run(conv, depth, entry, work, prev))
        else:
            # Same text, or a run that is already committed (or finished): share its result.
            decide("joined_late" if entry.text != text and not entry.result.done() else "joined")

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

    async def _run(
        self, conv: _Conversation, depth: int, entry: _Entry, work, prev: asyncio.Task | None
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

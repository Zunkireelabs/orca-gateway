"""Transport-level de-noising for a chatty channel.

A voice platform may send several requests for ONE spoken turn (one per speech-recognition
hypothesis), all sharing a conversation id and a history depth, and abort the losers later.
Running each against the backend multiplies load and risks repeating side effects.

This coalesces them: per conversation at most one backend run is in flight, requests at the
same depth share one result, the newest hypothesis wins, and a run is aborted when nobody is
left waiting for it. It knows nothing about what a turn means.
"""

from __future__ import annotations

import asyncio
import contextlib
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


@dataclass
class _Conversation:
    entries: dict[int, _Entry] = field(default_factory=dict)
    max_depth: int = -1
    last_seen: float = field(default_factory=time.monotonic)


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
    ):
        """Run (or join) the turn at `depth`. `gone()` reports this caller disconnecting."""
        self._prune()
        conv = self._convs.setdefault(conversation_id, _Conversation())
        conv.last_seen = time.monotonic()
        if depth < conv.max_depth:
            raise StaleTurnError(depth)
        if depth > conv.max_depth:
            for old_depth, old in list(conv.entries.items()):
                self._abort(old, StaleTurnError(old_depth))
                del conv.entries[old_depth]
            conv.max_depth = depth

        entry = conv.entries.get(depth)
        if entry is None:
            entry = conv.entries[depth] = _Entry(text=text)
            entry.task = asyncio.create_task(self._run(entry, work, None))
        elif not entry.result.done() and entry.text != text:
            # Newer hypothesis for the same turn: restart, after the old run has fully stopped
            # so at most one backend call per conversation is ever in flight.
            prev = entry.task
            if prev is not None:
                prev.cancel()
            entry.text = text
            entry.task = asyncio.create_task(self._run(entry, work, prev))

        entry.waiters += 1
        try:
            while not entry.result.done():
                if await gone():
                    raise ClientGoneError(depth)
                await asyncio.wait({entry.result}, timeout=0.1)
            return entry.result.result()
        finally:
            entry.waiters -= 1
            if entry.waiters == 0 and not entry.result.done():
                self._abort(entry, ClientGoneError(depth))
                if conv.entries.get(depth) is entry:
                    del conv.entries[depth]

    async def _run(self, entry: _Entry, work, prev: asyncio.Task | None) -> None:
        if prev is not None:
            with contextlib.suppress(BaseException):
                await prev
        await asyncio.sleep(self._debounce_s)
        try:
            async with asyncio.timeout(self._run_timeout_s):
                async with self._slots:
                    outcome = await work(entry.text)
        except asyncio.CancelledError:
            raise  # restarted, superseded or abandoned; the canceller settles the future
        except Exception as exc:
            if not entry.result.done():
                entry.result.set_exception(exc)
                entry.result.exception()  # mark retrieved; waiters may all be gone
        else:
            if not entry.result.done():
                entry.result.set_result(outcome)

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

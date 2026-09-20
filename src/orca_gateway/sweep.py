"""The idle-timeout sweep: closes calls that have gone quiet, on its own clock, never on the
request path (see 0002_calls.sql for why "ended" has to be decided this way at all -- there is no
telephony hangup signal). Run as a background asyncio task inside the single uvicorn worker
(main.py's lifespan); this deliberately does NOT need a second process or an external cron -- the
gateway already runs exactly one worker (docker-compose.yml) and already keeps other per-process
state in memory (the coalescer, the backend-run cap) on that same assumption.
"""

from __future__ import annotations

import asyncio
import logging

from orca_gateway.calls_repo import PgCallsRepository

logger = logging.getLogger("orca_gateway.sweep")


async def run_forever(repo: PgCallsRepository, *, idle_s: float, interval_s: float) -> None:
    """Runs until cancelled. A single sweep failure (e.g. a transient DB blip) is logged and
    retried on the next tick rather than killing the loop -- metering must not be able to take
    the gateway down."""
    while True:
        try:
            closed = await repo.sweep_idle(idle_s)
            if closed:
                logger.info("idle-timeout sweep closed %d call(s): %s", len(closed), closed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("idle-timeout sweep failed; will retry next interval")
        await asyncio.sleep(interval_s)
